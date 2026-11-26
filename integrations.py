"""DeepSeek chat completions and SMTP delivery for the desktop app."""

import ipaddress
import json
import re
import smtplib
import ssl
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_CONFIG = {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key": "",
    "smtp_host": "",
    "smtp_port": 465,
    "smtp_security": "ssl",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "recipient": "",
}

SECRET_KEYS = ("api_key", "smtp_password")
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
RESPONSE_LIMIT = 1024 * 1024


class IntegrationError(ValueError):
    """An actionable error safe to display in the local interface."""


class NoApiRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
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


def _mailbox(raw: str, label: str) -> str:
    error = f"请为{label}填写一个有效邮箱地址。"
    try:
        address = Address(addr_spec=raw)
        domain = address.domain.encode("idna").decode("ascii")
    except (ValueError, UnicodeError, HeaderParseError):
        raise IntegrationError(error) from None
    normalized = Address(username=address.username, domain=domain).addr_spec
    if (
        not address.username
        or not address.username.isascii()
        or not _valid_host(domain)
        or len(normalized) > 254
        or address.addr_spec.casefold() != raw.casefold()
    ):
        raise IntegrationError(error)
    return normalized


def _sender(config: dict) -> str:
    if config["smtp_from"]:
        return config["smtp_from"]
    try:
        return _mailbox(config["smtp_user"], "发件人")
    except IntegrationError:
        return ""


def public_config(config: dict) -> dict:
    safe = {key: config[key] for key in DEFAULT_CONFIG if key not in SECRET_KEYS}
    safe["api_ready"] = bool(config["base_url"] and config["model"] and config["api_key"])
    safe["smtp_ready"] = bool(
        config["smtp_host"] and config["smtp_user"] and config["smtp_password"] and _sender(config)
    )
    return safe


def update_config(existing: dict, payload: dict) -> dict:
    config = {key: existing.get(key, default) for key, default in DEFAULT_CONFIG.items()}
    for key in (DEFAULT_CONFIG.keys() & payload.keys()) - {"smtp_port"}:
        value = payload[key]
        if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
            raise IntegrationError("配置字段格式不正确。")
        if len(value) > 2048:
            raise IntegrationError("配置字段太长。")
        if key in SECRET_KEYS and not value.strip():
            continue
        config[key] = value if key == "smtp_password" else value.strip()

    for key in SECRET_KEYS:
        clear = payload.get(f"clear_{key}", False)
        if not isinstance(clear, bool):
            raise IntegrationError("清除密钥选项格式不正确。")
        if clear:
            config[key] = ""

    config["base_url"] = _base_url(config["base_url"] or DEFAULT_CONFIG["base_url"])
    config["model"] = config["model"] or DEFAULT_CONFIG["model"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", config["model"]):
        raise IntegrationError("请填写有效的模型名称，例如 deepseek-flash。")
    if config["api_key"] and not re.fullmatch(r"[!-~]+", config["api_key"]):
        raise IntegrationError("API Key 含有空格或无效字符。")
    if config["smtp_security"] not in {"ssl", "starttls"}:
        raise IntegrationError("请选择 SSL/TLS 或 STARTTLS。")
    if config["smtp_host"] and not _valid_host(config["smtp_host"]):
        raise IntegrationError("SMTP 服务器请填写域名或 IP，端口请另填。")

    port = payload.get("smtp_port", config["smtp_port"])
    if port == "":
        port = 465 if config["smtp_security"] == "ssl" else 587
    if isinstance(port, str) and re.fullmatch(r"[0-9]{1,5}", port):
        port = int(port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise IntegrationError("SMTP 端口应为 1 到 65535 之间的整数。")
    config["smtp_port"] = port

    for key, label in (("smtp_from", "发件人"), ("recipient", "收件人")):
        if config[key]:
            config[key] = _mailbox(config[key], label)
    return config


def call_deepseek(config: dict, messages: list[dict], *, max_tokens: int = 4096) -> str:
    if not config["api_key"]:
        raise IntegrationError("请先在设置中填写 DeepSeek API Key。")
    endpoint = _base_url(config["base_url"])
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"

    payload = {
        "model": config["model"],
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }
    if urlsplit(endpoint).hostname == "api.deepseek.com" and config["model"] in {
        "deepseek-flash", "deepseek-v4-pro"
    }:
        payload["thinking"] = {"type": "disabled"}
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
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


def send_email(config: dict, title: str, body: str, recipient: str) -> str:
    if not public_config(config)["smtp_ready"]:
        raise IntegrationError("请先在设置中配置发件邮箱的 SMTP 服务和授权码。")
    target = _mailbox(recipient.strip(), "收件人")
    if not title.strip() or len(title) > 200 or any(char in title for char in "\r\n\x00"):
        raise IntegrationError("请填写 200 字以内的单行标题。")
    if not body.strip() or "\x00" in body:
        raise IntegrationError("请填写有效的故事正文。")

    message = EmailMessage()
    message["From"] = _sender(config)
    message["To"] = target
    message["Subject"] = title.strip()
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=message["From"].addresses[0].domain)
    message.set_content(f"{title.strip()}\n\n{body.strip()}\n", charset="utf-8", cte="quoted-printable")

    context = ssl.create_default_context()
    server = None
    sending = False
    host = config["smtp_host"]
    port = config["smtp_port"]
    try:
        if config["smtp_security"] == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls(context=context)
        server.login(config["smtp_user"], config["smtp_password"])
        sending = True
        if server.send_message(message):
            raise IntegrationError("SMTP 服务器拒绝了该收件地址。")
    except smtplib.SMTPAuthenticationError:
        raise IntegrationError("SMTP 登录失败，请检查邮箱账号与授权码。") from None
    except UnicodeError:
        raise IntegrationError("SMTP 账号或授权码含有邮箱服务不支持的字符。") from None
    except smtplib.SMTPRecipientsRefused:
        raise IntegrationError("SMTP 服务器拒绝了该收件地址。") from None
    except smtplib.SMTPSenderRefused:
        raise IntegrationError("SMTP 服务器拒绝了发件人，请检查发件地址。") from None
    except smtplib.SMTPNotSupportedError:
        raise IntegrationError("SMTP 服务器不支持所选加密或登录方式，请检查设置。") from None
    except ssl.SSLError:
        raise IntegrationError("SMTP 加密连接失败，请检查服务器地址、端口和加密方式。") from None
    except smtplib.SMTPResponseException:
        raise IntegrationError("SMTP 服务器拒绝了邮件请求，请检查邮箱配置和发件权限。") from None
    except OSError:
        if sending:
            raise IntegrationError("发送期间连接中断，邮件状态未知。重试前请先查收。") from None
        raise IntegrationError("连接 SMTP 服务器失败，请检查网络、地址和端口。") from None
    finally:
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
    return target
