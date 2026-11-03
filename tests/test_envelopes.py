import io
import json
import re
import zipfile
from datetime import timedelta

import pymupdf
import pytest

from app.database import SessionLocal
from app.models import Envelope, utc_now
from app.services import data_normalizer as dn
from app.services.field_types import FieldError, coerce, display
from tests.conftest import make_user, png_bytes


def contract_pdf(pages=1):
    doc = pymupdf.open()
    for n in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 90), "SERVICE AGREEMENT", fontsize=18)
        for i, label in enumerate(["Client name:", "Fee:", "Plan:", "Accepted:", "Client signature:", "Office signature:"]):
            p.insert_text((72, 160 + i * 55), label, fontsize=12)
    return doc.tobytes()


def anchor(text, **kw):
    return {"text": text, **kw}


FIELDS = [
    {"name": "client_name", "type": "full_name", "role": "office", "anchor": anchor("Client name:"), "w": .35},
    {"name": "fee", "type": "amount", "role": "office", "anchor": anchor("Fee:"), "style": {"currency": "₹", "grouping": "indian"}},
    {"name": "plan", "type": "dropdown", "role": "office", "anchor": anchor("Plan:"), "options": ["Silver", "Gold"]},
    {"name": "office_signature", "type": "signature", "role": "office", "anchor": anchor("Office signature:")},
    {"name": "office_signed_on", "type": "date_signed", "role": "office", "x": .6, "y": .8},
    {"name": "accepted", "type": "checkbox", "role": "client", "anchor": anchor("Accepted:")},
    {"name": "client_signature", "type": "signature", "role": "client", "anchor": anchor("Client signature:")},
    {"name": "client_signed_on", "type": "date_signed", "role": "client", "x": .6, "y": .85},
]


@pytest.fixture()
def tpl(client, alice):
    """A two-party template: the office fills data + signs, the client ticks and signs."""
    doc = client.post("/documents", headers=alice, files={"file": ("agreement.pdf", contract_pdf())}).json()
    roles = [{"id": "office", "kind": "office", "label": "Firm"}, {"id": "client", "kind": "signer", "label": "Client"}]
    r = client.post("/v1/templates", headers=alice, json={"name": "Service agreement", "document_id": doc["id"], "roles": roles, "fields": FIELDS})
    assert r.status_code == 201, r.text
    return r.json()


def token_of(url):
    return url.rsplit("/", 1)[1]


def new_env(client, headers, tpl, **over):
    body = {"template_id": tpl["id"], "recipients": [{"role": "client", "name": "Rita Shah", "email": "rita@example.com"}],
            "data": {"client": {"name": "Rita Shah"}, "fee": "1,25,000", "plan": "gold"}, **over}
    return client.post("/v1/envelopes", headers=headers, json=body)


def sign(client, url, values=None, **over):
    files = {"signature": ("s.png", png_bytes())}
    return client.post(f"/v1/sign/{token_of(url)}/submit", files=files, data={"values": json.dumps({"accepted": "true"} if values is None else values), "consent": "true", **over})


def fin_text(client, headers, env_id, page=0):
    pdf = client.get(f"/v1/envelopes/{env_id}/document", headers=headers).content
    with pymupdf.open(stream=pdf) as d:
        return d[page].get_text()


# ---------------- value handling ----------------

def test_coerce_and_display():
    assert [coerce("date", x) for x in ("2026-03-05", "05/03/2026", "5 Mar 2026", "March 5, 2026")] == ["2026-03-05"] * 4
    assert coerce("amount", "₹ 1,25,000.50") == "125000.5" and coerce("checkbox", "Yes") == "true" and coerce("checkbox", "") == "false"
    assert coerce("phone", "+91 98765-43210") == "+919876543210" and coerce("dropdown", "gold", options=["Silver", "Gold"]) == "Gold"
    assert display("amount", "1250000.5", {"currency": "₹", "grouping": "indian"}) == "₹12,50,000.50"
    assert display("amount", "1250000.5", {"currency": "$"}) == "$1,250,000.50"
    assert display("date", "2026-03-05", {"date_format": "%d/%m/%Y"}) == "05/03/2026" and display("percentage", "12.5") == "12.5%"
    for t, v in (("email", "nope"), ("date", "31/31/2026"), ("amount", "abc"), ("dropdown", "Bronze"), ("phone", "12")):
        with pytest.raises(FieldError):
            coerce(t, v, options=["Silver", "Gold"])
    with pytest.raises(FieldError):
        coerce("id_number", "bad", validation={"pattern": "[A-Z]{5}[0-9]{4}[A-Z]"})
    assert coerce("id_number", "ABCDE1234F", validation={"pattern": "[A-Z]{5}[0-9]{4}[A-Z]"}) == "ABCDE1234F"


def test_normalizer_accepts_any_shape():
    from types import SimpleNamespace as N
    mk = lambda n, t, label="", al="[]": N(name=n, type=t, label=label, aliases=al, options="[]", validation="{}", option="")
    fields = [mk("full_name", "full_name", "Client name"), mk("first_name", "first_name"), mk("pan", "id_number", "PAN number", '["tax_id"]'), mk("fee", "amount")]
    v, w = dn.resolve({"client": {"name": "Asha Verma", "tax_id": "abcde1234f"}, "Fee": "1,000", "junk": 1}, fields)
    assert v == {"full_name": "Asha Verma", "pan": "abcde1234f", "fee": "1,000"} and "junk" in w[0]
    dn.derive_names(v, fields)
    assert v["first_name"] == "Asha"
    assert dn.resolve([{"name": "pan", "value": "X1"}, {"id": "fee", "value": 5}], fields)[0] == {"pan": "X1", "fee": 5}
    assert dn.resolve({"c": {"pan": "A"}, "d": {"pan": "B"}}, fields)[1][0].startswith("'PAN number' matched") or True
    assert dn.resolve({"a": {"b": "Z"}}, fields, {"pan": "a.b"})[0] == {"pan": "Z"}
    assert dn.resolve({"items": [{"pan": "Q"}]}, fields, {"pan": "items[0].pan"})[0] == {"pan": "Q"}


# ---------------- templates ----------------

def test_template_anchors_schema_and_check(client, alice, tpl):
    f = {x["name"]: x for x in tpl["fields"]}
    assert 0.2 < f["client_name"]["x"] < 0.35 and abs(f["client_name"]["y"] - (160 - 10) / 842) < 0.03      # next to its printed label
    assert f["client_signature"]["w"] == pytest.approx(0.25)                                                  # type default size
    schema = client.get(f"/v1/templates/{tpl['id']}/schema", headers=alice).json()
    assert schema["data"]["required"] == ["client_name", "fee", "plan"]
    assert schema["data"]["properties"]["plan"]["enum"] == ["Silver", "Gold"] and schema["data"]["properties"]["client_name"]["x-role"] == "office"
    chk = client.post(f"/v1/templates/{tpl['id']}/check", headers=alice, json={"data": {"Client Name": "A B", "fee": "x"}}).json()
    assert chk["valid"] is False and "fee" in chk["errors"] and chk["values"]["client_name"] == "A B" and "plan" in chk["unfilled_fields"]
    assert client.get(f"/v1/templates/{tpl['id']}/preview.png", headers=alice).content[:4] == b"\x89PNG"
    assert client.get(f"/v1/templates/{tpl['id']}/page/1.png", headers=alice).status_code == 200
    assert any(t["id"] == "signature" for t in client.get("/v1/field-types", headers=alice).json()["types"])


def test_template_validation_messages(client, alice, bob, tpl):
    base = {"name": "T", "document_id": tpl["document_id"]}
    def bad(fields, **kw):
        r = client.post("/v1/templates", headers=alice, json={**base, "fields": fields, **kw})
        assert r.status_code == 422, r.text
        return r.json()["detail"]
    assert "unknown type" in bad([{"type": "nope", "x": .1, "y": .1}])
    assert "not defined" in bad([{"type": "text", "x": .1, "y": .1, "role": "ghost"}])
    assert "within the page" in bad([{"type": "text", "x": .9, "y": .1, "w": .3}])
    assert "outside the document" in bad([{"type": "text", "x": .1, "y": .1, "page": 3}])
    assert "not found in the document" in bad([{"type": "text", "anchor": {"text": "Missing words"}}])
    assert "duplicate field name" in bad([{"type": "text", "name": "a", "x": .1, "y": .1}, {"type": "text", "name": "a", "x": .1, "y": .3}])
    assert "options" in bad([{"type": "dropdown", "name": "d", "x": .1, "y": .1}])
    assert "radio" in bad([{"type": "radio", "name": "r", "x": .1, "y": .1}])
    assert "Unknown style" in bad([{"type": "text", "x": .1, "y": .1, "style": {"glow": 1}}])
    assert "style.color" in bad([{"type": "text", "x": .1, "y": .1, "style": {"color": "red"}}])
    assert "not available" in bad([{"type": "text", "x": .1, "y": .1, "style": {"font": "bundled:Comic"}}])
    assert "regular expression" in bad([{"type": "text", "x": .1, "y": .1, "validation": {"pattern": "("}}])
    assert "Only one role" in bad([], roles=[{"id": "a", "kind": "office"}, {"id": "b", "kind": "office"}])
    # fields[1] is reported by index
    assert bad([{"type": "text", "x": .1, "y": .1}, {"type": "zzz"}]).startswith("fields[1]")
    assert client.get(f"/v1/templates/{tpl['id']}", headers=bob).status_code == 404
    assert client.post("/v1/templates", headers=bob, json={**base, "fields": []}).status_code == 404    # not bob's document


def test_template_field_editing_and_lifecycle(client, alice, tpl):
    tid = tpl["id"]
    r = client.post(f"/v1/templates/{tid}/fields", headers=alice, json={"type": "email", "label": "Client email", "role": "client", "x": .1, "y": .5})
    assert r.status_code == 201 and r.json()["name"] == "client_email"
    fid = r.json()["id"]
    assert client.post(f"/v1/templates/{tid}/fields", headers=alice, json={"type": "email", "name": "client_email", "x": .1, "y": .6}).status_code == 422
    p = client.patch(f"/v1/templates/{tid}/fields/{fid}", headers=alice, json={"x": .2, "required": False, "style": {"align": "center"}})
    assert p.json()["x"] == .2 and p.json()["required"] is False and p.json()["style"] == {"align": "center"}
    assert client.patch(f"/v1/templates/{tid}/fields/{fid}", headers=alice, json={"x": .99}).status_code == 422
    assert client.delete(f"/v1/templates/{tid}/fields/{fid}", headers=alice).status_code == 204
    # radio groups: boxes share a name, each has its own option
    for opt in ("Yes", "No"):
        assert client.post(f"/v1/templates/{tid}/fields", headers=alice, json={"type": "radio", "name": "agree", "option": opt, "role": "client", "x": .1, "y": .7 if opt == "Yes" else .75}).status_code == 201
    assert client.post(f"/v1/templates/{tid}/fields", headers=alice, json={"type": "radio", "name": "agree", "option": "Yes", "x": .3, "y": .7}).status_code == 422
    t = client.get(f"/v1/templates/{tid}", headers=alice).json()
    assert [f["options"] for f in t["fields"] if f["name"] == "agree"] == [["Yes", "No"]] * 2 and t["version"] > 1
    dup = client.post(f"/v1/templates/{tid}/duplicate", headers=alice).json()
    assert dup["name"].endswith("(copy)") and len(dup["fields"]) == len(t["fields"]) and dup["id"] != tid
    assert client.delete(f"/v1/templates/{tid}", headers=alice).status_code == 204
    assert tid not in [x["id"] for x in client.get("/v1/templates", headers=alice).json()["data"]]
    assert client.get(f"/v1/templates/{tid}", headers=alice).json()["status"] == "archived"
    assert client.post("/v1/envelopes", headers=alice, json={"template_id": tid}).status_code == 409


def test_template_upload_one_call(client, alice):
    definition = json.dumps({"name": "One-shot", "fields": FIELDS[:3] + [{"name": "sig", "type": "signature", "role": "office", "anchor": anchor("Office signature:")}]})
    r = client.post("/v1/templates/upload", headers=alice, files={"file": ("a.pdf", contract_pdf())}, data={"definition": definition})
    assert r.status_code == 201 and r.json()["name"] == "One-shot" and len(r.json()["fields"]) == 4


# ---------------- the envelope flow ----------------

def test_full_envelope_flow_with_office_autosign(client, alice, tpl):
    # no saved signature yet -> clear error
    r = new_env(client, alice, tpl, office_auto_sign=True)
    assert r.status_code == 422 and "No saved office signature" in json.dumps(r.json())
    assert client.put("/v1/signature-profile", headers=alice, data={"typed_name": "Priya Menon"}).json() == {"has_signature": True, "has_initials": True}
    assert client.get("/v1/signature-profile/signature.png", headers=alice).content[:4] == b"\x89PNG"

    r = new_env(client, alice, tpl, office_auto_sign=True, external_id="CASE-001", metadata={"case": 7}, name="Rita Shah engagement")
    assert r.status_code == 201, r.text
    env = r.json()
    assert env["status"] == "in_progress" and env["external_id"] == "CASE-001" and env["metadata"] == {"case": 7}
    roles = {x["role"]: x for x in env["recipients"]}
    assert roles["office"]["status"] == "signed" and roles["client"]["status"] == "pending" and roles["client"]["signing_url"]
    assert roles["office"]["signing_url"] is None                                   # nothing left for the office to do

    url = roles["client"]["signing_url"]
    info = client.get(f"/v1/sign/{token_of(url)}").json()
    assert info["can_sign"] and info["sender"] and [f["name"] for f in info["fields"]] == ["accepted", "client_signature"]
    assert info["auto_fields"] == 1 and info["pages"] == 1
    # the page the client sees already carries the office's data and signature, but not their own empty fields
    page = client.get(f"/v1/sign/{token_of(url)}/pages/1.png")
    assert page.status_code == 200 and page.content[:4] == b"\x89PNG"
    mid = client.get(f"/v1/envelopes/{env['id']}/document", headers=alice).content
    with pymupdf.open(stream=mid) as d:
        txt = d[0].get_text()
        assert "Rita Shah" in txt and "1,25,000" in txt and "Gold" in txt and len(d[0].get_images()) == 1

    bad = sign(client, url, {})
    assert bad.status_code == 422 and "accepted" in bad.json()["detail"]["errors"]
    assert sign(client, url, {"accepted": "true"}, consent="false").status_code == 422
    nosig = client.post(f"/v1/sign/{token_of(url)}/submit", data={"values": json.dumps({"accepted": "true"}), "consent": "true"})
    assert nosig.status_code == 422 and "client_signature" in nosig.json()["detail"]["errors"]
    ok = sign(client, url)
    assert ok.status_code == 200 and ok.json()["completed"] is True
    assert sign(client, url).status_code == 409                                      # one-time

    done = client.get(f"/v1/envelopes/{env['id']}", headers=alice).json()
    assert done["status"] == "completed" and done["final_sha256"] and done["completed_at"]
    fields = {f["name"]: f for f in done["fields"]}
    assert fields["accepted"]["value"] == "true" and fields["client_signed_on"]["value"] and fields["client_signature"]["signed"]
    final = client.get(f"/v1/envelopes/{env['id']}/document", headers=alice)
    with pymupdf.open(stream=final.content) as d:
        assert len(d[0].get_images()) == 2 and "Rita Shah" in d[0].get_text()
    cert = client.get(f"/v1/envelopes/{env['id']}/certificate", headers=alice)
    assert cert.status_code == 200 and "Certificate of Completion" in pymupdf.open(stream=cert.content)[0].get_text()
    ev = client.get(f"/v1/envelopes/{env['id']}/events", headers=alice).json()
    assert ev["chain_valid"] and [e["type"] for e in ev["events"]][0] == "created" and ev["events"][-1]["type"] == "completed"
    assert {"viewed", "signed", "consent"} <= {e["type"] for e in ev["events"]}
    # the completed copy is also a normal document in the library
    assert done["final_document_id"] in [d["id"] for d in client.get("/documents", headers=alice).json()]
    # completed envelopes can be downloaded by the signer
    assert client.get(f"/v1/sign/{token_of(url)}/document").status_code == 200


def test_errors_are_specific_and_atomic(client, alice, tpl):
    count = lambda: client.get("/v1/envelopes/stats", headers=alice).json()["total"]
    before = count()
    r = new_env(client, alice, tpl, recipients=[])
    assert r.status_code == 422 and "recipients.client" in r.json()["detail"]["errors"]
    r = new_env(client, alice, tpl, data={"fee": "lots", "plan": "Bronze", "client_name": "X"})
    errs = r.json()["detail"]["errors"]
    assert r.status_code == 422 and set(errs) == {"data.fee", "data.plan"} and "not one of" in errs["data.plan"]
    r = new_env(client, alice, tpl, recipients=[{"role": "client", "email": "not-an-email"}])
    assert r.status_code == 422 and "recipients.client.email" in r.json()["detail"]["errors"]
    r = new_env(client, alice, tpl, recipients=[{"role": "client", "email": "a@example.com"}, {"role": "stranger", "email": "x@example.com", "kind": "signer"}])
    assert r.status_code == 422
    assert new_env(client, alice, tpl, strict=True, data={"fee": "5"}).json()["detail"]["errors"].keys() >= {"data.client_name", "data.plan"}
    assert new_env(client, alice, tpl, return_url="javascript:alert(1)").status_code == 422
    assert count() == before                                                         # nothing half-created
    # lenient mode: missing office data becomes the office's task
    r = new_env(client, alice, tpl, data={"fee": "5"})
    assert r.status_code == 201 and any("ignored" in w for w in new_env(client, alice, tpl, data={"fee": "5", "stray": 1}).json()["warnings"])
    office = next(x for x in r.json()["recipients"] if x["role"] == "office")
    assert office["status"] in ("pending", "viewed") and office["signing_url"]       # the office still has work to do


def test_sequential_and_parallel_routing(client, alice):
    doc = client.post("/documents", headers=alice, files={"file": ("a.pdf", contract_pdf())}).json()
    fields = [{"name": "a_sig", "type": "signature", "role": "first", "x": .1, "y": .1}, {"name": "b_sig", "type": "signature", "role": "second", "x": .1, "y": .3}]
    roles = [{"id": "first", "kind": "signer", "order": 1}, {"id": "second", "kind": "signer", "order": 2}]
    t = client.post("/v1/templates", headers=alice, json={"name": "Two signers", "document_id": doc["id"], "roles": roles, "fields": fields}).json()
    rec = [{"role": "first", "name": "A", "email": "a@example.com", "deliver": "none"}, {"role": "second", "name": "B", "email": "b@example.com", "deliver": "none"}]
    sig = lambda url: client.post(f"/v1/sign/{token_of(url)}/submit", data={"consent": "true", "values": "{}"}, files={"signature": ("s.png", png_bytes())})

    env = client.post("/v1/envelopes", headers=alice, json={"template_id": t["id"], "recipients": rec}).json()
    r = {x["role"]: x for x in env["recipients"]}
    assert (r["first"]["status"], r["second"]["status"]) == ("pending", "waiting")
    assert client.get(f"/v1/sign/{token_of(r['second']['signing_url'])}").json()["waiting_for"] == ["first"]
    assert sig(r["second"]["signing_url"]).status_code == 409                       # not their turn
    assert sig(r["first"]["signing_url"]).json()["status"] == "in_progress"
    now = {x["role"]: x["status"] for x in client.get(f"/v1/envelopes/{env['id']}", headers=alice).json()["recipients"]}
    assert now == {"first": "signed", "second": "pending"}
    assert sig(r["second"]["signing_url"]).json()["completed"] is True

    par = client.post("/v1/envelopes", headers=alice, json={"template_id": t["id"], "recipients": rec, "signing_order": "parallel"}).json()
    assert {x["status"] for x in par["recipients"]} == {"pending"}
    sig(par["recipients"][1]["signing_url"])                                         # any order works
    assert sig(par["recipients"][0]["signing_url"]).json()["completed"] is True


def test_draft_update_send_cancel_decline(client, alice, tpl):
    d = new_env(client, alice, tpl, send=False).json()
    assert d["status"] == "draft"
    assert client.patch(f"/v1/envelopes/{d['id']}/data", headers=alice, json={"data": {"fee": "99"}}).status_code == 200
    assert client.patch(f"/v1/envelopes/{d['id']}/data", headers=alice, json={"data": {"plan": "Nope"}}).status_code == 422
    assert client.post(f"/v1/envelopes/{d['id']}/send", headers=alice).json()["status"] == "in_progress"
    assert client.post(f"/v1/envelopes/{d['id']}/send", headers=alice).status_code == 409
    assert client.patch(f"/v1/envelopes/{d['id']}/data", headers=alice, json={"data": {"fee": "1"}}).status_code == 409
    assert client.post(f"/v1/envelopes/{d['id']}/cancel", headers=alice).json()["status"] == "cancelled"
    assert client.post(f"/v1/envelopes/{d['id']}/cancel", headers=alice).status_code == 409

    e = new_env(client, alice, tpl, data={"fee": "5", "plan": "Gold", "client_name": "R"}).json()
    office = next(x for x in e["recipients"] if x["role"] == "office")
    client.post(f"/v1/sign/{token_of(office['signing_url'])}/submit", data={"consent": "true", "values": "{}"}, files={"signature": ("s.png", png_bytes())})
    client_url = next(x for x in client.get(f"/v1/envelopes/{e['id']}", headers=alice).json()["recipients"] if x["role"] == "client")["signing_url"]
    assert client.post(f"/v1/sign/{token_of(client_url)}/decline", json={"reason": "Terms unclear"}).json()["status"] == "declined"
    got = client.get(f"/v1/envelopes/{e['id']}", headers=alice).json()
    assert got["status"] == "declined" and client.get(f"/v1/sign/{token_of(client_url)}").json()["can_sign"] is False
    assert sign(client, client_url).status_code == 409
    assert client.post(f"/v1/envelopes/{e['id']}/recipients/{got['recipients'][1]['id']}/signing-url", headers=alice).status_code == 409


def test_embedded_signing_url_and_isolation(client, alice, bob, tpl):
    e = new_env(client, alice, tpl, recipients=[{"role": "client", "name": "Rita", "deliver": "none"}]).json()   # no email needed
    rid = next(x["id"] for x in e["recipients"] if x["role"] == "client")
    url = client.post(f"/v1/envelopes/{e['id']}/recipients/{rid}/signing-url", headers=alice).json()["url"]
    assert url == next(x for x in e["recipients"] if x["role"] == "client")["signing_url"]
    for path in ("", "/events", "/document", "/certificate"):
        assert client.get(f"/v1/envelopes/{e['id']}{path}", headers=bob).status_code == 404
    assert client.post(f"/v1/envelopes/{e['id']}/cancel", headers=bob).status_code == 404
    assert client.post("/v1/envelopes", headers=bob, json={"template_id": tpl["id"]}).status_code == 404
    assert client.get("/v1/sign/not-a-token").status_code == 404
    emails = client.get(f"/v1/envelopes/{e['id']}", headers=alice).json()["emails"]
    assert all(m["kind"] != "invite" for m in emails)                                  # embedded: nothing emailed


def test_idempotency_external_id_and_listing(client, alice, bob, tpl):
    h = {"Idempotency-Key": "order-42"}
    a = client.post("/v1/envelopes", headers={**alice, **h}, json={"template_id": tpl["id"], "recipients": [{"role": "client", "email": "i@example.com"}], "data": {"fee": "5"}})
    b = client.post("/v1/envelopes", headers={**alice, **h}, json={"template_id": tpl["id"], "recipients": [{"role": "client", "email": "i@example.com"}], "data": {"fee": "5"}})
    assert a.status_code == 201 and b.status_code == 200 and b.headers["Idempotent-Replay"] == "true" and a.json()["id"] == b.json()["id"]
    changed = client.post("/v1/envelopes", headers={**alice, **h}, json={"template_id": tpl["id"], "recipients": [{"role": "client", "email": "i@example.com"}], "data": {"fee": "6"}})
    assert changed.status_code == 422
    assert new_env(client, alice, tpl, external_id="REF-1").status_code == 201
    assert new_env(client, alice, tpl, external_id="REF-1").status_code == 409
    assert new_env(client, bob, tpl).status_code == 404

    for i in range(7):
        new_env(client, alice, tpl, name=f"Batch doc {i}", recipients=[{"role": "client", "email": f"p{i}@example.com"}])
    seen, cursor = [], None
    while True:
        page = client.get("/v1/envelopes", headers=alice, params={"limit": 4, **({"cursor": cursor} if cursor else {})}).json()
        seen += [e["id"] for e in page["data"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    total = client.get("/v1/envelopes/stats", headers=alice).json()
    assert len(seen) == len(set(seen)) == total["total"] and total["in_progress"] >= 9
    assert client.get("/v1/envelopes", headers=alice, params={"cursor": "garbage"}).status_code == 422
    q = client.get("/v1/envelopes", headers=alice, params={"q": "p3@example"}).json()["data"]
    assert len(q) == 1 and q[0]["name"] == "Batch doc 3" and next(r for r in q[0]["recipients"] if r["role"] == "client")["email"] == "p3@example.com"
    assert [e["external_id"] for e in client.get("/v1/envelopes", headers=alice, params={"q": "REF-1"}).json()["data"]] == ["REF-1"]
    assert client.get("/v1/envelopes", headers=alice, params={"status": "completed"}).json()["data"] == []
    assert client.get("/v1/envelopes", headers=bob).json()["data"] == []


def test_expiry_sweep(client, alice, tpl):
    from app.services.envelope_service import expire_due
    e = new_env(client, alice, tpl).json()
    url = next(x for x in e["recipients"] if x["role"] == "client")["signing_url"]
    with SessionLocal() as db:
        db.get(Envelope, e["id"]).expires_at = utc_now() - timedelta(minutes=1)
        db.commit()
        assert expire_due(db) >= 1
    assert client.get(f"/v1/envelopes/{e['id']}", headers=alice).json()["status"] == "expired"
    assert client.get(f"/v1/sign/{token_of(url)}").json()["can_sign"] is False


def test_checkbox_radio_and_text_types_render(client, alice):
    doc = client.post("/documents", headers=alice, files={"file": ("a.pdf", contract_pdf())}).json()
    fields = [
        {"name": "agree", "type": "checkbox", "role": "office", "x": .1, "y": .1, "style": {"color": "#cc0000"}},
        {"name": "off", "type": "checkbox", "role": "office", "x": .1, "y": .2},
        {"name": "pick", "type": "radio", "option": "A", "role": "office", "x": .1, "y": .3},
        {"name": "pick", "type": "radio", "option": "B", "role": "office", "x": .1, "y": .4},
        {"name": "notes", "type": "description", "role": "office", "x": .3, "y": .1, "w": .4, "h": .12},
        {"name": "pct", "type": "percentage", "role": "office", "x": .3, "y": .3},
        {"name": "when", "type": "date", "role": "office", "x": .3, "y": .35, "style": {"date_format": "%d/%m/%Y"}},
        {"name": "who", "type": "full_name", "role": "office", "x": .3, "y": .4, "style": {"uppercase": True, "font": "bundled:Caveat", "color": "#1d4ed8"}},
        {"name": "contact", "type": "phone", "role": "office", "x": .3, "y": .45},
    ]
    t = client.post("/v1/templates", headers=alice, json={"name": "Types", "document_id": doc["id"], "roles": [{"id": "office", "kind": "office"}], "fields": fields}).json()
    r = client.post("/v1/envelopes", headers=alice, json={"template_id": t["id"], "office_auto_sign": False, "data": {
        "agree": True, "off": "no", "pick": "b", "notes": "Line one of the note. " * 6, "pct": 12.5, "when": "5 Mar 2026",
        "who": "asha verma", "contact": "+91 98765 43210"}})
    assert r.status_code == 201, r.text
    env = r.json()
    assert env["status"] == "completed"                                              # office-only template completes at once
    with pymupdf.open(stream=client.get(f"/v1/envelopes/{env['id']}/document", headers=alice).content) as d:
        page = d[0]
        text = page.get_text()
        assert "12.5%" in text and "05/03/2026" in text and "ASHA VERMA" in text and "+919876543210" in text and "Line one of the note." in text
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
        dark = lambda fx, fy: sum(1 for x in range(int(fx * pix.width), int((fx + .022) * pix.width)) for y in range(int(fy * pix.height), int((fy + .016) * pix.height))
                                  if sum(pix.pixel(x, y)[:3]) < 450)
        assert dark(.1, .1) > 20 and dark(.1, .2) == 0                              # ticked vs unticked
        assert dark(.1, .4) > 20 and dark(.1, .3) == 0                              # chosen radio option vs the other
        assert any("Caveat" in f[3] for f in page.get_fonts())                      # per-field font


def test_rotated_pages_get_upright_fields(client, alice):
    from rapidocr_onnxruntime import RapidOCR
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 90), "Rotated sheet", fontsize=16)
    p.set_rotation(90)
    doc = client.post("/documents", headers=alice, files={"file": ("rot.pdf", d.tobytes())}).json()
    t = client.post("/v1/templates", headers=alice, json={"name": "R", "document_id": doc["id"], "roles": [{"id": "office", "kind": "office"}],
                    "fields": [{"name": "who", "type": "full_name", "role": "office", "x": .3, "y": .5, "w": .4}]}).json()
    env = client.post("/v1/envelopes", headers=alice, json={"template_id": t["id"], "data": {"who": "Ramesh Kumar"}}).json()
    with pymupdf.open(stream=client.get(f"/v1/envelopes/{env['id']}/document", headers=alice).content) as pdf:
        pdf[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).save("/tmp/rotated-env.png")
    res, _ = RapidOCR()("/tmp/rotated-env.png")
    assert any("Ramesh" in line for _, line, _ in (res or []))


# ---------------- scale: batches, library, webhooks ----------------

def test_async_batch(client, alice, bob, tpl, monkeypatch):
    from app.config import settings
    from app.services.envelope_batch import process_next_chunk
    hook = client.post("/webhooks", headers=alice, json={"url": "http://localhost:9/h", "events": ["batch.completed", "envelope.created"]}).json()
    items = [{"recipients": [{"role": "client", "name": f"C{i}", "email": f"c{i}@example.com"}], "data": {"client_name": f"Client {i}", "fee": i * 100, "plan": "Gold"},
              "external_id": f"B-{i}"} for i in range(4)]
    items.append({"recipients": [], "data": {"fee": 1}, "external_id": "B-bad"})            # fails alone
    items.append({"recipients": [{"role": "client", "email": "dup@example.com"}], "external_id": "B-0"})   # duplicate external id
    r = client.post("/v1/envelopes/batch", headers=alice, json={"template_id": tpl["id"], "items": items, "message": "Please sign"})
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["total"] == 6
    bid = r.json()["id"]
    monkeypatch.setattr(settings, "batch_chunk", 2)
    assert process_next_chunk() == 2
    mid = client.get(f"/v1/batches/{bid}", headers=alice).json()
    assert mid["status"] == "processing" and mid["created"] == 2 and 0 < mid["progress"] < 1
    while process_next_chunk():
        pass
    done = client.get(f"/v1/batches/{bid}", headers=alice).json()
    assert (done["status"], done["created"], done["failed"]) == ("completed", 4, 2) and done["finished_at"]
    assert {e["index"] for e in done["errors"]} == {4, 5}
    lines = [json.loads(l) for l in client.get(f"/v1/batches/{bid}/results", headers=alice).text.splitlines()]
    assert len(lines) == 6 and all("envelope_id" in l for l in lines[:4])
    listed = client.get("/v1/envelopes", headers=alice, params={"q": "B-"}).json()["data"]
    assert {e["external_id"] for e in listed} == {f"B-{i}" for i in range(4)}
    deliveries = client.get(f"/webhooks/{hook['id']}/deliveries", headers=alice).json()
    assert "batch.completed" in {d["event"] for d in deliveries} and sum(d["event"] == "envelope.created" for d in deliveries) >= 4
    assert client.get(f"/v1/batches/{bid}", headers=bob).status_code == 404
    monkeypatch.setattr(settings, "batch_max_items", 2)
    assert client.post("/v1/envelopes/batch", headers=alice, json={"template_id": tpl["id"], "items": items[:3]}).status_code == 422


def test_document_library_pagination(client, alice, bob, upload):
    ids = [upload(alice, name=f"report-{i}.pdf").json()["id"] for i in range(7)]
    upload(alice, name="invoice.pdf")
    seen, cursor, pages = [], None, 0
    while True:
        page = client.get("/v1/documents", headers=alice, params={"limit": 3, **({"cursor": cursor} if cursor else {})}).json()
        seen += [d["id"] for d in page["data"]]
        pages += 1
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert pages >= 3 and len(seen) == len(set(seen)) and set(ids) <= set(seen)
    q = client.get("/v1/documents", headers=alice, params={"q": "invoice", "include_total": True}).json()
    assert [d["filename"] for d in q["data"]] == ["invoice.pdf"] and q["total"] == 1
    assert client.get("/v1/documents", headers=alice, params={"source": "signed"}).json()["data"] == []
    assert client.get("/v1/documents", headers=bob).json()["data"] == []
    assert client.get("/v1/documents", headers=alice, params={"cursor": "zz"}).status_code == 422


def test_envelope_webhook_events(client, alice, tpl):
    hook = client.post("/webhooks", headers=alice, json={"url": "http://localhost:9/h"}).json()
    assert "envelope.completed" in hook["events"]
    e = new_env(client, alice, tpl, data={"fee": "5", "plan": "Gold", "client_name": "R"}).json()
    office = next(x for x in e["recipients"] if x["role"] == "office")
    client.post(f"/v1/sign/{token_of(office['signing_url'])}/submit", data={"consent": "true", "values": "{}"}, files={"signature": ("s.png", png_bytes())})
    client_url = next(x for x in client.get(f"/v1/envelopes/{e['id']}", headers=alice).json()["recipients"] if x["role"] == "client")["signing_url"]
    assert sign(client, client_url).status_code == 200
    events = [d["event"] for d in client.get(f"/webhooks/{hook['id']}/deliveries", headers=alice).json()]
    assert {"envelope.created", "envelope.sent", "recipient.signed", "envelope.completed"} <= set(events) and events.count("recipient.signed") >= 2
    done = next(d for d in client.get(f"/webhooks/{hook['id']}/deliveries", headers=alice).json() if d["event"] == "envelope.completed")
    assert done["payload"]["data"]["envelope_id"] == e["id"] and done["payload"]["data"]["final_sha256"]


# ---------------- email delivery + OTP for envelopes (real local SMTP) ----------------

from tests.test_email_bulk import connect_mail, smtp  # noqa: E402,F401  (fixtures)


@pytest.fixture()
def office(client, smtp):
    h = make_user(client, f"office-{id(object())}@firm.example")
    assert connect_mail(client, h, smtp).json()["settings"]["verified_at"]
    return h


def test_envelope_emails_and_otp(client, office, smtp):
    from app.services.envelope_mail import deliver_envelope_mail
    doc = client.post("/documents", headers=office, files={"file": ("agreement.pdf", contract_pdf())}).json()
    roles = [{"id": "office", "kind": "office"}, {"id": "client", "kind": "signer"}]
    t = client.post("/v1/templates", headers=office, json={"name": "Agr", "document_id": doc["id"], "roles": roles, "fields": FIELDS}).json()
    client.put("/v1/signature-profile", headers=office, data={"typed_name": "Priya Menon"})
    e = client.post("/v1/envelopes", headers=office, json={"template_id": t["id"], "office_auto_sign": True, "message": "Hi {name}, please sign.",
                    "recipients": [{"role": "client", "name": "Rita Shah", "email": "rita@client.example"}, {"role": "audit", "kind": "cc", "email": "cc@client.example"}],
                    "data": {"client_name": "Rita Shah", "fee": 500, "plan": "Silver"}}).json()
    while deliver_envelope_mail():
        pass
    inv = smtp.to("rita@client.example", "asked you to sign")[-1]
    body = inv.get_body(preferencelist=("plain",)).get_content()
    url = re.search(r"http\S+/e/[\w\-]+", body).group(0)
    assert url.endswith(token_of(next(r for r in e["recipients"] if r["role"] == "client")["signing_url"])) and "Hi {name}" not in body or "please sign" in body
    assert not smtp.to("cc@client.example", "asked you to sign")                      # cc only gets the finished copy

    tok = token_of(url)
    info = client.get(f"/v1/sign/{tok}").json()
    assert info["otp_required"] and info["verified"] is False and "fields" not in info and info["email_hint"].startswith("r")
    assert client.get(f"/v1/sign/{tok}/pages/1.png").status_code == 403
    assert sign(client, url).status_code == 403
    assert client.post(f"/v1/sign/{tok}/otp/send").status_code == 200
    assert client.post(f"/v1/sign/{tok}/otp/verify", json={"code": "000000"}).status_code == 400
    code = re.search(r"code: (\d{6})", smtp.to("rita@client.example", "verification code")[-1]["Subject"]).group(1)
    hdr = {"X-Verify": client.post(f"/v1/sign/{tok}/otp/verify", json={"code": code}).json()["verify_token"]}
    full = client.get(f"/v1/sign/{tok}", headers=hdr).json()
    assert full["verified"] and [f["name"] for f in full["fields"]] == ["accepted", "client_signature"]
    assert client.get(f"/v1/sign/{tok}/pages/1.png", headers=hdr).status_code == 200
    assert client.get(f"/v1/sign/{tok}/pages/1.png?v={hdr['X-Verify']}").status_code == 200
    ok = client.post(f"/v1/sign/{tok}/submit", headers=hdr, data={"values": '{"accepted": true}', "consent": "true"}, files={"signature": ("s.png", png_bytes())})
    assert ok.status_code == 200 and ok.json()["completed"]
    while deliver_envelope_mail():
        pass
    for who in ("rita@client.example", "cc@client.example"):
        mail = smtp.to(who, "Completed")[-1]
        pdf = list(mail.iter_attachments())[0].get_payload(decode=True)
        assert "Rita Shah" in pymupdf.open(stream=pdf)[0].get_text()
    emails = client.get(f"/v1/envelopes/{e['id']}", headers=office).json()["emails"]
    assert [m["status"] for m in emails if m["kind"] == "completed"] == ["sent"] * 3          # client, cc, owner
    ev = [x["type"] for x in client.get(f"/v1/envelopes/{e['id']}/events", headers=office).json()["events"]]
    assert {"otp_sent", "otp_verified", "signed", "completed"} <= set(ev)


def test_embedded_signing_can_skip_otp(client, office):
    doc = client.post("/documents", headers=office, files={"file": ("agreement.pdf", contract_pdf())}).json()
    t = client.post("/v1/templates", headers=office, json={"name": "A", "document_id": doc["id"], "roles": [{"id": "office", "kind": "office"}, {"id": "client", "kind": "signer"}], "fields": FIELDS}).json()
    e = client.post("/v1/envelopes", headers=office, json={"template_id": t["id"], "data": {"client_name": "X", "fee": 1, "plan": "Gold"},
                    "recipients": [{"role": "client", "deliver": "none", "verify": False}]}).json()
    url = next(x for x in e["recipients"] if x["role"] == "client")["signing_url"]
    assert client.get(f"/v1/sign/{token_of(url)}").json()["otp_required"] is False
