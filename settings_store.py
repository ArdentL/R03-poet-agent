"""Windows DPAPI settings; private user-only JSON on Linux and macOS."""

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from integrations import ModelSettings, update_config


class SettingsError(ValueError):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


def dpapi(content: bytes, *, encrypt: bool) -> bytes:
    buffer = ctypes.create_string_buffer(content)
    source = DataBlob(len(content), ctypes.cast(buffer, ctypes.c_void_p))
    result = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    operation.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR if encrypt else ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class SettingsStore:
    def __init__(self, directory: Path) -> None:
        self.path = directory / ("settings.bin" if os.name == "nt" else "settings.json")
        self.lock = Lock()
        self.config = ModelSettings()
        if self.path.exists():
            try:
                content = self.path.read_bytes()
                if os.name == "nt":
                    content = dpapi(content, encrypt=False)
                stored = json.loads(content.decode("utf-8"))
                if "api_key" in stored:  # Migrate the previous single-provider settings.
                    self.config = update_config(
                        self.config,
                        {
                            key: stored[key]
                            for key in ("base_url", "model", "api_key")
                            if key in stored
                        },
                    )
                else:
                    self.config = ModelSettings.model_validate(stored)
            except (OSError, TypeError, ValueError) as exc:
                raise SettingsError(f"无法读取本机设置。请检查设置文件权限：{self.path}") from exc

    def get(self) -> ModelSettings:
        with self.lock:
            return self.config.model_copy(deep=True)

    def save(self, data: dict) -> ModelSettings:
        with self.lock:
            new_config = update_config(self.config, data)
            encoded = new_config.model_dump_json().encode("utf-8")
            temporary_path = None
            try:
                encrypted = dpapi(encoded, encrypt=True) if os.name == "nt" else encoded
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with NamedTemporaryFile(dir=self.path.parent, delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(encrypted)
                temporary_path.replace(self.path)
            except OSError as exc:
                raise SettingsError("无法保存本机设置，请检查当前账户的设置目录权限。") from exc
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            self.config = new_config
            return new_config.model_copy(deep=True)
