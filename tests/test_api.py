import pymupdf
import pytest

from tests.conftest import make_user, pdf_bytes, png_bytes

SIG = {"x": "0.5", "y": "0.7", "width": "0.25", "page_number": "0"}


def test_login_and_wrong_password(client, alice):
    assert client.get("/auth/me", headers=alice).status_code == 200
    r = client.post("/auth/token", data={"username": "nobody@example.com", "password": "x" * 12})
    assert r.status_code == 401
    assert client.get("/documents").status_code == 401


def test_duplicate_registration(client):
    make_user(client, "dup@example.com")
    r = client.post("/auth/register", json={"email": "dup@example.com", "password": "a-long-password-1"})
    assert r.status_code == 409


def test_upload_validation(client, alice, upload):
    assert upload(alice).status_code == 201
    assert upload(alice, b"not a pdf at all").status_code == 422
    assert upload(alice, b"%PDF-1.4 garbage").status_code == 422
    assert upload(alice, b"").status_code == 400
    r = upload(alice, png_bytes(100, 100), name="photo.png")
    assert r.status_code == 201 and r.json()["filename"].endswith(".pdf")


def test_oversize_rejected(client, alice, upload, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    assert upload(alice, pdf_bytes()).status_code == 413


def test_cross_user_isolation(client, alice, bob, upload):
    doc_id = upload(alice).json()["id"]
    assert client.get(f"/documents/{doc_id}/download", headers=bob).status_code == 404
    assert client.get(f"/documents/{doc_id}/pages/0.png", headers=bob).status_code == 404
    assert client.post(f"/documents/{doc_id}/tag", headers=bob).status_code == 404
    r = client.post("/signing/requests", headers=bob, json={"document_id": doc_id, "mode": "in_person"})
    assert r.status_code == 404
    assert client.get(f"/documents/{doc_id}/download", headers=alice).status_code == 200


def test_scanner_converts_images(client, alice):
    files = [("files", (f"{i}.jpg", png_bytes(300, 400, "JPEG"))) for i in range(3)]
    r = client.post("/scanner/convert", headers=alice, files=files)
    assert r.status_code == 201 and r.json()["pages"] == 3
    bad = client.post("/scanner/convert", headers=alice, files=[("files", ("x.jpg", b"junk"))])
    assert bad.status_code == 422


def test_sign_own_keeps_original(client, alice, upload):
    doc = upload(alice).json()
    r = client.post(f"/documents/{doc['id']}/sign", headers=alice, data={**SIG, "signer_name": "Alice"},
                    files={"signature": ("s.png", png_bytes())})
    assert r.status_code == 201
    signed = r.json()
    assert signed["parent_id"] == doc["id"] and signed["source"] == "signed"
    data = client.get(f"/documents/{signed['id']}/download", headers=alice).content
    with pymupdf.open(stream=data) as pdf:
        assert pdf[0].get_images() and "Signed by Alice" in pdf[0].get_text()
    assert len(client.get("/documents", headers=alice).json()) >= 2


def test_sign_rejects_bad_signature(client, alice, upload):
    doc = upload(alice).json()
    r = client.post(f"/documents/{doc['id']}/sign", headers=alice, data={**SIG, "signer_name": "A"},
                    files={"signature": ("s.png", b"nope")})
    assert r.status_code == 422


def _request(client, headers, doc_id, **kw):
    body = {"document_id": doc_id, "mode": "email", "recipient_email": "r@example.com", **kw}
    r = client.post("/signing/requests", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json(), r.json()["signing_link"].rsplit("/", 1)[1]


def _public_sign(client, token, **over):
    data = {**SIG, "signer_name": "Rita", "consent": "true", **over}
    return client.post(f"/signing/public/{token}/sign", data=data, files={"signature": ("s.png", png_bytes())})


def test_email_signing_flow_and_audit(client, alice, upload):
    doc = upload(alice).json()
    req, token = _request(client, alice, doc["id"])
    assert req["email_sent"] is False and req["status"] == "pending"
    assert client.get(f"/signing/public/{token}/info").json()["filename"] == "contract.pdf"
    assert client.get(f"/signing/public/{token}/pages/1.png").status_code == 200
    assert client.get(f"/signing/public/{token}/document").status_code == 200

    assert _public_sign(client, token, consent="false").status_code == 422
    assert _public_sign(client, token).status_code == 200
    # one-time: replay fails, link no longer serves the document
    assert _public_sign(client, token).status_code == 409
    assert client.get(f"/signing/public/{token}/document").status_code == 409

    listed = client.get("/signing/requests", headers=alice).json()
    mine = next(r for r in listed if r["id"] == req["id"])
    assert mine["status"] == "signed"
    signed = client.get(f"/documents/{mine['signed_document_id']}/download", headers=alice)
    assert signed.status_code == 200

    audit = client.get(f"/signing/requests/{req['id']}/events", headers=alice).json()
    assert audit["chain_valid"] is True
    assert [e["type"] for e in audit["events"]] == ["created", "email_not_sent", "signed"]


def test_in_person_and_cancel(client, alice, upload):
    doc = upload(alice).json()
    req, token = _request(client, alice, doc["id"], mode="in_person", recipient_email=None, recipient_name="Sam")
    assert req["mode"] == "in_person"
    assert client.post(f"/signing/requests/{req['id']}/cancel", headers=alice).status_code == 200
    assert client.get(f"/signing/public/{token}/info").status_code == 410
    assert _public_sign(client, token).status_code == 410
    assert client.post(f"/signing/requests/{req['id']}/cancel", headers=alice).status_code == 409


def test_expired_link(client, alice, upload):
    from datetime import datetime, timedelta, timezone
    from app.database import SessionLocal
    from app.models import SignatureRequest

    doc = upload(alice).json()
    req, token = _request(client, alice, doc["id"])
    with SessionLocal() as db:
        db.get(SignatureRequest, req["id"]).expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert client.get(f"/signing/public/{token}/info").status_code == 410
    assert client.get("/signing/public/not-a-token/info").status_code == 404


def test_tampered_document_blocks_signing(client, alice, upload):
    from app.database import SessionLocal
    from app.models import Document
    from app.services.storage import resolve

    doc = upload(alice).json()
    req, token = _request(client, alice, doc["id"])
    with SessionLocal() as db:
        path = resolve(db.get(Document, doc["id"]).file_path)
    path.write_bytes(pdf_bytes(3))
    assert _public_sign(client, token).status_code == 409
    assert client.get(f"/documents/{doc['id']}/download", headers=alice).status_code == 409


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.get("/scanner").status_code == 200
    assert client.get("/sign/anything").status_code == 200


def test_assets_are_cache_busted(client):
    html = client.get("/dashboard").text
    assert "/static/css/app.css?v=" in html and "/static/js/dashboard.js?v=" in html
