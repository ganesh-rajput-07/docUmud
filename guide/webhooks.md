# Webhooks

Webhooks push events to your system the moment they happen, so you don't have to poll.

## Subscribe

```bash
curl -s $BASE/webhooks -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://your-app.example.com/documud", "events": ["envelope.completed", "recipient.signed"]}'
```

Omit `events` to receive all of them. The response contains a `secret` **once**; store it to verify signatures. Outside development the URL must be a public `https://` address (private/internal addresses are refused). Manage with `GET /webhooks`, `DELETE /webhooks/{id}`, and inspect recent attempts with `GET /webhooks/{id}/deliveries`. You can also do this in **Dashboard → Developers**.

## Events

| Event | When | `data` includes |
|---|---|---|
| `envelope.created` | An envelope was created. | `envelope_id`, `name`, `status`, `external_id`, `template_id`, `metadata` |
| `envelope.sent` | A draft was sent / signing started. | same |
| `recipient.signed` | One recipient (office or external) finished. | same + `recipient {id, role, name, email, signed_at}` |
| `envelope.completed` | Everyone signed and the final PDF is ready. | same + `final_document_id`, `final_sha256` |
| `envelope.declined` | A recipient declined. | same + `reason`, `recipient` |
| `envelope.cancelled` | You voided it. | same |
| `envelope.expired` | It passed `expires_at` unsigned. | same |
| `batch.completed` | A batch finished. | `batch_id`, `total`, `created`, `failed` |
| `signature.signed`, `signature.cancelled`, `job.completed`, `job.failed` | The older single-document signature requests and processing jobs. | |

## Delivery

```
POST https://your-app.example.com/documud
Content-Type: application/json
X-DocUMud-Event: envelope.completed
X-DocUMud-Delivery: 7d0f…            (unique per delivery: use it to de-duplicate)
X-DocUMud-Timestamp: 1767312000
X-DocUMud-Signature: sha256=3b1f…
```

```json
{"id": "7d0f…", "event": "envelope.completed", "created_at": "2026-11-02T10:15:00+00:00",
 "data": {"envelope_id": "5b1e…", "name": "Engagement letter", "status": "completed", "external_id": "CASE-10432",
          "template_id": "…", "metadata": {"case": 7}, "final_document_id": "…", "final_sha256": "9c4a…"}}
```

Respond with any `2xx` within 10 seconds. Anything else (or a timeout) is retried after **30 s, 2 min, 10 min, 1 h, 6 h** and then marked failed (`WEBHOOK_MAX_ATTEMPTS`, default 6). Delivery is **at least once**: de-duplicate on `X-DocUMud-Delivery`. Redirects are not followed. Webhooks are sent by the worker, so it must be running.

## Verify the signature

The signature is `HMAC-SHA256(secret, "{timestamp}.{raw request body}")`. Verify it against the **raw bytes**, and reject old timestamps to stop replays.

```python
import hmac, hashlib, time

def verify(secret: str, headers, raw_body: bytes, tolerance: int = 300) -> bool:
    ts = headers["X-DocUMud-Timestamp"]
    if abs(time.time() - int(ts)) > tolerance:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-DocUMud-Signature"])
```

```js
// Node (Express with express.raw({type: "application/json"}))
const crypto = require("crypto");
function verify(secret, headers, rawBody, tolerance = 300) {
  const ts = headers["x-documud-timestamp"];
  if (Math.abs(Date.now() / 1000 - Number(ts)) > tolerance) return false;
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(`${ts}.`).update(rawBody).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(headers["x-documud-signature"]));
}
```

## A typical handler

1. Verify the signature and timestamp.
2. Skip if you've already processed this `X-DocUMud-Delivery`.
3. On `envelope.completed`: look the envelope up by `data.external_id` (your reference), `GET /v1/envelopes/{envelope_id}/document` with your API key, store the PDF and its `final_sha256`.
4. Return `200`.
