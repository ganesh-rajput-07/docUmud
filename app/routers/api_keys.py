from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ApiKey, User, utc_now
from app.security import generate_api_key, get_session_user

router = APIRouter(prefix="/api-keys", tags=["API keys"])

MAX_KEYS = 20


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _out(k: ApiKey) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "created_at": k.created_at.isoformat(),
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
    }


@router.post("", status_code=201)
def create_key(body: KeyIn, db: Session = Depends(get_db), user: User = Depends(get_session_user)):
    active = db.scalars(
        select(ApiKey.id).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
    ).all()
    if len(active) >= MAX_KEYS:
        raise HTTPException(409, f"You can have at most {MAX_KEYS} active API keys.")
    key, key_hash = generate_api_key()
    record = ApiKey(user_id=user.id, name=body.name.strip(), prefix=key[:11], key_hash=key_hash)
    db.add(record)
    db.commit()
    return {**_out(record), "key": key}  # the full key is returned only here


@router.get("")
def list_keys(db: Session = Depends(get_db), user: User = Depends(get_session_user)):
    keys = db.scalars(
        select(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
    ).all()
    return [_out(k) for k in keys]


@router.delete("/{key_id}", status_code=204)
def revoke_key(key_id: str, db: Session = Depends(get_db), user: User = Depends(get_session_user)):
    key = db.get(ApiKey, key_id)
    if not key or key.user_id != user.id or key.revoked_at:
        raise HTTPException(404, "API key not found")
    key.revoked_at = utc_now()
    db.commit()
