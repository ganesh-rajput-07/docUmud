import io
import json
import time
import zipfile
from pathlib import Path

import openpyxl
import pymupdf
import pytest

from tests.conftest import make_user, pdf_bytes

FONT = Path(__file__).parent.parent / "app/static/fonts/Kristi.ttf"


def xlsx_bytes(rows, headers=("Name", "PAN", "Amount", "Date")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROWS = [("Ramesh Iyer", "ABCDE1234F", 12500.5, "2026-04-01"), ("Sita Rao", "PQRSX9876K", 3000, "2026-04-02")]


@pytest.fixture()
def tagged(client, alice, upload):
    """A 2-page PDF with Name/PAN/Amount tags on page 1 and Name repeated on page 2."""
    doc = upload(alice).json()
    url = f"/documents/{doc['id']}/tags"
    for page, x, y, label in [(0, .1, .20, "Name"), (0, .1, .30, "PAN"), (0, .1, .40, "Amount"), (1, .1, .20, "Name")]:
        r = client.post(url, headers=alice, json={"page": page, "x": x, "y": y, "w": .5, "h": .05, "label": label})
        assert r.status_code == 201
    return doc


def add_sheet(client, headers, data=None, name="clients.xlsx", **form):
    data = xlsx_bytes(ROWS) if data is None else data
    return client.post("/datasets", headers=headers, data=form, files={"file": (name, data)})


def page_text(pdf_bytes_, page=0):
    with pymupdf.open(stream=pdf_bytes_) as pdf:
        return pdf[page].get_text()


def test_dataset_upload_parsing(client, alice, bob):
    r = add_sheet(client, alice)
    assert r.status_code == 201
    ds = r.json()
    assert ds["columns"] == ["Name", "PAN", "Amount", "Date"] and ds["row_count"] == 2
    rows = client.get(f"/datasets/{ds['id']}", headers=alice).json()["rows"]
    assert rows[0] == ["Ramesh Iyer", "ABCDE1234F", "12500.5", "2026-04-01"] and rows[1][2] == "3000"  # no "3000.0"
    assert client.get(f"/datasets/{ds['id']}", headers=bob).status_code == 404
    assert client.delete(f"/datasets/{ds['id']}", headers=bob).status_code == 404
    assert client.delete(f"/datasets/{ds['id']}", headers=alice).status_code == 204


def test_dataset_csv_and_errors(client, alice):
    csv = "Name;PAN\nAsha;AAAAA0000A\n\nRavi;BBBBB1111B\n".encode()
    ds = add_sheet(client, alice, csv, "x.csv").json()
    assert ds["columns"] == ["Name", "PAN"] and ds["row_count"] == 2          # ';' sniffed, blank line skipped
    assert add_sheet(client, alice, b"junk", "bad.xlsx").status_code == 422
    assert add_sheet(client, alice, b"x", "notes.docx").status_code == 422
    assert add_sheet(client, alice, b"Name,PAN\n", "empty.csv").status_code == 422
    assert add_sheet(client, alice, xlsx_bytes(ROWS), sheet="Nope").status_code == 422
    dup = add_sheet(client, alice, xlsx_bytes([("a",)], headers=("Name", "Name", "")), "d.xlsx").json()
    assert dup["columns"] == ["Name", "Name (2)", "Column 3"] or dup["columns"][:2] == ["Name", "Name (2)"]


def test_template_defaults_and_auto_mapping(client, alice, tagged):
    url = f"/documents/{tagged['id']}/template"
    t = client.get(url, headers=alice).json()
    assert t["style"]["font"] == "bundled:Lato" and [f["label"] for f in t["fields"]] == ["Name", "PAN", "Amount"]
    assert t["fields"][0]["count"] == 2 and t["dataset"] is None

    ds = add_sheet(client, alice).json()
    t = client.put(url, headers=alice, json={"dataset_id": ds["id"], "font": "bundled:Caveat", "color": "#1d4ed8", "align": "center"}).json()
    assert t["mapping"] == {"Name": "Name", "PAN": "PAN", "Amount": "Amount"}   # matched by header
    assert t["style"] == {"font": "bundled:Caveat", "color": "#1d4ed8", "align": "center", "max_font_size": 0}

    t = client.put(url, headers=alice, json={"mapping": {"Name": "Date", "PAN": None, "Amount": "Amount"}}).json()
    assert t["mapping"]["Name"] == "Date" and t["mapping"]["PAN"] is None
    assert client.get(url, headers=alice).json()["style"]["font"] == "bundled:Caveat"    # persisted


def test_template_validation_and_isolation(client, alice, bob, tagged):
    url = f"/documents/{tagged['id']}/template"
    ds = add_sheet(client, alice).json()
    assert client.put(url, headers=alice, json={"font": "bundled:Nope"}).status_code == 422
    assert client.put(url, headers=alice, json={"font": "user:abc"}).status_code == 422
    assert client.put(url, headers=alice, json={"color": "red"}).status_code == 422
    assert client.put(url, headers=alice, json={"align": "justify"}).status_code == 422
    assert client.put(url, headers=alice, json={"dataset_id": "nope"}).status_code == 404
    client.put(url, headers=alice, json={"dataset_id": ds["id"]})
    assert client.put(url, headers=alice, json={"mapping": {"Name": "Missing"}}).status_code == 422
    assert client.get(url, headers=bob).status_code == 404
    assert client.put(url, headers=bob, json={"dataset_id": ds["id"]}).status_code == 404
    # bob can't use alice's data sheet on his own document
    bdoc = client.post("/documents", headers=bob, files={"file": ("b.pdf", pdf_bytes())}).json()
    assert client.put(f"/documents/{bdoc['id']}/template", headers=bob, json={"dataset_id": ds["id"]}).status_code == 404


def test_fill_zip(client, alice, tagged):
    ds = add_sheet(client, alice).json()
    url = f"/documents/{tagged['id']}"
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"], "filename_column": "Name"})
    r = client.post(f"{url}/fill", headers=alice, json={})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["X-DocUMud-Rows"] == "2" and r.headers["X-DocUMud-Overflow"] == "0"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["Ramesh Iyer.pdf", "Sita Rao.pdf"]
    one = z.read("Ramesh Iyer.pdf")
    assert "Ramesh Iyer" in page_text(one, 0) and "ABCDE1234F" in page_text(one, 0) and "12500.5" in page_text(one, 0)
    assert "Ramesh Iyer" in page_text(one, 1) and "Sita" not in page_text(one, 0)   # repeated tag on page 2
    assert "Sita Rao" in page_text(z.read("Sita Rao.pdf"), 0)
    # original untouched
    orig = client.get(f"{url}/download", headers=alice).content
    assert "Ramesh" not in page_text(orig, 0)

    part = client.post(f"{url}/fill", headers=alice, json={"start": 2, "end": 2})
    assert zipfile.ZipFile(io.BytesIO(part.content)).namelist() == ["Sita Rao.pdf"]


def test_fill_default_names_and_duplicates(client, alice, tagged):
    ds = add_sheet(client, alice, xlsx_bytes([("Same", "A"), ("Same", "B"), ("a/b:c", "C")], headers=("Name", "PAN"))).json()
    url = f"/documents/{tagged['id']}"
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"], "filename_column": "Name"})
    names = zipfile.ZipFile(io.BytesIO(client.post(f"{url}/fill", headers=alice, json={}).content)).namelist()
    assert names == ["Same.pdf", "Same-2.pdf", "a_b_c.pdf"]
    client.put(f"{url}/template", headers=alice, json={"filename_column": None})
    names = zipfile.ZipFile(io.BytesIO(client.post(f"{url}/fill", headers=alice, json={}).content)).namelist()
    assert names == ["contract-001.pdf", "contract-002.pdf", "contract-003.pdf"]


def test_fill_merged(client, alice, tagged):
    ds = add_sheet(client, alice).json()
    url = f"/documents/{tagged['id']}"
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"]})
    r = client.post(f"{url}/fill", headers=alice, json={"mode": "merged"})
    assert r.status_code == 200
    doc = r.json()["document"]
    assert doc["source"] == "filled" and doc["pages"] == 4 and doc["parent_id"] == tagged["id"]
    data = client.get(f"/documents/{doc['id']}/download", headers=alice).content
    assert "Ramesh Iyer" in page_text(data, 0) and "Sita Rao" in page_text(data, 2)


def test_fixed_values_blank_cells_and_missing_columns(client, alice, upload):
    doc = upload(alice).json()
    url = f"/documents/{doc['id']}"
    for label, value, y in [("Name", "", .2), ("Firm", "ACME & Co", .3), ("Remarks", "", .4)]:
        client.post(f"{url}/tags", headers=alice, json={"page": 0, "x": .1, "y": y, "w": .5, "h": .05, "label": label, "value": value})
    ds = add_sheet(client, alice, xlsx_bytes([("Asha", "")], headers=("Name", "Other"))).json()
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"]})
    pdf = zipfile.ZipFile(io.BytesIO(client.post(f"{url}/fill", headers=alice, json={}).content)).read("contract-001.pdf")
    text = page_text(pdf)
    assert "Asha" in text and "ACME & Co" in text          # column match + fixed value (Firm has no column)


def test_overflow_is_reported(client, alice, upload):
    doc = upload(alice).json()
    url = f"/documents/{doc['id']}"
    client.post(f"{url}/tags", headers=alice, json={"page": 0, "x": .1, "y": .2, "w": .1, "h": .02, "label": "Note"})
    ds = add_sheet(client, alice, xlsx_bytes([("word " * 200,)], headers=("Note",))).json()
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"]})
    r = client.post(f"{url}/fill", headers=alice, json={})
    assert r.status_code == 200 and r.headers["X-DocUMud-Overflow"] == "1"
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist()


def test_custom_font_used_and_fallback(client, alice, tagged):
    ds = add_sheet(client, alice).json()
    url = f"/documents/{tagged['id']}"
    font = client.post("/fonts", headers=alice, files={"file": ("Hand.ttf", FONT.read_bytes())}).json()
    cat = client.get("/fonts/catalog", headers=alice).json()
    assert {f["label"] for f in cat["bundled"]} >= {"Lato", "Caveat"} and cat["user"][0]["key"] == f"user:{font['id']}"
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"], "font": f"user:{font['id']}"})
    z = client.post(f"{url}/fill", headers=alice, json={})
    with pymupdf.open(stream=zipfile.ZipFile(io.BytesIO(z.content)).read("contract-001.pdf")) as pdf:
        assert any("Kristi" in f[3] for f in pdf[0].get_fonts())
    # deleting the font falls back to the default with a warning instead of failing
    client.delete(f"/fonts/{font['id']}", headers=alice)
    z = client.post(f"{url}/fill", headers=alice, json={})
    assert z.status_code == 200 and "Lato" in z.headers["X-DocUMud-Warnings"]


def test_fill_errors_and_limits(client, alice, bob, tagged, upload, monkeypatch):
    url = f"/documents/{tagged['id']}"
    assert client.post(f"{url}/fill", headers=alice, json={}).status_code == 409           # no sheet yet
    untagged = upload(alice).json()
    assert client.post(f"/documents/{untagged['id']}/fill", headers=alice, json={}).status_code == 409
    ds = add_sheet(client, alice).json()
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"]})
    assert client.post(f"{url}/fill", headers=bob, json={}).status_code == 404
    assert client.post(f"{url}/fill", headers=alice, json={"start": 5}).status_code == 422
    from app.config import settings
    monkeypatch.setattr(settings, "fill_max_rows", 1)
    assert client.post(f"{url}/fill", headers=alice, json={}).status_code == 422
    monkeypatch.setattr(settings, "fill_max_rows", 500)
    monkeypatch.setattr(settings, "max_pdf_pages", 3)
    assert client.post(f"{url}/fill", headers=alice, json={"mode": "merged"}).status_code == 422
    assert client.delete(f"/datasets/{ds['id']}", headers=alice).status_code == 204
    assert client.get(f"{url}/template", headers=alice).json()["dataset"] is None           # reference cleared


def test_preview_png(client, alice, tagged):
    url = f"/documents/{tagged['id']}"
    r = client.get(f"{url}/fill/preview.png", headers=alice)                                # no sheet: shows field names
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    ds = add_sheet(client, alice).json()
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"]})
    assert client.get(f"{url}/fill/preview.png?row=2&page=1", headers=alice).status_code == 200
    assert client.get(f"{url}/fill/preview.png?row=9", headers=alice).status_code == 404
    assert client.get(f"{url}/fill/preview.png?page=9", headers=alice).status_code == 404


def test_fill_speed(client, alice, tagged):
    rows = [(f"Client {i}", f"ABCDE{i:04d}F", i * 10, "2026-04-01") for i in range(200)]
    ds = add_sheet(client, alice, xlsx_bytes(rows)).json()
    url = f"/documents/{tagged['id']}"
    client.put(f"{url}/template", headers=alice, json={"dataset_id": ds["id"], "font": "bundled:Lato"})
    t = time.time()
    r = client.post(f"{url}/fill", headers=alice, json={})
    elapsed = time.time() - t
    print(f"\n200 PDFs in {elapsed:.1f}s, zip {len(r.content)//1024} KB")
    assert r.headers["X-DocUMud-Rows"] == "200" and elapsed < 30
