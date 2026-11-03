# API reference

The interactive reference (every parameter, try-it-out) is always current at [`/docs`](/docs). This page explains the concepts around it.

## Authentication

Create an API key in **Dashboard → Developers**. Send it on every request:

```
X-API-Key: dm_xxxxxxxx
```
(or `Authorization: Bearer dm_xxxxxxxx`). Keys are shown once, stored hashed, and can be revoked. A key can do everything its account can **except** manage API keys and email credentials (those need a browser login).

## Errors

| Status | Meaning |
|---|---|
| `401` | Missing, invalid or revoked credentials. |
| `404` | Doesn't exist **or isn't yours** (you can't probe other accounts). |
| `409` | The object is in the wrong state (e.g. sending a non-draft, `external_id` already used). |
| `422` | The request is invalid. For envelopes the body lists every problem: `{"detail": {"message": "…", "errors": {"data.fee": "…"}}}`. Plain request-shape errors use FastAPI's `{"detail": [{"loc": [...], "msg": "…"}]}`. |
| `429` | Rate limited. Honour `Retry-After`. |

## Rate limits

300 requests per minute per API key (`X-RateLimit-Limit`, `X-RateLimit-Remaining` headers; `429` + `Retry-After` when exceeded). Counters are per server process; see [Scaling](/guide/scaling).

## Idempotency

Send `Idempotency-Key: <your unique string>` on `POST /v1/envelopes`. Repeating the request with the same key and body returns the **original envelope** (status `200`, header `Idempotent-Replay: true`) instead of creating a duplicate; the same key with a different body is a `422`. Use it on every retry. `external_id` gives you a second, permanent guard: it's unique per account (`409` if reused).

## Pagination

List endpoints (`GET /v1/envelopes`, `GET /v1/documents`) are **cursor-paginated**, newest first:

```json
{"data": [ … ], "has_more": true, "next_cursor": "MjAyNi0x…"}
```

Pass `next_cursor` back as `?cursor=`. `limit` is 1–200 (default 50). Pages stay equally fast however deep you go, and new items never shift pages.

## Envelopes

### Create

`POST /v1/envelopes`

| Field | Notes |
|---|---|
| `template_id` | Required. |
| `recipients` | One per external role the template uses: `{role, name, email, deliver, verify, order}`. Add `{"kind": "cc", "role": "anything", "email": "…"}` for people who only get the finished copy. |
| `data` | Field values in [any shape](/guide/data). `field_map` optionally maps `{field: "path.in.data"}`. |
| `name` | Defaults to the template name. |
| `message` | Added to the invitation email. `{name}` is replaced with the recipient's name. |
| `signing_order` | `sequential` (default; roles by their `order`, equal orders sign together) or `parallel`. |
| `expires_in_days` | 1–90, default 14. |
| `external_id` | Your reference. Unique per account. Look up with `?external_id=`. |
| `metadata` | Free-form object, returned on reads and in every webhook. |
| `return_url` | Where to send signers after they finish (https; `http` only in development). |
| `send` | `true` (default) starts signing. `false` creates a **draft**: edit with `PATCH /v1/envelopes/{id}/data`, then `POST …/send`. |
| `office_auto_sign` | Apply the office's [saved signature](#office-signature) to the office's signature, initials and date-signed fields. |
| `strict` | Fail if any required office field has no data. |

**Recipient options.** `deliver`: `email` (default) emails the link; `none` sends nothing and you use the `signing_url` yourself ([embedded signing](#embedded-signing)). `verify` (default `true`): require an emailed one-time code before the person can open the document (applies when your mailbox is connected; set `false` for embedded flows where you've already authenticated the person).

The response is the envelope, including each recipient's `signing_url`:

```json
{
  "id": "…", "name": "Engagement letter", "status": "in_progress", "template_id": "…", "template_version": 3,
  "signing_order": "sequential", "external_id": "CASE-10432", "metadata": {}, "return_url": null,
  "recipients": [
    {"id": "…", "role": "office", "kind": "office", "name": "Priya", "email": "priya@firm.example", "order": 1, "status": "signed", "deliver": "none", "signed_at": "…", "signing_url": null},
    {"id": "…", "role": "client", "kind": "signer", "name": "Rita Shah", "email": "rita@example.com", "order": 2, "status": "pending", "deliver": "email", "signed_at": null, "signing_url": "https://…/e/…"}
  ],
  "created_at": "…", "expires_at": "…", "sent_at": "…", "completed_at": null,
  "final_document_id": null, "final_sha256": null, "document_url": "/v1/envelopes/…/document",
  "warnings": []
}
```

### Lifecycle

```
draft ──send──► in_progress ──everyone signs──► completed
                    │  ├─ someone declines ───► declined
                    │  ├─ you void it ────────► cancelled
                    │  └─ expires_at passes ──► expired
```

Recipient status: `waiting` (not their turn) → `pending` (their turn) → `viewed` → `signed`, or `declined`.

**Routing.** In `sequential` mode only the lowest-`order` unsigned roles are active; when they finish, the next group is notified. A role with **nothing left to fill or sign** (every field already supplied) is completed automatically, so a data-only office role never blocks anyone. When the last signer finishes, the final PDF is assembled, hashed and stored, the completed copy is emailed to everyone (including CCs and you), and `envelope.completed` fires.

### Read and act

| | |
|---|---|
| `GET /v1/envelopes` | List. Filters: `status` (comma-separated), `template_id`, `external_id` (exact, indexed), `q` (substring over name / reference / recipient email: a scan, so prefer `external_id` for lookups), `created_after`, `created_before`, `limit`, `cursor`. |
| `GET /v1/envelopes/stats` | Counts per status in one query. |
| `GET /v1/envelopes/{id}` | Full detail: recipients, every field with its value/signed state, and email delivery status. |
| `GET /v1/envelopes/{id}/events` | Hash-chained audit trail; `chain_valid` tells you if it was tampered with. |
| `GET /v1/envelopes/{id}/document` | The final PDF once completed; before that, the current state rendered on the fly. |
| `GET /v1/envelopes/{id}/certificate` | Certificate of Completion (PDF) with parties, timestamps, IPs, hashes and the audit trail. Completed envelopes only. |
| `POST /v1/envelopes/{id}/send` | Send a draft. |
| `PATCH /v1/envelopes/{id}/data` | Change values while still a draft. |
| `POST /v1/envelopes/{id}/remind` | Email the people whose turn it is (not within an hour of the last email). `?recipient_id=` to target one. |
| `POST /v1/envelopes/{id}/cancel` | Void; every link stops working. |
| `POST /v1/envelopes/{id}/recipients/{recipient_id}/signing-url` | (Re-)issue a recipient's link. |

## Embedded signing

You want signing to happen inside **your** product, not an email.

1. Create the recipient with `"deliver": "none"` (and usually `"verify": false`, since you've already authenticated them).
2. Take `recipients[].signing_url` from the create response (or ask again with `POST …/signing-url`).
3. Send the person there, either:
   - **Redirect**: navigate to the `signing_url`. Set `return_url` on the envelope; when they finish, the page shows a **Continue** button to it.
   - **iframe**: set the server setting `EMBED_ALLOWED_ORIGINS="https://app.yourcompany.com"`. Only the signing page (`/e/…`) then permits being framed, and only by those origins; everything else stays frame-blocked.
4. Learn the outcome from the `recipient.signed` / `envelope.completed` [webhooks](/guide/webhooks) (or poll), then fetch the PDF with your API key.

The `signing_url` is a capability: anyone holding it can sign for that recipient, so treat it like a password and don't log it.

## Office signature

The office's signature and initials are saved once and applied by `office_auto_sign`:

```bash
curl -X PUT $BASE/v1/signature-profile -H "X-API-Key: $KEY" -F typed_name="Priya Menon" -F font=GreatVibes
curl -X PUT $BASE/v1/signature-profile -H "X-API-Key: $KEY" -F signature=@sig.png -F initials=@initials.png   # PNG, transparent background
```

`GET /v1/signature-profile` says whether one is saved; `GET /v1/signature-profile/signature.png` previews it; `DELETE` removes it. If you ask for `office_auto_sign` without one you get a clear `422`. If the office still has unfilled required fields afterwards (e.g. you supplied no data for them), the office stays pending and an `auto_sign_partial` event explains why.

## Batches

`POST /v1/envelopes/batch` accepts up to `BATCH_MAX_ITEMS` (default 2000) items and **returns immediately** with `202`. The worker creates the envelopes in the background (`BATCH_CHUNK` per turn); every item is independent, so one bad row fails alone.

```json
{"template_id": "…", "office_auto_sign": true, "message": "Please sign", "send": true,
 "items": [
   {"external_id": "A-1", "recipients": [{"role": "client", "email": "a@example.com"}], "data": {"client_name": "A"}},
   {"external_id": "A-2", "recipients": [{"role": "client", "email": "b@example.com"}], "data": {"client_name": "B"}}
 ]}
```

Shared options (`message`, `signing_order`, `expires_in_days`, `send`, `office_auto_sign`, `strict`, `return_url`) apply to every item; an item may override them. Track with:

- `GET /v1/batches/{id}`: `status` (`queued` → `processing` → `completed`), `created`, `failed`, `progress`, and the first 100 errors.
- `GET /v1/batches/{id}/results`: one JSON line per item: `{"index": 0, "envelope_id": "…"}` or `{"index": 4, "error": "…", "errors": {…}}`.
- The `batch.completed` webhook.

The **worker must be running** (`python -m app.worker`).

## Documents

`POST /documents` (multipart `file`) uploads a PDF, JPG or PNG; `GET /documents/{id}/download` downloads it. `GET /v1/documents` lists a large library with `q` (filename), `source`, `limit`, `cursor`, and `include_total=true` when you need a count. Completed envelopes also appear here (`source: "envelope"`).

## Email and verification

Signature emails are sent **from the account owner's own mailbox** (Gmail app password, Outlook, any SMTP), configured in **Email settings**; credentials are stored encrypted and never returned. Emails are queued and delivered by the worker with retries, a per-account daily limit (default 400, so provider caps such as Gmail's ~500/day are respected) and automatic failure reporting on the envelope (`emails[]` in `GET /v1/envelopes/{id}`). With no mailbox connected nothing is emailed; use `signing_url`s instead.

When a mailbox is connected, external signers verify a **6-digit emailed code** before they can open the document (30 s resend cool-down, 5 codes/hour, 5 attempts, 10 min expiry). The audit trail records `otp_sent` / `otp_verified`, and the certificate states which identity check was used.

## Recipient endpoints

The signing page is a client of these; use them to build a fully custom signing UI. The token in the URL is the credential and needs no API key.

| | |
|---|---|
| `GET /v1/sign/{token}` | What this person must do: envelope, sender, status, `can_sign`, and (once verified) the editable `fields` with positions. |
| `POST /v1/sign/{token}/otp/send`, `…/otp/verify` | Email verification. `verify` returns a `verify_token`, sent afterwards as `X-Verify: <token>` or `?v=<token>`. |
| `GET /v1/sign/{token}/pages/{n}.png` | A page as this person sees it (other roles' values and signatures drawn in; their own fields left empty). |
| `POST /v1/sign/{token}/submit` | Multipart: `values` (JSON `{field name: value}`), `consent=true`, `signature` and/or `initials` PNG. Returns per-field errors on `422`. |
| `POST /v1/sign/{token}/decline` | `{"reason": "…"}`. |
| `GET /v1/sign/{token}/document` | The document (final PDF once completed). |

## Limits

| | |
|---|---|
| Upload size | 25 MB (`MAX_UPLOAD_MB`), PDFs up to 100 pages (`MAX_PDF_PAGES`) |
| Fields per template | no hard cap; each text value ≤ 4,000 characters |
| Batch | 2,000 items per request (`BATCH_MAX_ITEMS`) |
| Data sheets | 5,000 rows × 100 columns |
| Envelope expiry | 1–90 days |
| Signature images | PNG ≤ 1 MB |
| Webhooks | 10 per account |
