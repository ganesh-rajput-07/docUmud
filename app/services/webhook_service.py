"""Outbound webhooks: outbox rows are created with the event, delivered by the worker."""
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Webhook, WebhookDelivery, aware, new_id, utc_now

logger = logging.getLogger("documud.webhooks")

EVENTS = ("signature.signed", "signature.cancelled", "job.completed", "job.failed")
BACKOFF_SECONDS = (30, 120, 600, 3600, 21600)  # delay before retry n+1


def validate_url(url: str) -> None:
    """Reject non-HTTPS and internal targets (SSRF). Development allows http/localhost."""
    parsed = urlparse(url)
    dev = settings.environment == "development"
    if parsed.scheme not in (("https", "http") if dev else ("https",)) or not parsed.hostname:
        raise ValueError("Webhook URL must be a valid https:// URL.")
    if dev:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("Webhook host could not be resolved.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("Webhook URL must not point to a private or internal address.")


def emit(db: Session, user_id: str, event: str, data: dict) -> None:
    """Queue `event` for the user's matching webhooks. Caller commits."""
    hooks = db.scalars(
        select(Webhook).where(Webhook.user_id == user_id, Webhook.active.is_(True))
    ).all()
    now = utc_now()
    for hook in hooks:
        if event not in hook.events.split(","):
            continue
        delivery_id = new_id()
        payload = {"id": delivery_id, "event": event, "created_at": now.isoformat(), "data": data}
        db.add(WebhookDelivery(
            id=delivery_id, webhook_id=hook.id, event=event,
            payload=json.dumps(payload), next_attempt_at=now,
        ))


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _attempt(client: httpx.Client, hook: Webhook, delivery: WebhookDelivery) -> None:
    body = delivery.payload.encode()
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    delivery.attempts += 1
    try:
        validate_url(hook.url)
        res = client.post(
            hook.url, content=body, follow_redirects=False,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"{settings.app_name}-Webhooks/1",
                "X-DocUMud-Event": delivery.event,
                "X-DocUMud-Delivery": delivery.id,
                "X-DocUMud-Timestamp": timestamp,
                "X-DocUMud-Signature": sign(hook.secret, timestamp, body),
            },
        )
        delivery.last_status_code = res.status_code
        if 200 <= res.status_code < 300:
            delivery.status, delivery.delivered_at, delivery.last_error = "delivered", utc_now(), None
            return
        delivery.last_error = f"HTTP {res.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        delivery.last_status_code = None
        delivery.last_error = str(exc)[:500] or exc.__class__.__name__

    if delivery.attempts >= settings.webhook_max_attempts:
        delivery.status = "failed"
    else:
        delay = BACKOFF_SECONDS[min(delivery.attempts - 1, len(BACKOFF_SECONDS) - 1)]
        delivery.next_attempt_at = utc_now() + timedelta(seconds=delay)


def deliver_due(limit: int = 20, transport: httpx.BaseTransport | None = None) -> int:
    """Send due deliveries; returns how many were attempted."""
    with SessionLocal() as db:
        due = db.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= utc_now())
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        ).all()
        if not due:
            return 0
        with httpx.Client(timeout=10, transport=transport) as client:
            for delivery in due:
                hook = db.get(Webhook, delivery.webhook_id)
                if not hook or not hook.active:
                    delivery.status, delivery.last_error = "failed", "Webhook removed"
                    continue
                _attempt(client, hook, delivery)
                db.commit()  # persist each result so a crash can't resend delivered events
        db.commit()
        return len(due)
