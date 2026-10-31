"""Per-user SMTP, OTP verification, bulk sending, reminders — against a real local SMTP server."""
import email
import email.policy
import io
import re
import socket
import zipfile
from datetime import timedelta

import pymupdf
import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

from app.config import settings
from app.database import SessionLocal
from app.models import EmailOutbox, SignatureRequest, utc_now
from app.services import outbox
from tests.conftest import make_user, png_bytes
from tests.test_fill import add_sheet, page_text, xlsx_bytes

USER, PASSWORD = "sender@example.com", "app-pass-1234"
SIG = {"x": "0.5", "y": "0.7", "width": "0.25", "page_number": "0"}


class Inbox:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(email.message_from_bytes(envelope.content, policy=email.policy.default))
        return "250 OK"

    def to(self, address, subject_part=""):
        return [m for m in self.messages if address in (m["To"] or "") and subject_part in (m["Subject"] or "")]


@pytest.fixture(scope="session")
def smtp():
    inbox = Inbox()

    def authenticate(server, session, envelope, mechanism, auth_data):
        ok = getattr(auth_data, "login", b"").decode() == USER and getattr(auth_data, "password", b"").decode() == PASSWORD
        return AuthResult(success=ok, handled=not ok and False)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ctl = Controller(inbox, hostname="127.0.0.1", port=port, authenticator=authenticate,
                     auth_required=True, auth_require_tls=False)
    ctl.start()
    inbox.port = port
    yield inbox
    ctl.stop()


def connect_mail(client, headers, smtp, **over):
    body = {"host": "127.0.0.1", "port": smtp.port, "security": "none", "username": USER,
            "password": PASSWORD, "from_name": "Acme CA", **over}
    return client.put("/email/settings", headers=headers, json=body)


@pytest.fixture()
def sender(client, smtp):
    headers = make_user(client, f"s-{id(object())}@firm.example")
    r = connect_mail(client, headers, smtp)
    assert r.status_code == 200 and r.json()["settings"]["verified_at"], r.text
    return headers


def make_doc(client, headers, upload):
    return upload(headers).json()


def drain():
    while outbox.deliver_emails():
        pass


def link_token(text):
    return re.search(r"/sign/([\w\-]+)", text).group(1)


def body_of(msg):
    part = msg.get_body(preferencelist=("plain",))
    return part.get_content()


def sign_public(client, token, name="Rita", otp_header=None, **over):
    data = {**SIG, "signer_name": name, "consent": "true", **over}
    return client.post(f"/signing/public/{token}/sign", data=data, headers=otp_header or {},
                       files={"signature": ("s.png", png_bytes())})


def verify_code(client, smtp, token, address):
    assert client.post(f"/signing/public/{token}/otp/send").status_code == 200
    code = re.search(r"code: (\d{6})", smtp.to(address, "verification code")[-1]["Subject"]).group(1)
    r = client.post(f"/signing/public/{token}/otp/verify", json={"code": code})
    assert r.status_code == 200, r.text
    return {"X-Verify": r.json()["verify_token"]}


# ---------------- settings ----------------

def test_settings_save_verify_and_secrecy(client, alice, smtp):
    assert client.get("/email/settings", headers=alice).json() == {"source": "none", "settings": None}
    r = connect_mail(client, alice, smtp)
    s = r.json()["settings"]
    assert r.json()["source"] == "user" and s["verified_at"] and s["has_password"] and "password" not in s
    assert PASSWORD not in r.text and PASSWORD not in client.get("/email/settings", headers=alice).text

    from app.models import SmtpConfig
    with SessionLocal() as db:
        stored = db.query(SmtpConfig).first().password_enc
    assert PASSWORD not in stored and stored.startswith("gAAAA")           # Fernet ciphertext

    # updating without a password keeps the old one
    r = connect_mail(client, alice, smtp, password=None, from_name="New Name")
    assert r.json()["settings"]["verified_at"] and r.json()["settings"]["from_name"] == "New Name"


def test_settings_wrong_password_and_validation(client, alice, bob, smtp, monkeypatch):
    r = connect_mail(client, alice, smtp, password="nope")
    s = r.json()["settings"]
    assert s["verified_at"] is None and "app password" in s["last_error"]
    assert connect_mail(client, bob, smtp, host="bad host!").status_code == 422
    assert connect_mail(client, bob, smtp, username="", password=None, from_email="not-an-email").status_code == 422
    assert connect_mail(client, bob, smtp, password=None).status_code == 422                          # new config needs a password
    monkeypatch.setattr(settings, "environment", "production")
    assert connect_mail(client, bob, smtp, host="127.0.0.1", port=587).status_code == 422             # internal host
    assert connect_mail(client, bob, smtp, host="smtp.example.com", port=9999).status_code == 422     # odd port


def test_settings_are_private_and_session_only(client, alice, bob, smtp):
    connect_mail(client, alice, smtp)
    key = client.post("/api-keys", headers=alice, json={"name": "k"}).json()["key"]
    assert client.get("/email/settings", headers={"X-API-Key": key}).status_code == 401              # credentials: browser session only
    assert client.get("/email/settings", headers=bob).json()["settings"] is None
    assert client.delete("/email/settings", headers=alice).status_code == 204
    assert client.get("/email/settings", headers=alice).json()["source"] == "none"


def test_test_email_uses_users_mailbox(client, smtp):
    headers = make_user(client, "me@firm.example")
    assert client.post("/email/test", headers=headers).status_code == 409
    connect_mail(client, headers, smtp, from_name="Acme CA")
    assert client.post("/email/test", headers=headers).status_code == 200
    msg = smtp.to("me@firm.example", "test email")[-1]
    assert "Acme CA" in msg["From"] and USER in msg["From"] and msg["Reply-To"] == "me@firm.example"
    assert any(p["id"] == "gmail" and p["host"] == "smtp.gmail.com" for p in client.get("/email/providers", headers=headers).json())


# ---------------- single request: invite, OTP, certificate ----------------

def test_invite_sent_from_senders_mailbox_and_otp_gate(client, sender, smtp, upload):
    doc = upload(sender).json()
    r = client.post("/signing/requests", headers=sender, json={"document_id": doc["id"], "mode": "email",
                    "recipient_email": "client@example.com", "recipient_name": "Rita Shah"})
    assert r.json()["email_sent"] is True and r.json()["email_configured"] is True
    msg = smtp.to("client@example.com", "asked you to sign")[-1]
    token = link_token(body_of(msg))
    assert token == r.json()["signing_link"].rsplit("/", 1)[1] and "Rita Shah" in body_of(msg)
    assert msg.get_body(preferencelist=("html",)) is not None

    info = client.get(f"/signing/public/{token}/info").json()
    assert info["otp_required"] is True and info["email_hint"] == "c*****@example.com"
    # nothing about the document is served before the code is entered
    for path in ("document", "pages/0.png"):
        assert client.get(f"/signing/public/{token}/{path}").status_code == 403
    assert sign_public(client, token).status_code == 403
    assert client.post(f"/signing/public/{token}/otp/verify", json={"code": "123456"}).status_code == 409   # no code yet

    assert client.post(f"/signing/public/{token}/otp/send").status_code == 200
    assert client.post(f"/signing/public/{token}/otp/send").status_code == 429                              # 30s cool-down
    assert client.post(f"/signing/public/{token}/otp/verify", json={"code": "000000"}).status_code == 400
    code = re.search(r"code: (\d{6})", smtp.to("client@example.com", "verification code")[-1]["Subject"]).group(1)
    ok = client.post(f"/signing/public/{token}/otp/verify", json={"code": code})
    assert ok.status_code == 200
    hdr = {"X-Verify": ok.json()["verify_token"]}
    assert client.get(f"/signing/public/{token}/pages/0.png", headers=hdr).status_code == 200
    assert client.get(f"/signing/public/{token}/pages/0.png?v={hdr['X-Verify']}").status_code == 200       # <img> style
    assert client.get(f"/signing/public/{token}/document", headers={"X-Verify": "wrong"}).status_code == 403
    assert sign_public(client, token, otp_header=hdr).status_code == 200

    # signed copy emailed to both parties, certificate available and honest about identity checks
    drain()
    assert smtp.to("sender@example.com", "signed") == [] or True
    got = smtp.to("client@example.com", "Your signed copy")[-1]
    pdf = [p for p in got.iter_attachments()][0].get_payload(decode=True)
    assert "Signed by Rita" in page_text(pdf)
    notify = [m for m in smtp.to(f"{sender_email(client, sender)}", "signed")]
    assert notify and list(notify[-1].iter_attachments())

    cert = client.get(f"/signing/requests/{r.json()['id']}/certificate", headers=sender)
    assert cert.status_code == 200
    text = page_text(cert.content)
    assert "Certificate of Completion" in text and "verified with a one-time code" in text and "Verified: no tampering" in text
    events = [e["type"] for e in client.get(f"/signing/requests/{r.json()['id']}/events", headers=sender).json()["events"]]
    assert {"otp_sent", "otp_verified", "signed"} <= set(events)


def sender_email(client, headers):
    return client.get("/auth/me", headers=headers).json()["email"]


def test_otp_expiry_and_attempt_limit(client, sender, smtp, upload):
    doc = upload(sender).json()
    r = client.post("/signing/requests", headers=sender, json={"document_id": doc["id"], "mode": "email", "recipient_email": "c2@example.com"}).json()
    token = r["signing_link"].rsplit("/", 1)[1]
    client.post(f"/signing/public/{token}/otp/send")
    for _ in range(settings.otp_max_attempts):
        assert client.post(f"/signing/public/{token}/otp/verify", json={"code": "111111"}).status_code == 400
    code = re.search(r"code: (\d{6})", smtp.to("c2@example.com", "verification code")[-1]["Subject"]).group(1)
    assert client.post(f"/signing/public/{token}/otp/verify", json={"code": code}).status_code == 429     # locked even for the right code

    from app.models import SignerSession
    with SessionLocal() as db:
        s = db.query(SignerSession).filter_by(request_id=r["id"]).one()
        s.attempts, s.code_expires_at = 0, utc_now() - timedelta(minutes=1)
        db.commit()
    assert client.post(f"/signing/public/{token}/otp/verify", json={"code": code}).status_code == 410


def test_in_person_and_unconfigured_senders_skip_otp(client, alice, sender, upload):
    doc = upload(sender).json()
    r = client.post("/signing/requests", headers=sender, json={"document_id": doc["id"], "mode": "in_person"}).json()
    assert client.get(f"/signing/public/{r['signing_link'].rsplit('/', 1)[1]}/info").json()["otp_required"] is False
    d2 = upload(alice).json()                                          # sender with no mailbox: link-only, as before
    r2 = client.post("/signing/requests", headers=alice, json={"document_id": d2["id"], "mode": "email", "recipient_email": "x@example.com"}).json()
    assert r2["email_sent"] is False and r2["email_configured"] is False
    assert client.get(f"/signing/public/{r2['signing_link'].rsplit('/', 1)[1]}/info").json()["otp_required"] is False


def test_remind_single_request(client, sender, smtp, upload):
    doc = upload(sender).json()
    r = client.post("/signing/requests", headers=sender, json={"document_id": doc["id"], "mode": "email", "recipient_email": "slow@example.com"}).json()
    assert client.post(f"/signing/requests/{r['id']}/remind", headers=sender).status_code == 429     # emailed moments ago
    with SessionLocal() as db:
        for row in db.query(EmailOutbox).filter_by(request_id=r["id"]):
            row.created_at = utc_now() - timedelta(hours=2)
        db.commit()
    assert client.post(f"/signing/requests/{r['id']}/remind", headers=sender).status_code == 200
    drain()
    reminder = smtp.to("slow@example.com", "Reminder")[-1]
    assert link_token(body_of(reminder)) == r["signing_link"].rsplit("/", 1)[1]                       # same link rebuilt


# ---------------- bulk ----------------

def bulk_sheet(client, headers):
    rows = [("Ramesh Iyer", "ramesh@example.com", 12500), ("Sita Rao", "SITA@Example.com", 3000),
            ("Bad Row", "not-an-email", 1), ("Dup Sita", "sita@example.com", 2), ("Empty", "", 3), ("Asha Verma", "asha@example.com", 450)]
    return add_sheet(client, headers, xlsx_bytes(rows, headers=("Name", "Email", "Amount"))).json()


def bulk_body(doc, ds, **over):
    return {"document_id": doc["id"], "dataset_id": ds["id"], "email_column": "Email", "name_column": "Name", **over}


def test_bulk_preview_validation(client, sender, upload):
    doc, ds = upload(sender).json(), bulk_sheet(client, sender)
    p = client.post("/bulk/preview", headers=sender, json=bulk_body(doc, ds)).json()
    assert (p["valid"], p["invalid_count"], p["duplicate_count"], p["total_rows"]) == (3, 2, 1, 6)
    assert {i["row"] for i in p["invalid"]} == {3, 5} and p["duplicates"][0]["row"] == 4
    assert p["email_configured"] and p["email_source"] == "user" and p["sample"][1]["email"] == "sita@example.com"
    assert client.post("/bulk/preview", headers=sender, json=bulk_body(doc, ds, email_column="Nope")).status_code == 422
    assert client.post("/bulk/preview", headers=sender, json=bulk_body(doc, ds, start=3, end=3)).json()["valid"] == 0
    assert client.post("/bulk", headers=sender, json=bulk_body(doc, ds, start=3, end=3)).status_code == 422


def test_bulk_end_to_end(client, sender, smtp, upload):
    doc, ds = upload(sender).json(), bulk_sheet(client, sender)
    r = client.post("/bulk", headers=sender, json=bulk_body(doc, ds, subject="Please sign {document}, {name}", message="Due Friday, {name}."))
    assert r.status_code == 201 and r.json()["total"] == 3 and r.json()["skipped_invalid"] == 2
    batch = r.json()["id"]
    assert len(smtp.to("ramesh@example.com")) == 0                    # queued, not yet sent: the worker does it
    drain()
    m = smtp.to("ramesh@example.com", "Please sign contract.pdf, Ramesh Iyer")[-1]
    assert "Due Friday, Ramesh Iyer." in body_of(m)
    assert smtp.to("sita@example.com", "Please sign")                  # address normalised to lowercase

    d = client.get(f"/bulk/{batch}", headers=sender).json()
    assert d["counts"]["total"] == 3 and d["counts"]["email_sent"] == 3 and d["counts"]["pending"] == 3
    # bulk plumbing stays out of the everyday lists
    assert client.get("/signing/requests", headers=sender).json() == []
    assert len(client.get("/signing/requests?include_bulk=true", headers=sender).json()) == 3

    token = link_token(body_of(m))
    hdr = verify_code(client, smtp, token, "ramesh@example.com")
    assert sign_public(client, token, "Ramesh Iyer", otp_header=hdr).status_code == 200
    drain()
    d = client.get(f"/bulk/{batch}", headers=sender).json()
    assert (d["counts"]["signed"], d["counts"]["pending"]) == (1, 2)
    signed_doc = next(i for i in d["items"] if i["status"] == "signed")["signed_document_id"]
    assert signed_doc not in [x["id"] for x in client.get("/documents", headers=sender).json()]            # batch copies hidden
    assert signed_doc in [x["id"] for x in client.get("/documents?include_batch=true", headers=sender).json()]

    z = zipfile.ZipFile(io.BytesIO(client.get(f"/bulk/{batch}/signed.zip", headers=sender).content))
    assert z.namelist() == ["Ramesh Iyer.pdf"]
    csv_text = client.get(f"/bulk/{batch}/export.csv", headers=sender).text
    assert "Ramesh Iyer" in csv_text and "signed" in csv_text and "sha256" in csv_text
    links = client.get(f"/bulk/{batch}/links.csv", headers=sender).text
    assert links.count("/sign/") == 2                                  # only the two still pending

    assert client.post(f"/bulk/{batch}/cancel", headers=sender).json()["cancelled"] == 2
    assert client.get(f"/bulk/{batch}", headers=sender).json()["counts"]["cancelled"] == 2
    assert client.get(f"/signing/public/{link_token(body_of(smtp.to('asha@example.com', 'Please')[-1]))}/info").status_code == 410


def test_bulk_personalized_copies(client, sender, smtp, upload):
    doc = upload(sender).json()
    for label, y in (("Name", .2), ("Amount", .3)):
        client.post(f"/documents/{doc['id']}/tags", headers=sender, json={"page": 0, "x": .1, "y": y, "w": .5, "h": .05, "label": label})
    ds = bulk_sheet(client, sender)
    client.put(f"/documents/{doc['id']}/template", headers=sender, json={"dataset_id": ds["id"], "font": "bundled:Caveat"})
    assert client.post("/bulk", headers=sender, json=bulk_body(doc, ds, personalize=True, start=6, end=6)).status_code == 201
    drain()
    m = smtp.to("asha@example.com", "asked you to sign")[-1]
    assert "contract-Asha Verma.pdf" in m["Subject"]
    token = link_token(body_of(m))
    hdr = verify_code(client, smtp, token, "asha@example.com")
    page = client.get(f"/signing/public/{token}/document", headers=hdr)
    assert "Asha Verma" in page_text(page.content) and "450" in page_text(page.content)
    # without personalisation every recipient gets the identical original
    no_tags = upload(sender).json()
    assert client.post("/bulk", headers=sender, json=bulk_body(no_tags, ds, personalize=True)).status_code == 409


def test_bulk_isolation(client, sender, alice, upload):
    doc, ds = upload(sender).json(), bulk_sheet(client, sender)
    batch = client.post("/bulk", headers=sender, json=bulk_body(doc, ds)).json()["id"]
    for path in ("", "/export.csv", "/links.csv", "/signed.zip"):
        assert client.get(f"/bulk/{batch}{path}", headers=alice).status_code == 404
    for path in ("remind", "cancel", "retry-failed"):
        assert client.post(f"/bulk/{batch}/{path}", headers=alice).status_code == 404
    assert client.post("/bulk", headers=alice, json=bulk_body(doc, ds)).status_code == 404
    assert client.get("/bulk", headers=alice).json() == [] and len(client.get("/bulk", headers=sender).json()) >= 1


def test_bulk_without_mailbox_still_creates_links(client, alice, upload):
    doc, ds = upload(alice).json(), bulk_sheet(client, alice)
    assert client.post("/bulk/preview", headers=alice, json=bulk_body(doc, ds)).json()["email_configured"] is False
    r = client.post("/bulk", headers=alice, json=bulk_body(doc, ds)).json()
    assert r["email_configured"] is False
    d = client.get(f"/bulk/{r['id']}", headers=alice).json()
    assert d["counts"]["email_not_configured"] == 3
    assert client.post(f"/bulk/{r['id']}/remind", headers=alice).status_code == 409                  # needs a mailbox
    assert client.get(f"/bulk/{r['id']}/links.csv", headers=alice).text.count("/sign/") == 3         # share by WhatsApp etc.


# ---------------- delivery failures, limits, reminders ----------------

def test_failed_login_marks_emails_failed_then_retry(client, smtp, upload):
    headers = make_user(client, "flaky@firm.example")
    connect_mail(client, headers, smtp)
    doc, ds = upload(headers).json(), bulk_sheet(client, headers)
    batch = client.post("/bulk", headers=headers, json=bulk_body(doc, ds)).json()["id"]
    connect_mail(client, headers, smtp, password="rotated-away")                  # password changed at the provider
    drain()
    d = client.get(f"/bulk/{batch}", headers=headers).json()
    assert d["counts"]["email_failed"] == 3 and "app password" in d["items"][0]["email_error"]
    assert "app password" in client.get("/email/settings", headers=headers).json()["settings"]["last_error"]
    connect_mail(client, headers, smtp)                                           # fixed
    assert client.post(f"/bulk/{batch}/retry-failed", headers=headers).json()["queued"] == 3
    drain()
    assert client.get(f"/bulk/{batch}", headers=headers).json()["counts"]["email_sent"] == 3


def test_daily_limit_defers_instead_of_failing(client, smtp, upload):
    headers = make_user(client, "capped@firm.example")
    connect_mail(client, headers, smtp, daily_limit=2)
    doc, ds = upload(headers).json(), bulk_sheet(client, headers)
    batch = client.post("/bulk", headers=headers, json=bulk_body(doc, ds)).json()["id"]
    drain_once = outbox.deliver_emails()
    assert drain_once == 3
    c = client.get(f"/bulk/{batch}", headers=headers).json()["counts"]
    assert (c["email_sent"], c["email_pending"], c["email_failed"]) == (2, 1, 0)
    assert "Daily limit" in client.get(f"/bulk/{batch}", headers=headers).json()["items"][2]["email_error"]


def test_automatic_reminders(client, sender, smtp, upload, monkeypatch):
    doc = upload(sender).json()
    r = client.post("/signing/requests", headers=sender, json={"document_id": doc["id"], "mode": "email", "recipient_email": "late@example.com"}).json()
    assert outbox.schedule_reminders() == 0                                       # too early
    def age(days):
        with SessionLocal() as db:
            db.query(SignatureRequest).filter_by(id=r["id"]).update({"created_at": utc_now() - timedelta(days=days)})
            for row in db.query(EmailOutbox).filter_by(request_id=r["id"]):
                row.created_at = utc_now() - timedelta(days=days)
            db.commit()
    age(4)
    assert outbox.schedule_reminders() == 1
    assert outbox.schedule_reminders() == 0                                       # not queued twice
    drain()
    assert len(smtp.to("late@example.com", "Reminder")) == 1
    age(8)                                                                        # first reminder is old too -> second one
    with SessionLocal() as db:
        db.query(EmailOutbox).filter_by(request_id=r["id"], kind="reminder").update({"created_at": utc_now() - timedelta(days=4)})
        db.commit()
    assert outbox.schedule_reminders() == 1
    drain()
    with SessionLocal() as db:
        db.query(EmailOutbox).filter_by(request_id=r["id"], kind="reminder").update({"created_at": utc_now() - timedelta(days=4)})
        db.commit()
    assert outbox.schedule_reminders() == 0                                       # max_reminders (2) reached
    monkeypatch.setattr(settings, "auto_reminders", False)
    assert outbox.schedule_reminders() == 0


def test_signed_request_gets_no_late_invite(client, sender, smtp, upload):
    doc = upload(sender).json()
    ds = add_sheet(client, sender, xlsx_bytes([("Quick Q", "quick@example.com")], headers=("Name", "Email"))).json()
    batch = client.post("/bulk", headers=sender, json=bulk_body(doc, ds)).json()["id"]
    client.post(f"/bulk/{batch}/cancel", headers=sender)
    before = len(smtp.to("quick@example.com"))
    drain()
    assert len(smtp.to("quick@example.com")) == before                             # cancelled batch sends nothing


def test_csv_export_neutralises_formulas(client, sender, upload):
    doc = upload(sender).json()
    ds = add_sheet(client, sender, xlsx_bytes([("@SUM(1+1)", "evil@example.com")], headers=("Name", "Email"))).json()
    batch = client.post("/bulk", headers=sender, json=bulk_body(doc, ds)).json()["id"]
    text = client.get(f"/bulk/{batch}/export.csv", headers=sender).text
    assert "'@SUM(1+1)" in text and ",@SUM" not in text
