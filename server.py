"""Loopback web application for the story workbench."""

import argparse
from functools import partial
from hmac import compare_digest
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import webbrowser

from integrations import IntegrationError, call_deepseek, public_config, send_email
from settings_store import SettingsError, SettingsStore
from story import build_messages, check_story, normalize_result, read_spec
from poetry import build_poetry_messages, check_poetry, normalize_poetry, read_poetry_spec
from seed_audio import synthesize


STATIC_DIR = Path(__file__).resolve().parent / "static"
PAGES = {
    "/": ("index.html", "text/html"),
    "/static/style.css": ("style.css", "text/css"),
    "/static/app.js": ("app.js", "text/javascript"),
    "/static/poetry.js": ("poetry.js", "text/javascript"),
}


FIELDS = {
    "mode": (20, "创作操作"),
    "selected_idea": (1600, "选定构思"),
    "title": (200, "故事标题"),
    "body": (12000, "故事正文"),
    "revision": (2000, "改稿反馈"),
    "recipient": (254, "收件邮箱"),
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


class StoryHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, store: SettingsStore, token: str) -> None:
        self.store = store
        self.token = token
        super().__init__(*args)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def log_message(self, format: str, *args) -> None:
        pass

    def respond(self, status: int, content: bytes, mime: str, *, cookie: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime if mime.startswith("audio/") else f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
            "style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ))
        if cookie:
            self.send_header("Set-Cookie", f"story_token={self.token}; Path=/; SameSite=Strict")
        self.end_headers()
        self.wfile.write(content)

    def reply(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.respond(status, content, "application/json")

    def do_GET(self) -> None:
        if self.headers.get("Host") != self.host:
            self.reply({"error": "请从本机启动入口打开故事智能体。"}, HTTPStatus.FORBIDDEN)
            return
        if self.path == "/api/start":
            config = public_config(self.store.get())
            self.reply({"config": config})
            return
        if self.path == "/favicon.ico":
            self.respond(HTTPStatus.NO_CONTENT, b"", "text/plain")
            return
        page = PAGES.get(self.path)
        if page is None:
            self.reply({"error": "页面不存在。"}, HTTPStatus.NOT_FOUND)
            return
        filename, mime = page
        self.respond(HTTPStatus.OK, (STATIC_DIR / filename).read_bytes(), mime, cookie=self.path == "/")

    def trusted_request(self) -> bool:
        origin = self.headers.get("Origin")
        if self.headers.get("Host") != self.host or origin not in (None, f"http://{self.host}"):
            return False
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        header = self.headers.get("X-Story-Token", "").encode("latin-1")
        cookie = cookies.get("story_token")
        return bool(cookie and compare_digest(header, self.token.encode("ascii")) and header == cookie.value.encode("utf-8"))

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
            "/api/generate": self.generate,
            "/api/check": self.check,
            "/api/send": self.send_story,
            "/api/poetry/generate": self.generate_poetry,
            "/api/poetry/check": self.check_poetry_draft,
            "/api/poetry/audio": self.generate_audio,
        }
        action = actions.get(self.path)
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
        else:
            if isinstance(result, bytes):
                self.respond(HTTPStatus.OK, result, "audio/wav")
            else:
                self.reply(result)

    def save_settings(self, data: dict) -> dict:
        return {"config": public_config(self.store.save(data))}

    def generate(self, data: dict) -> dict:
        spec = read_spec(data)
        mode = field(data, "mode", required=True)
        messages = build_messages(
            spec, mode, field(data, "selected_idea"), field(data, "title"),
            field(data, "body"), field(data, "revision"),
        )
        content = call_deepseek(self.store.get(), messages, max_tokens=4096 if mode == "ideas" else 10000)
        try:
            return {"result": normalize_result(spec, content, mode)}
        except ValueError as exc:
            raise IntegrationError(str(exc)) from exc

    def check(self, data: dict) -> dict:
        return {"checks": check_story(read_spec(data), field(data, "title"), field(data, "body"))}

    def generate_poetry(self, data: dict) -> dict:
        spec = read_poetry_spec(data)
        body = field(data, "poem_body")
        title = field(data, "title") if body else ""
        metadata = {key: field(data, key) for key in ("author", "era") if body and key in data}
        messages = build_poetry_messages(spec, title, body)
        content = call_deepseek(self.store.get(), messages, max_tokens=16000)
        try:
            return {"result": normalize_poetry(spec, content, body, title, metadata)}
        except ValueError as exc:
            raise IntegrationError(str(exc)) from exc

    def check_poetry_draft(self, data: dict) -> dict:
        return {"checks": check_poetry(read_poetry_spec(data), field(data, "title"),
                                       field(data, "poem_body"), field(data, "tts_prompt"))}

    def generate_audio(self, data: dict) -> bytes:
        return synthesize(field(data, "tts_prompt", required=True))

    def send_story(self, data: dict) -> dict:
        title = field(data, "title", required=True)
        body = field(data, "body", required=True)
        recipient = field(data, "recipient", required=True)
        return {"recipient": send_email(self.store.get(), title, body, recipient)}


def main() -> int:
    parser = argparse.ArgumentParser(description="故事智能体 · 本地故事创作台")
    parser.add_argument("--port", type=int, default=8747)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    default_data = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "StoryDesk"
    try:
        store = SettingsStore(args.data_dir or default_data)
        handler = partial(StoryHandler, store=store, token=secrets.token_urlsafe(32))
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except (OSError, SettingsError) as exc:
        print(f"故事智能体启动失败：{exc}", file=sys.stderr)
        return 1

    with server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"故事智能体已启动 · {url}\n关闭此窗口或按 Ctrl+C 可以退出。", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
