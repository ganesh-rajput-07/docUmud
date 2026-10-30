import json
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, Webhook, WebhookDelivery, aware
from app.security import get_current_user
from app.services.webhook_service import EVENTS, validate_url

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

MAX_WEBHOOKS = 10


class WebhookIn(BaseModel):
    url: str = Field(max_length=2000)
    events: list[str] | None = Field(default=None, description=f"Any of {', '.join(EVENTS)}. Default: all.")


def _out(h: Webhook) -> dict:
    return {"id": h.id, "url": h.url, "events": h.events.split(","), "created_at": aware(h.created_at).isoformat()}


def _owned(db: Session, webhook_id: str, user: User) -> Webhook:
    hook = db.get(Webhook, webhook_id)
    if not hook or hook.user_id != user.id or not hook.active:
        raise HTTPException(404, "Webhook not found")
    return hook


@router.post("", status_code=201)
def create_webhook(body: WebhookIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Register an endpoint. The returned `secret` is shown once; use it to verify
    `X-DocUMud-Signature` = sha256 HMAC of `"{X-DocUMud-Timestamp}.{raw body}"`."""
    events = body.events or list(EVENTS)
    unknown = [e for e in events if e not in EVENTS]
    if unknown:
        raise HTTPException(422, f"Unknown event(s): {', '.join(unknown)}")
    try:
        validate_url(body.url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    count = len(db.scalars(select(Webhook.id).where(Webhook.user_id == user.id, Webhook.active.is_(True))).all())
    if count >= MAX_WEBHOOKS:
        raise HTTPException(409, f"You can have at most {MAX_WEBHOOKS} webhooks.")
    hook = Webhook(user_id=user.id, url=body.url, secret=secrets.token_hex(32), events=",".join(dict.fromkeys(events)))
    db.add(hook)
    db.commit()
    return {**_out(hook), "secret": hook.secret}


@router.get("")
def list_webhooks(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    hooks = db.scalars(
        select(Webhook).where(Webhook.user_id == user.id, Webhook.active.is_(True)).order_by(Webhook.created_at.desc())
    ).all()
    return [_out(h) for h in hooks]


@router.get("/{webhook_id}/deliveries")
def list_deliveries(webhook_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    hook = _owned(db, webhook_id, user)
    rows = db.scalars(
        select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook.id)
        .order_by(WebhookDelivery.created_at.desc()).limit(25)
    ).all()
    return [
        {"id": d.id, "event": d.event, "status": d.status, "attempts": d.attempts,
         "last_status_code": d.last_status_code, "last_error": d.last_error,
         "created_at": aware(d.created_at).isoformat(), "payload": json.loads(d.payload)}
        for d in rows
    ]


@router.delete("/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _owned(db, webhook_id, user).active = False
    db.commit()
