"""Loopback web application for the poetry workbench."""

import argparse
import json
import os
import secrets
import sys
import webbrowser
from functools import partial
from hmac import compare_digest
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from integrations import IntegrationError, call_model, public_config
from performance import PerformanceManager
from poetry import build_poetry_messages, check_poetry, normalize_poetry, read_poetry_spec
from seed_audio import synthesize
from settings_store import SettingsError, SettingsStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_DIR = STATIC_DIR.parent
PAGES = {
    "/": ("index.html", "text/html"),
    "/static/style.css": ("style.css", "text/css"),
    "/static/app.js": ("app.js", "text/javascript"),
    "/static/poetry.js": ("poetry.js", "text/javascript"),
}


FIELDS = {
    "title": (200, "诗词标题"),
    "poem_body": (6000, "诗词正文"),
    "tts_prompt": (32000, "TTS 提示词"),
    "author": (200, "作者"),
    "era": (200, "朝代"),
}


def field(data: dict, key: str, *, required: bool = False) -> str:
    limit, label = FIELDS[key]
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"{label}格式不正确或超过 {limit} 字。")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{label}。")
    return value


class PoetryHandler(BaseHTTPRequestHandler):
    def __init__(
        self, *args: Any, store: SettingsStore, token: str, performances: PerformanceManager
    ) -> None:
        self.store = store
        self.token = token
        self.performances = performances
        super().__init__(*args)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def respond(self, status: int, content: bytes, mime: str, *, cookie: bool = False) -> None:
        self.send_response(status)
        self.send_header(
            "Content-Type", mime if mime.startswith("audio/") else f"{mime}; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            (
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
                "style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
        )
        if cookie:
            self.send_header("Set-Cookie", f"poetry_token={self.token}; Path=/; SameSite=Strict")
        self.end_headers()
        self.wfile.write(content)

    def reply(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.respond(status, content, "application/json")

    def do_GET(self) -> None:
        if self.headers.get("Host") != self.host:
            self.reply({"error": "请从本机启动入口打开诗词智能体。"}, HTTPStatus.FORBIDDEN)
            return
        if self.path == "/api/start":
            config = public_config(self.store.get())
            self.reply({"config": config})
            return
        if self.path.startswith("/api/performances"):
            if not self.trusted_request(require_header=False):
                self.reply({"error": "本地会话已过期，请刷新页面。"}, HTTPStatus.FORBIDDEN)
                return
            parts = self.path.strip("/").split("/")
            try:
                if parts == ["api", "performances"]:
                    job = self.performances.get()
                elif len(parts) in (3, 4) and parts[:2] == ["api", "performances"]:
                    job = self.performances.get(parts[2])
                    if len(parts) == 4:
                        if parts[3] != "audio" or job is None or not job.audio_ready:
                            raise FileNotFoundError()
                        audio = self.performances.directory / job.id / "audio.mp3"
                        self.respond(HTTPStatus.OK, audio.read_bytes(), "audio/mpeg")
                        return
                else:
                    raise FileNotFoundError()
            except (ValueError, OSError):
                self.reply({"error": "任务或音频不存在。"}, HTTPStatus.NOT_FOUND)
            else:
                self.reply({"performance": job.model_dump(mode="json") if job else None})
            return
        if self.path == "/favicon.ico":
            self.respond(HTTPStatus.NO_CONTENT, b"", "text/plain")
            return
        page = PAGES.get(self.path)
        if page is None:
            self.reply({"error": "页面不存在。"}, HTTPStatus.NOT_FOUND)
            return
        filename, mime = page
        self.respond(
            HTTPStatus.OK, (STATIC_DIR / filename).read_bytes(), mime, cookie=self.path == "/"
        )

    def trusted_request(self, *, require_header: bool = True) -> bool:
        origin = self.headers.get("Origin")
        if self.headers.get("Host") != self.host or origin not in (None, f"http://{self.host}"):
            return False
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        header = self.headers.get("X-Poetry-Token", "").encode("latin-1")
        cookie = cookies.get("poetry_token")
        return bool(
            cookie
            and compare_digest(cookie.value.encode("utf-8"), self.token.encode("ascii"))
            and (not require_header or compare_digest(header, self.token.encode("ascii")))
        )

    def read_payload(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求应使用 JSON 格式。")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 256 * 1024:
            raise ValueError("请求为空或内容过长。")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError("请求中的 JSON 无法读取。") from exc
        if not isinstance(data, dict):
            raise ValueError("请求格式不正确。")
        return data

    def do_POST(self) -> None:
        if not self.trusted_request():
            self.reply({"error": "本地会话已过期，请刷新页面后重试。"}, HTTPStatus.FORBIDDEN)
            return
        actions = {
            "/api/settings": self.save_settings,
            "/api/poetry/generate": self.generate_poetry,
            "/api/poetry/check": self.check_poetry_draft,
            "/api/poetry/audio": self.generate_audio,
            "/api/performances": self.start_performance,
        }
        action = actions.get(self.path)
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "performances"] and parts[3] == "cancel":
            action = partial(self.cancel_performance, parts[2])
        if action is None:
            self.reply({"error": "操作不存在。"}, HTTPStatus.NOT_FOUND)
            return
        try:
            result = action(self.read_payload())
        except IntegrationError as exc:
            self.reply({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        except SettingsError as exc:
            self.reply({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except ValueError as exc:
            self.reply({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except OSError as exc:
            self.reply({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            if isinstance(result, bytes):
                self.respond(HTTPStatus.OK, result, "audio/mpeg")
            else:
                self.reply(result)

    def save_settings(self, data: dict) -> dict:
        return {"config": public_config(self.store.save(data))}

    def generate_poetry(self, data: dict) -> dict:
        spec = read_poetry_spec(data)
        body = field(data, "poem_body")
        title = field(data, "title") if body else ""
        metadata = {key: field(data, key) for key in ("author", "era") if body and key in data}
        messages = build_poetry_messages(spec, title, body)
        content = call_model(self.store.get(), messages, max_tokens=16000)
        try:
            return {"result": normalize_poetry(spec, content, body, title, metadata)}
        except ValueError as exc:
            raise IntegrationError(str(exc)) from exc

    def check_poetry_draft(self, data: dict) -> dict:
        return {
            "checks": [
                item.model_dump()
                for item in check_poetry(
                    read_poetry_spec(data),
                    field(data, "title"),
                    field(data, "poem_body"),
                    field(data, "tts_prompt"),
                )
            ]
        }

    def generate_audio(self, data: dict) -> bytes:
        return synthesize(field(data, "tts_prompt", required=True))

    def start_performance(self, data: dict) -> dict:
        job = self.performances.start(data, self.store.get())
        return {"performance": job.model_dump(mode="json")}

    def cancel_performance(self, job_id: str, data: dict) -> dict:
        return {"performance": self.performances.cancel(job_id).model_dump(mode="json")}


def main() -> int:
    parser = argparse.ArgumentParser(description="诗词智能体 · 本地诗词朗诵台")
    parser.add_argument("--port", type=int, default=8747)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--robot-config", type=Path, default=APP_DIR / "config" / "robot.json")
    args = parser.parse_args()
    default_data = (
        Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "PoetryDesk"
        if os.name == "nt"
        else Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "poetrydesk"
    )
    try:
        store = SettingsStore(args.data_dir or default_data)
        if args.data_dir is None and not store.path.exists():
            # Import existing credentials once; new settings contain only model connections.
            legacy = default_data.with_name("StoryDesk" if os.name == "nt" else "storydesk")
            if (legacy / store.path.name).exists():
                store.config = SettingsStore(legacy).get()
                store.save({})
        performances = PerformanceManager(
            APP_DIR / ".robot-poet" / "performances", args.robot_config
        )
        handler = partial(
            PoetryHandler, store=store, token=secrets.token_urlsafe(32), performances=performances
        )
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except (OSError, SettingsError) as exc:
        print(f"诗词智能体启动失败：{exc}", file=sys.stderr)
        return 1

    with server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"诗词智能体已启动 · {url}\n关闭此窗口或按 Ctrl+C 可以退出。", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            performances.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
