import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="documud-test-")
os.environ.update(
    SECRET_KEY="test-secret",
    DATABASE_URL=f"sqlite:///{_tmp}/test.db",
    STORAGE_ROOT=f"{_tmp}/storage",
    ALLOW_REGISTRATION="true",
    SMTP_HOST="",
    AUTH_RATE_LIMIT_PER_MINUTE="100000",
    RATE_LIMIT_PER_MINUTE="100000",
    PUBLIC_RATE_LIMIT_PER_MINUTE="100000",
)

import io  # noqa: E402

import pymupdf  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def make_user(client, email, password="a-long-password-1"):
    r = client.post("/auth/register", json={"email": email, "name": "Test", "password": password})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def alice(client):
    import uuid
    return make_user(client, f"alice-{uuid.uuid4().hex[:8]}@example.com")


@pytest.fixture()
def bob(client):
    import uuid
    return make_user(client, f"bob-{uuid.uuid4().hex[:8]}@example.com")


def pdf_bytes(pages=2):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {i + 1} agreement text")
    return doc.tobytes()


def png_bytes(w=600, h=200, fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGBA" if fmt == "PNG" else "RGB", (w, h), (0, 0, 0, 255)).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture()
def upload(client):
    def _upload(headers, data=None, name="contract.pdf"):
        return client.post("/documents", headers=headers, files={"file": (name, pdf_bytes() if data is None else data)})
    return _upload
