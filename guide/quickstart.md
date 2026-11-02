# Quickstart

Send a document for signature in five steps. Replace `$BASE` with your server (for example `http://127.0.0.1:8000`) and `$KEY` with an API key from **Dashboard → Developers → Create key**.

```bash
export BASE=http://127.0.0.1:8000
export KEY=dm_xxxxxxxx
```

## 1. Create a template from a PDF

One call uploads the PDF and defines its fields. Fields are placed **next to printed text** (`anchor`), so you don't need coordinates.

```bash
curl -s $BASE/v1/templates/upload -H "X-API-Key: $KEY" \
  -F file=@engagement-letter.pdf \
  -F 'definition={
    "name": "Engagement letter",
    "roles": [
      {"id": "office", "kind": "office", "label": "Our firm"},
      {"id": "client", "kind": "signer", "label": "Client"}
    ],
    "fields": [
      {"name": "client_name", "type": "full_name", "role": "office", "anchor": {"text": "Client name:"}, "w": 0.35,
       "aliases": ["customer_name"]},
      {"name": "pan",         "type": "id_number", "role": "office", "anchor": {"text": "PAN:"},
       "aliases": ["tax_id"], "validation": {"pattern": "[A-Z]{5}[0-9]{4}[A-Z]"}},
      {"name": "fee",         "type": "amount",    "role": "office", "anchor": {"text": "Fee:"},
       "style": {"currency": "₹", "grouping": "indian"}},
      {"name": "accepted",    "type": "checkbox",  "role": "client", "anchor": {"text": "I accept the terms", "position": "left"}},
      {"name": "office_sig",  "type": "signature", "role": "office", "anchor": {"text": "For the firm:"}},
      {"name": "client_sig",  "type": "signature", "role": "client", "anchor": {"text": "Client signature:"}},
      {"name": "signed_on",   "type": "date_signed", "role": "client", "anchor": {"text": "Date:"}}
    ]
  }'
```

The response contains the template `id`. Check how it looks (sample data rendered into your PDF):

```bash
curl -s "$BASE/v1/templates/$TPL/preview.png?page=1" -H "X-API-Key: $KEY" -o preview.png
```

> If an anchor text isn't found, you get a `422` naming the field and the text. Placement is done once, when the template is saved.

## 2. Save the office signature (once)

```bash
curl -s -X PUT $BASE/v1/signature-profile -H "X-API-Key: $KEY" \
  -F typed_name="Priya Menon" -F font=GreatVibes
# or upload your own transparent PNG:  -F signature=@signature.png
```

## 3. Create an envelope

Send the data in **whatever shape you already have**. The office signs automatically; the client gets the link.

```bash
curl -s $BASE/v1/envelopes -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-10432" \
  -d '{
    "template_id": "'$TPL'",
    "external_id": "CASE-10432",
    "office_auto_sign": true,
    "message": "Hi {name}, please review and sign.",
    "recipients": [{"role": "client", "name": "Rita Shah", "email": "rita@example.com"}],
    "data": {
      "customer": {"name": "Rita Shah", "tax_id": "ABCDE1234F"},
      "fee": "1,25,000"
    }
  }'
```

What happens:

- `customer.name` fills **client_name** (its alias `customer_name` matches the path `customer.name`), `fee` fills **fee** by name, and `customer.tax_id` fills **pan** (alias `tax_id`). See [Sending data](/guide/data) for the matching rules.
- The office signature and date are applied; the client's turn begins.
- The client gets an email (if your mailbox is connected) containing their `signing_url`. The response also includes it, so you can show it in your own app instead.

```json
{
  "id": "5b1e…", "status": "in_progress", "external_id": "CASE-10432",
  "recipients": [
    {"role": "office", "status": "signed", "signing_url": null},
    {"role": "client", "status": "pending", "signing_url": "https://…/e/Tk3…"}
  ]
}
```

Bad data never creates half an envelope. You get a `422` listing every problem at once:

```json
{"detail": {"message": "The request has problems; nothing was created.",
            "errors": {"data.fee": "'lots' is not a valid number", "recipients.client.email": "…"}}}
```

## 4. The client signs

They open the link, fill the fields you left for them, draw/type/upload a signature and press **Finish**. If you want to skip emailing and host the experience yourself, create the recipient with `"deliver": "none"` and redirect them to the `signing_url` (set `return_url` to bring them back). See [embedded signing](/guide/api#embedded-signing).

## 5. Get the result

Subscribe to the `envelope.completed` [webhook](/guide/webhooks), or poll:

```bash
curl -s $BASE/v1/envelopes/$ENV -H "X-API-Key: $KEY"                    # status, recipients, field values
curl -s $BASE/v1/envelopes/$ENV/document    -H "X-API-Key: $KEY" -o signed.pdf
curl -s $BASE/v1/envelopes/$ENV/certificate -H "X-API-Key: $KEY" -o certificate.pdf
curl -s $BASE/v1/envelopes/$ENV/events      -H "X-API-Key: $KEY"        # hash-chained audit trail
```

Look an envelope up by **your** reference: `GET /v1/envelopes?external_id=CASE-10432`.

## The same in Python

```python
import requests
BASE, H = "http://127.0.0.1:8000", {"X-API-Key": "dm_xxxxxxxx"}

env = requests.post(f"{BASE}/v1/envelopes", headers={**H, "Idempotency-Key": "order-10432"}, json={
    "template_id": TEMPLATE_ID,
    "office_auto_sign": True,
    "recipients": [{"role": "client", "name": "Rita Shah", "email": "rita@example.com"}],
    "data": {"customer": {"name": "Rita Shah"}, "fee": 125000},
}).json()
print(env["id"], [r["signing_url"] for r in env["recipients"]])
```

## Many at once

```bash
curl -s $BASE/v1/envelopes/batch -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{
  "template_id": "'$TPL'", "office_auto_sign": true,
  "items": [
    {"external_id": "A-1", "recipients": [{"role":"client","email":"a@example.com"}], "data": {"client_name": "A", "fee": 100}},
    {"external_id": "A-2", "recipients": [{"role":"client","email":"b@example.com"}], "data": {"client_name": "B", "fee": 200}}
  ]}'
# -> 202 {"id": "...", "status": "queued"}; poll GET /v1/batches/{id} or use the batch.completed webhook
```
