import hashlib
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ApiKey, User, aware, utc_now

_hasher = PasswordHash.recommended()
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="documud-auth-v1")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

API_KEY_PREFIX = "dm_"

# Verified against when the user doesn't exist, to equalise login timing.
_DUMMY_HASH = _hasher.hash("documud-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    return _hasher.verify(password, hashed or _DUMMY_HASH) and hashed is not None


def create_access_token(user_id: str) -> str:
    return _serializer.dumps({"uid": user_id})


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, hash_api_key(key)


def _unauthorized(detail: str = "Authentication required.") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def _user_from_session(token: str, db: Session) -> User:
    try:
        data = _serializer.loads(token, max_age=settings.access_token_minutes * 60)
    except (BadSignature, SignatureExpired):
        raise _unauthorized("Session expired. Please sign in again.")
    user = db.get(User, data.get("uid"))
    if not user or not user.is_active:
        raise _unauthorized("Session expired. Please sign in again.")
    return user


def _user_from_api_key(key: str, db: Session) -> User:
    record = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(key)))
    user = db.get(User, record.user_id) if record else None
    if not record or record.revoked_at or not user or not user.is_active:
        raise _unauthorized("Invalid or revoked API key.")
    now = utc_now()
    if not record.last_used_at or (now - aware(record.last_used_at)).total_seconds() > 60:
        record.last_used_at = now
        db.commit()
    return user


def get_session_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """Browser sessions only (used for managing API keys)."""
    if not token or token.startswith(API_KEY_PREFIX):
        raise _unauthorized()
    return _user_from_session(token, db)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    api_key: str | None = Depends(api_key_header),
    db: Session = Depends(get_db),
) -> User:
    """Session token, or an API key via `X-API-Key` / `Authorization: Bearer dm_...`."""
    key = api_key or (token if token and token.startswith(API_KEY_PREFIX) else None)
    if key:
        return _user_from_api_key(key, db)
    if token:
        return _user_from_session(token, db)
    raise _unauthorized()
