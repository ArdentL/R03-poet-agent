"""Store user settings in a Windows DPAPI-protected file."""

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from integrations import DEFAULT_CONFIG, update_config


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
        ctypes.POINTER(DataBlob), wintypes.LPCWSTR if encrypt else ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
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
        self.path = directory / "settings.bin"
        self.lock = Lock()
        self.config = DEFAULT_CONFIG.copy()
        if self.path.exists():
            try:
                stored = json.loads(dpapi(self.path.read_bytes(), encrypt=False).decode("utf-8"))
                self.config.update({key: stored[key] for key in DEFAULT_CONFIG if key in stored})
            except (OSError, TypeError, ValueError) as exc:
                raise SettingsError(f"无法读取本机设置。请检查设置文件权限：{self.path}") from exc

    def get(self) -> dict:
        with self.lock:
            return self.config.copy()

    def save(self, data: dict) -> dict:
        with self.lock:
            new_config = update_config(self.config, data)
            encoded = json.dumps(new_config, ensure_ascii=False).encode("utf-8")
            temporary_path = None
            try:
                encrypted = dpapi(encoded, encrypt=True)
                self.path.parent.mkdir(parents=True, exist_ok=True)
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
            return new_config.copy()
