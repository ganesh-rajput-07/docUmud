import json

import httpx
import pymupdf
import pytest

from app.config import settings
from app.services import webhook_service
from app.services.webhook_service import deliver_due, sign
from tests.conftest import png_bytes

SIG = {"x": "0.5", "y": "0.7", "width": "0.25", "page_number": "0"}
TAG = {"page": 0, "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05, "label": "Invoice No", "value": "INV-1"}


def _drain():
    while deliver_due(transport=httpx.MockTransport(lambda r: httpx.Response(200))):
        pass


def test_webhook_signed_event_delivery(client, alice, upload):
    _drain()
    hook = client.post("/webhooks", headers=alice, json={"url": "http://localhost:9/hook", "events": ["signature.signed"]}).json()
    assert len(hook["secret"]) == 64 and "secret" not in client.get("/webhooks", headers=alice).json()[0]

    doc = upload(alice).json()
    body = {"document_id": doc["id"], "mode": "email", "recipient_email": "r@example.com"}
    req = client.post("/signing/requests", headers=alice, json=body).json()
    token = req["signing_link"].rsplit("/", 1)[1]
    # cancelled/other events are not subscribed -> nothing queued yet
    assert client.get(f"/webhooks/{hook['id']}/deliveries", headers=alice).json() == []
    r = client.post(f"/signing/public/{token}/sign", data={**SIG, "signer_name": "Rita", "consent": "true"},
                    files={"signature": ("s.png", png_bytes())})
    assert r.status_code == 200

    seen = []
    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200)
    assert deliver_due(transport=httpx.MockTransport(handler)) == 1
    sent = seen[0]
    payload = json.loads(sent.content)
    assert payload["event"] == "signature.signed" and payload["data"]["signer_name"] == "Rita"
    assert sent.headers["X-DocUMud-Signature"] == sign(hook["secret"], sent.headers["X-DocUMud-Timestamp"], sent.content)

    d = client.get(f"/webhooks/{hook['id']}/deliveries", headers=alice).json()
    assert d[0]["status"] == "delivered" and d[0]["attempts"] == 1


def test_webhook_retry_then_fail(client, alice, upload, monkeypatch):
    _drain()
    hook = client.post("/webhooks", headers=alice, json={"url": "http://localhost:9/x", "events": ["signature.cancelled"]}).json()
    doc = upload(alice).json()
    req = client.post("/signing/requests", headers=alice, json={"document_id": doc["id"], "mode": "in_person"}).json()
    client.post(f"/signing/requests/{req['id']}/cancel", headers=alice)

    failing = httpx.MockTransport(lambda r: httpx.Response(500))
    assert deliver_due(transport=failing) == 1
    d = client.get(f"/webhooks/{hook['id']}/deliveries", headers=alice).json()[0]
    assert d["status"] == "pending" and d["attempts"] == 1 and d["last_error"] == "HTTP 500"
    assert deliver_due(transport=failing) == 0  # backed off, not yet due

    monkeypatch.setattr(settings, "webhook_max_attempts", 1)
    from app.database import SessionLocal
    from app.models import WebhookDelivery, utc_now
    with SessionLocal() as db:
        row = db.get(WebhookDelivery, d["id"]); row.next_attempt_at = utc_now(); db.commit()
    deliver_due(transport=failing)
    assert client.get(f"/webhooks/{hook['id']}/deliveries", headers=alice).json()[0]["status"] == "failed"


def test_webhook_validation_and_isolation(client, alice, bob, monkeypatch):
    assert client.post("/webhooks", headers=alice, json={"url": "ftp://x.com"}).status_code == 422
    assert client.post("/webhooks", headers=alice, json={"url": "http://localhost:1/x", "events": ["nope"]}).status_code == 422
    hook = client.post("/webhooks", headers=alice, json={"url": "http://localhost:1/x"}).json()
    assert client.delete(f"/webhooks/{hook['id']}", headers=bob).status_code == 404
    assert client.get(f"/webhooks/{hook['id']}/deliveries", headers=bob).status_code == 404
    assert client.delete(f"/webhooks/{hook['id']}", headers=alice).status_code == 204
    assert client.get("/webhooks", headers=alice).json() == []

    monkeypatch.setattr(settings, "environment", "production")
    for bad in ("http://example.com/x", "https://127.0.0.1/x", "https://10.0.0.5/x", "https://169.254.169.254/latest"):
        assert client.post("/webhooks", headers=alice, json={"url": bad}).status_code == 422, bad


def test_rate_limit(client, monkeypatch):
    from app.services.rate_limit import limiter
    limiter._hits.clear()
    monkeypatch.setattr(settings, "auth_rate_limit_per_minute", 3)
    codes = [client.post("/auth/token", data={"username": "x@example.com", "password": "y" * 12}).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    r = client.post("/auth/token", data={"username": "x@example.com", "password": "y"})
    assert int(r.headers["Retry-After"]) > 0 and r.headers["X-RateLimit-Remaining"] == "0"
    limiter._hits.clear()


def test_api_rate_limit_is_per_key(client, alice, bob, monkeypatch):
    from app.services.rate_limit import limiter
    limiter._hits.clear()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    assert [client.get("/documents", headers=alice).status_code for _ in range(3)] == [200, 200, 429]
    assert client.get("/documents", headers=bob).status_code == 200
    limiter._hits.clear()


def test_export_tags_into_pdf(client, alice, upload):
    doc = upload(alice).json()
    url = f"/documents/{doc['id']}/tags"
    assert client.post(f"{url}/export", headers=alice).status_code == 409
    client.post(url, headers=alice, json=TAG)
    client.post(url, headers=alice, json={**TAG, "page": 1, "label": "Date", "value": ""})
    r = client.post(f"{url}/export", headers=alice)
    assert r.status_code == 201 and r.json()["source"] == "annotated" and r.json()["parent_id"] == doc["id"]

    data = client.get(f"/documents/{r.json()['id']}/download", headers=alice).content
    with pymupdf.open(stream=data) as pdf:
        a0 = list(pdf[0].annots())
        assert len(a0) == 1 and a0[0].info["title"] == "Invoice No" and a0[0].info["content"] == "INV-1"
        assert len(list(pdf[1].annots())) == 1
        embedded = json.loads(pdf.embfile_get("documud-tags.json"))
        assert {t["label"] for t in embedded} == {"Invoice No", "Date"}
    other = client.get(f"/documents/{doc['id']}/download", headers=alice).content
    with pymupdf.open(stream=other) as pdf:
        assert not list(pdf[0].annots())
