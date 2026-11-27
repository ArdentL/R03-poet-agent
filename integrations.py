"""Typed settings and a shared chat-completions client for DeepSeek and Doubao."""

import ipaddress
import json
import re
from http.client import HTTPException
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

RESPONSE_LIMIT = 1024 * 1024
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


class IntegrationError(ValueError):
    """An actionable service error safe to display in the local interface."""


class NoApiRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _valid_host(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return all(HOST_LABEL.fullmatch(label) for label in host.split("."))
    return True


def _base_url(base: str) -> str:
    if not base or not base.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in base):
        raise IntegrationError("请填写有效的 API 地址。")
    if any(char in base for char in "?#\\"):
        raise IntegrationError("API 地址只需填写服务地址或接口路径。")
    try:
        parts = urlsplit(base)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        raise IntegrationError("请填写有效的 API 地址。") from None
    if not _valid_host(host) or parts.username is not None or port == 0:
        raise IntegrationError("请填写有效的 API 地址，地址中无需填账号或密钥。")
    if parts.scheme == "https":
        return base.rstrip("/")
    if parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return base.rstrip("/")
    raise IntegrationError("API 地址请使用 HTTPS。本机测试服务可使用 HTTP。")


class ModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_url: str
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    api_key: str = Field(default="", repr=False, pattern=r"^[!-~]*$", max_length=2048)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _base_url(value)


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["deepseek", "doubao"] = "deepseek"
    deepseek: ModelConnection = Field(
        default_factory=lambda: ModelConnection(
            base_url="https://api.deepseek.com", model="deepseek-flash"
        )
    )
    doubao: ModelConnection = Field(
        default_factory=lambda: ModelConnection(
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seed-2-1-turbo-260628",
        )
    )

    @property
    def connection(self) -> ModelConnection:
        return self.deepseek if self.provider == "deepseek" else self.doubao


def public_config(config: ModelSettings) -> dict[str, Any]:
    providers = {}
    for name in ("deepseek", "doubao"):
        connection = getattr(config, name)
        providers[name] = connection.model_dump(exclude={"api_key"}) | {
            "api_ready": bool(connection.api_key)
        }
    return {
        "provider": config.provider,
        "providers": providers,
        "api_ready": bool(config.connection.api_key),
    }


def update_config(existing: ModelSettings, payload: dict[str, Any]) -> ModelSettings:
    provider = payload.get("provider", existing.provider)
    if provider not in ("deepseek", "doubao"):
        raise IntegrationError("请选择 DeepSeek 或豆包。")
    values = getattr(existing, provider).model_dump()
    for key in ("base_url", "model", "api_key"):
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or any(char in value for char in "\r\n\0"):
            raise IntegrationError("模型设置格式不正确。")
        if key != "api_key" or value.strip():
            values[key] = value.strip()
    if payload.get("clear_api_key") is True:
        values["api_key"] = ""
    try:
        connection = ModelConnection.model_validate(values)
    except ValidationError:
        raise IntegrationError("模型设置格式无效，请检查 API 地址、模型名称和密钥。") from None
    return ModelSettings.model_validate(
        existing.model_dump() | {"provider": provider, provider: connection}
    )


def call_model(
    settings: ModelSettings, messages: list[dict[str, str]], *, max_tokens: int = 4096
) -> str:
    config = settings.connection
    if not config.api_key:
        raise IntegrationError("请先在模型设置中填写当前服务的 API Key。")
    endpoint = _base_url(config.base_url)
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"

    payload = {
        "model": config.model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }
    if settings.provider == "doubao":
        payload["thinking"] = {"type": "disabled"}
    elif urlsplit(endpoint).hostname == "api.deepseek.com" and config.model in {
        "deepseek-flash",
        "deepseek-v4-pro",
    }:
        payload["thinking"] = {"type": "disabled"}
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        timeout = 90 if max_tokens <= 4096 else 180
        with build_opener(NoApiRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(RESPONSE_LIMIT + 1)
    except HTTPError as error:
        code = error.code
        error.close()
        details = {
            400: "API 拒绝了请求，请确认模型和接口支持 JSON 输出。",
            401: "API Key 验证失败，请检查设置。",
            402: "API 账户余额不足。",
            403: "API 拒绝访问，请检查密钥权限。",
            404: "找不到 API 接口或模型，请检查设置。",
            429: "API 请求过于频繁，请稍后再试。",
        }
        if 300 <= code < 400:
            raise IntegrationError("API 地址返回重定向，请填写最终接口地址。") from None
        raise IntegrationError(details.get(code, f"API 请求失败（HTTP {code}）。")) from None
    except (URLError, OSError, HTTPException):
        raise IntegrationError("连接 API 失败或超时，请检查网络和接口地址。") from None

    if len(raw) > RESPONSE_LIMIT:
        raise IntegrationError("API 响应超过大小限制。")
    try:
        choice = json.loads(raw.decode("utf-8"))["choices"][0]
        content = choice["message"]["content"]
    except (UnicodeDecodeError, ValueError, KeyError, IndexError, TypeError):
        raise IntegrationError("API 返回的内容格式异常，请检查接口地址和模型。") from None
    if choice.get("finish_reason") == "length":
        raise IntegrationError("模型输出在完成前被截断，请缩短补充要求后重试。")
    if not isinstance(content, str) or not content.strip():
        raise IntegrationError("模型返回了空内容，请重新生成。")
    return content
