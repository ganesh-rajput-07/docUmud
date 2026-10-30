"""Symmetric encryption for secrets stored at rest (users' SMTP passwords)."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet:
    source = settings.encryption_key or settings.secret_key
    key = base64.urlsafe_b64encode(hashlib.sha256(f"documud-enc-v1:{source}".encode()).digest())
    return Fernet(key)


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str | None:
    """Returns None if the value can't be decrypted (e.g. the key changed)."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
