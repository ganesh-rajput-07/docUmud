# DocUMud

Document workspace: scan, upload, sign (yourself, in person, or by email link), track signers, and auto-tag PDFs for accessibility.

## Run
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# .env holds SECRET_KEY etc. (settings in app/config.py)
.venv/bin/uvicorn app.main:app --reload      # terminal 1: http://127.0.0.1:8000
.venv/bin/python -m app.worker               # terminal 2: accessibility tagging (needs Java 11+)
.venv/bin/python -m pytest
```
Email invites need `SMTP_HOST` (+ `SMTP_*`) in `.env`; otherwise the signing link is shown to the sender to share.

## API
Create a key in the dashboard (**API access**), then send `X-API-Key: dm_…` (or `Authorization: Bearer dm_…`). Interactive docs: `/docs`.
```
curl -H "X-API-Key: $KEY" -F file=@a.pdf http://127.0.0.1:8000/documents
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"page":0,"x":0.1,"y":0.2,"w":0.3,"h":0.05,"label":"Invoice No","value":"INV-1"}' \
  http://127.0.0.1:8000/documents/$DOC/tags
```
Tag coordinates are fractions (0–1) of the page, origin top-left. Keys can't create other keys.

## Fill documents from data (templates / mail-merge)
For repeat work such as a CA preparing documents for many clients:
1. **Data sheets** tab: upload the client list once (`.xlsx` or `.csv`, first row = column headers; up to 5,000 rows). `POST /datasets`.
2. **Tag the document once**: on the tagging screen click a column chip, then drag a box where that value goes. Tag names are matched to column headers automatically (ignoring case and punctuation).
3. **More → Fill from data**: pick the sheet, check the field-to-column matching, choose a font (13 bundled or your own `.ttf`/`.otf`), ink colour and alignment. All of it is saved with the document, so next time you just open the page and generate. Text shrinks to fit its box and wraps if needed; values that still don't fit are shortened with “…” and reported.
4. **Generate** a row range as a ZIP of PDFs (named after a column you choose) or one combined PDF saved to Documents. Up to 500 PDFs per run (`FILL_MAX_ROWS`); 200 take about 6 seconds.
API: `GET/PUT /documents/{id}/template`, `GET /documents/{id}/fill/preview.png?row=&page=`, `POST /documents/{id}/fill` (`{"start":1,"end":50,"mode":"zip"|"merged"}`), `/datasets`, `/fonts/catalog`. Tags with no matching column keep their fixed *value* (e.g. your firm name). Excel number formatting is not preserved, so format such columns as text in the sheet.

## Signatures
Three ways to create a signature on the sign pages (yours, in person, or the email recipient's): **Draw**, **Type** (9 bundled OFL script fonts), or **Upload / scan** a photo of a pen signature, which is cleaned and made transparent in the browser (nothing is uploaded except the final PNG). Ink colour can be black, blue or dark red.
Your own fonts: **Add your own font…** on the Type tab saves a `.ttf`/`.otf` (up to 10, 2 MB each) privately to your account (`GET/POST/DELETE /fonts`). Email recipients can load a font file for their session only; it never leaves their device. Bundled fonts live in `app/static/fonts` (see its README for licences).

## OCR
Dashboard → a document's **More → Run OCR**, or `POST /documents/{id}/ocr`. The worker (needs no system packages; uses RapidOCR) creates a searchable `…-ocr.pdf` (invisible text layer) and stores the text; read it with `GET /documents/{id}/text` (works on any PDF: OCR result or the PDF's own text layer). Up to 30 pages per document (`OCR_MAX_PAGES`); the first job loads the model, so it is slower. Latin text only in the searchable layer, and pages with a rotation flag are extracted but not made searchable. Treat output as machine-read and check important values.

## Webhooks
`POST /webhooks {"url": "https://…", "events": [...]}` (default: all) returns a signing `secret` once. Events: `signature.signed`, `signature.cancelled`, `job.completed`, `job.failed` (job events cover both OCR and accessibility tagging, see `service`). The worker delivers them (so it must be running) with retries at 30s, 2m, 10m, 1h, 6h, then marks them failed; inspect via `GET /webhooks/{id}/deliveries`.
Each POST carries `X-DocUMud-Event`, `X-DocUMud-Delivery`, `X-DocUMud-Timestamp` and `X-DocUMud-Signature: sha256=` + HMAC-SHA256(secret, `"{timestamp}.{raw body}"`). Verify it and reject old timestamps. Outside `ENVIRONMENT=development`, only public https URLs are accepted.

## Rate limits
Per minute: 300 per API key/session, 10 login/register per IP, 120 per IP on recipient signing pages (`RATE_LIMIT_PER_MINUTE`, `AUTH_RATE_LIMIT_PER_MINUTE`, `PUBLIC_RATE_LIMIT_PER_MINUTE`). Exceeding returns 429 with `Retry-After`. Counters are per process and keyed on the connecting IP, so behind a reverse proxy the IP-keyed limits need the real client IP passed through.

## Tags in the PDF
`POST /documents/{id}/tags/export` (or **Save tags into PDF** on the tagging screen) creates a new PDF with one annotation per tag, plus the tag list embedded as `documud-tags.json`.

## Signing modes
- **Myself** – draw/type, click to place, signed copy saved (original kept).
- **In person** – signer signs on your device (1-day link).
- **Email** – one-time link valid 7 days. Every action goes into a hash-chained audit trail with document and signed-file SHA-256.

## Not yet (do before real contracts)
Visible stamp only, not a certificate-backed digital signature. No email OTP identity check or malware scan. No Alembic migrations. Tagged PDFs are not PDF/UA verified (veraPDF pending). Classification and payments are not built.
# docUmud
