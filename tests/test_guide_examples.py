"""The guide's quickstart, executed: documented examples must keep working."""
import json
import re
from pathlib import Path

import pymupdf

from tests.conftest import png_bytes

GUIDE = (Path(__file__).parent.parent / "guide" / "quickstart.md").read_text()


def _definition() -> dict:
    block = re.search(r"-F 'definition=(\{.*?\n  \})'", GUIDE, re.S).group(1)
    return json.loads(block)


def _create_body() -> dict:
    block = re.search(r"-d '(\{\s*\"template_id\".*?\n  \})'", GUIDE, re.S).group(1)
    return json.loads(block.replace('"\'$TPL\'"', '"__TPL__"'))


def _pdf() -> bytes:
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    for i, label in enumerate(["Client name:", "PAN:", "Fee:", "I accept the terms", "For the firm:", "Client signature:", "Date:"]):
        p.insert_text((90 if label.startswith("I accept") else 72, 140 + i * 60), label, fontsize=12)
    return d.tobytes()


def test_quickstart_end_to_end(client, alice):
    r = client.post("/v1/templates/upload", headers=alice, files={"file": ("engagement-letter.pdf", _pdf())},
                    data={"definition": json.dumps(_definition())})
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert len(tpl["fields"]) == 7
    assert client.put("/v1/signature-profile", headers=alice, data={"typed_name": "Priya Menon", "font": "GreatVibes"}).status_code == 200
    assert client.get(f"/v1/templates/{tpl['id']}/preview.png?page=1", headers=alice).status_code == 200

    body = json.loads(json.dumps(_create_body()).replace("__TPL__", tpl["id"]).replace("$TPL", tpl["id"]))
    body["template_id"] = tpl["id"]
    env = client.post("/v1/envelopes", headers={**alice, "Idempotency-Key": "order-10432"}, json=body)
    assert env.status_code == 201, env.text
    env = env.json()
    assert env["status"] == "in_progress" and env["external_id"] == "CASE-10432"
    roles = {x["role"]: x for x in env["recipients"]}
    assert roles["office"]["status"] == "signed" and roles["office"]["signing_url"] is None
    assert roles["client"]["status"] == "pending" and "/e/" in roles["client"]["signing_url"]

    # the documented aliasing really filled the fields from the nested data
    vals = {f["name"]: f["value"] for f in client.get(f"/v1/envelopes/{env['id']}", headers=alice).json()["fields"]}
    assert vals["client_name"] == "Rita Shah" and vals["pan"] == "ABCDE1234F" and vals["fee"] == "125000"

    # the client finishes; the documented lookups work
    token = roles["client"]["signing_url"].rsplit("/", 1)[1]
    done = client.post(f"/v1/sign/{token}/submit", data={"values": json.dumps({"accepted": True}), "consent": "true"},
                       files={"signature": ("s.png", png_bytes())})
    assert done.json()["completed"] is True
    found = client.get("/v1/envelopes", headers=alice, params={"external_id": "CASE-10432"}).json()["data"]
    assert [e["id"] for e in found] == [env["id"]] and found[0]["status"] == "completed"
    with pymupdf.open(stream=client.get(f"/v1/envelopes/{env['id']}/document", headers=alice).content) as pdf:
        text = pdf[0].get_text()
        assert "Rita Shah" in text and "ABCDE1234F" in text and "1,25,000" in text
    assert client.get(f"/v1/envelopes/{env['id']}/certificate", headers=alice).status_code == 200
    replay = client.post("/v1/envelopes", headers={**alice, "Idempotency-Key": "order-10432"}, json=body)
    assert replay.status_code == 200 and replay.json()["id"] == env["id"]


def test_embedded_iframe_allow_list(client, monkeypatch):
    from app.config import settings
    assert client.get("/e/abc").headers["X-Frame-Options"] == "DENY"
    monkeypatch.setattr(settings, "embed_allowed_origins", "https://app.example.com")
    r = client.get("/e/abc")
    assert r.headers["Content-Security-Policy"] == "frame-ancestors https://app.example.com" and "X-Frame-Options" not in r.headers
    assert client.get("/dashboard").headers["X-Frame-Options"] == "DENY"        # only signing pages may be framed
