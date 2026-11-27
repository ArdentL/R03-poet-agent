"""Text-to-audio integration for the Doubao Speech HTTP API."""

import base64
import json
import os
import re
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import uuid4

from integrations import IntegrationError, NoApiRedirect

ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/create"
MODEL = "seed-audio-1.0"
PROMPT_LIMIT = 3000
RESPONSE_LIMIT = 16 * 1024 * 1024
KEY_NAMES = ("DOUBAO_SPEECH_API_KEY", "DOUBAO_AUDIO_API_KEY", "ARK_API_KEY")


def configured_key() -> str:
    app_dir = Path(__file__).resolve().parent
    env_paths = [app_dir / ".env", app_dir.parent / "sdk" / ".env"]
    if env_file := os.getenv("POET_AUDIO_ENV_FILE"):
        env_paths.insert(0, Path(env_file))
    values: dict[str, str] = {}
    for path in env_paths:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as error:
            raise IntegrationError("无法以 UTF-8 读取音频服务的 .env 配置。") from error
        for line in lines:
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            name = name.strip()
            if not separator or name not in KEY_NAMES:
                continue
            value = value.strip().strip("\"'")
            if value:
                values.setdefault(name, value)
    for name in KEY_NAMES:
        if key := (os.getenv(name, "").strip() or values.get(name)):
            if not re.fullmatch(r"[!-~]+", key):
                raise IntegrationError("音频服务的 API Key 格式无效。")
            return key
    raise IntegrationError("请在 .env 中配置豆包语音控制台的 DOUBAO_SPEECH_API_KEY。")


def synthesize(prompt: str) -> bytes:
    if not prompt.strip() or len(prompt) > PROMPT_LIMIT:
        raise ValueError(f"音频提示词须为 1 到 {PROMPT_LIMIT} 个字符。")
    key = configured_key()
    payload = {"model": MODEL, "text_prompt": prompt, "audio_config": {"format": "mp3"}}
    request = Request(
        ENDPOINT,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": key,
            "X-Api-Request-Id": str(uuid4()),
        },
    )
    try:
        with build_opener(NoApiRedirect()).open(request, timeout=300) as response:
            raw = response.read(RESPONSE_LIMIT + 1)
    except HTTPError as error:
        with error:
            status = error.code
            log_id = error.headers.get("X-Tt-Logid", "")
            try:
                details = json.loads(error.read(8192).decode("utf-8"))
            except (ValueError, OSError, HTTPException):
                details = {}
        service_code = details.get("code") if isinstance(details, dict) else None
        if status == 401:
            message = "豆包语音 API Key 验证失败（HTTP 401），请在 .env 配置豆包语音控制台的 DOUBAO_SPEECH_API_KEY。"
        elif status == 403:
            message = "豆包语音权限不足（HTTP 403），请在这把 Key 所属项目的「开通管理」确认已开通豆包音频生成模型 1.0，并检查 Key 的访问权限。"
        elif status == 429:
            message = "豆包语音请求过于频繁（HTTP 429），请稍后重试。"
        else:
            message = f"豆包语音请求失败（HTTP {status}）。"
        if type(service_code) is int:
            message += f" 服务错误码 {service_code}。"
        if re.fullmatch(r"[A-Za-z0-9-]{1,100}", log_id):
            message += f" 日志 ID：{log_id}"
        raise IntegrationError(message) from None
    except (OSError, URLError, HTTPException):
        raise IntegrationError("连接豆包语音失败或超时，请检查网络后重试。") from None
    if len(raw) > RESPONSE_LIMIT:
        raise IntegrationError("豆包语音返回的音频超过大小限制。")
    try:
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError()
        code = result.get("code")
        if type(code) is int and code != 0:
            raise IntegrationError(f"豆包语音生成失败，服务错误码 {code}。")
        audio = base64.b64decode(result.get("audio") or result.get("data") or "", validate=True)
        if not (
            audio.startswith(b"ID3")
            or (len(audio) >= 4 and audio[0] == 255 and audio[1] & 224 == 224)
        ):
            raise ValueError()
    except IntegrationError:
        raise
    except (ValueError, TypeError) as error:
        raise IntegrationError("豆包语音未返回有效的 MP3 音频。") from error
    return audio
