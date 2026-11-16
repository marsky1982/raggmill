"""API Key 加密存储：Fernet，密钥落本地 data/secret.key。"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import SECRET_KEY_FILE

_fernet: Fernet | None = None


class PresetKeyError(RuntimeError):
    """本地 secret.key 与预设密文不匹配或密文已损坏。"""

    error_code = "preset_key_invalid"


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if SECRET_KEY_FILE.exists():
            key = SECRET_KEY_FILE.read_bytes()
        else:
            key = Fernet.generate_key()
            SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            SECRET_KEY_FILE.write_bytes(key)
        _fernet = Fernet(key)
    return _fernet


def encrypt(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as exc:
        raise PresetKeyError(
            "API Key 解密失败：data/secret.key 可能已更换、丢失，或预设密文已损坏"
        ) from exc


def mask(api_key: str) -> str:
    """sk-abcdefgh1234 -> sk-****1234；过短则全掩码。"""
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:3]}****{api_key[-4:]}"
