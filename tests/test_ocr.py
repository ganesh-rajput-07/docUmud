import io

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from app import worker


def _scan_png() -> bytes:
    img = Image.new("RGB", (1000, 360), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=48)
    d.text((40, 50), "Invoice No: INV-2041", fill="black", font=font)
    d.text((40, 170), "Total amount: 1,250.00", fill="black", font=font)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def _run_next_job():
    job_id = worker.claim_job()
    assert job_id
    worker.process_job(job_id)
    return job_id


def test_ocr_makes_searchable_pdf(client, alice, upload):
    doc = upload(alice, _scan_png(), name="scan.png").json()
    assert client.get(f"/documents/{doc['id']}/text", headers=alice).json()["has_text"] is False

    assert client.post(f"/documents/{doc['id']}/ocr", headers=alice).status_code == 202
    assert client.post(f"/documents/{doc['id']}/ocr", headers=alice).status_code == 409  # already queued
    _run_next_job()

    job = next(j for j in client.get("/jobs", headers=alice).json() if j["service"] == "ocr")
    assert job["status"] == "completed", job
    assert job["report"]["lines"] == 2 and job["report"]["avg_confidence"] > 0.8

    result_id = job["result_document_id"]
    data = client.get(f"/documents/{result_id}/download", headers=alice).content
    with pymupdf.open(stream=data) as pdf:
        hits = pdf[0].search_for("INV-2041")
        assert hits, pdf[0].get_text()

    text = client.get(f"/documents/{result_id}/text", headers=alice).json()
    assert text["source"] == "ocr" and "Invoice No: INV-2041" in text["pages"][0]["text"]
    assert client.get(f"/documents/{result_id}/text", headers={}).status_code == 401


def test_ocr_limits_and_isolation(client, alice, bob, upload, monkeypatch):
    from app.config import settings
    doc = upload(alice).json()
    assert client.post(f"/documents/{doc['id']}/ocr", headers=bob).status_code == 404
    assert client.get(f"/documents/{doc['id']}/text", headers=bob).status_code == 404
    monkeypatch.setattr(settings, "ocr_max_pages", 1)
    assert client.post(f"/documents/{doc['id']}/ocr", headers=alice).status_code == 422
    # born-digital PDFs expose their own text layer
    t = client.get(f"/documents/{doc['id']}/text", headers=alice).json()
    assert t["source"] == "pdf" and "agreement" in t["pages"][0]["text"]
