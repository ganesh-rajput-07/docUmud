# Sending data in any format

You shouldn't have to reshape your data to fit DocUMud. Send what you have; DocUMud matches it to the template's fields, validates it, and tells you exactly what it did.

## Accepted shapes

All of these work with the same `data` property.

**Flat object**

```json
{"client_name": "Rita Shah", "fee": "1,25,000", "accepted": true}
```

**Nested object** (paths are matched by their last segment, or by the full path with punctuation ignored)

```json
{"customer": {"name": "Rita Shah", "tax_id": "ABCDE1234F"}, "invoice": {"total": 125000}}
```

**Form-style name/value pairs**

```json
[{"name": "client_name", "value": "Rita Shah"}, {"id": "fee", "value": 125000}]
```

(each item may use `name`, `id`, `key` or `field` for the key)

**Labels, any case or punctuation**

```json
{"Client Name": "Rita Shah", "FEE": 125000}
```

## How a field finds its value

For each field, in this order:

1. **`field_map`** entry, if you gave one: `{"client_name": "customer.full_name"}` (dot paths and `[0]` array indexes).
2. The field's **name**, ignoring case, spaces, dashes and underscores (`Client Name`, `client-name`, `clientName` all match `client_name`).
3. The field's **aliases**.
4. The field's **label**.
5. The **last segment** of a nested path (`customer.name` → `name`), if that is unambiguous. If several nested keys end the same way, the shortest path wins and a **warning** tells you.

Keys that matched nothing are listed in `warnings` (never an error), so extra data in your payload is harmless.

```json
{"warnings": ["ignored keys that match no field: internal_note, trace_id"]}
```

## Smart handling

| Input | Result |
|---|---|
| Template has `first_name` / `last_name` and you send only `full_name` | They are split from the full name. |
| You send `first_name` + `last_name` but the template has `full_name` | It is assembled. |
| `"₹ 1,25,000.50"`, `125000.5`, `"1,25,000.50"` for an `amount` | Stored as `125000.5`; printed per the field's style. |
| `"2026-03-05"`, `"05/03/2026"`, `"5 Mar 2026"`, `"March 5, 2026"`, `"2026-03-05T10:00:00Z"` for a `date` | Stored as `2026-03-05`. Slash/dash dates with a 4-digit year last are **day-first**. |
| `true`, `"yes"`, `"Y"`, `1`, `"on"`, `"x"` for a `checkbox` | Ticked. `false`, `"no"`, `0`, `""` → not ticked. |
| `"gold"` for a dropdown with option `Gold` | Matched case-insensitively and stored as `Gold`. |
| `"+91 98765-43210"` for a `phone` | `+919876543210` |
| A value for a `radio` group | Must be one of the group's options. |
| Signature fields | Can't be supplied as data. The office uses `office_auto_sign`; signers sign themselves. |

## Validation errors

Every problem is reported at once and **nothing is created** if any value is invalid:

```json
HTTP 422
{"detail": {
  "message": "The request has problems; nothing was created.",
  "errors": {
    "data.fee":  "'lots' is not a valid number",
    "data.plan": "'Bronze' is not one of: Silver, Gold",
    "data.pan":  "'abc' doesn't match the required format",
    "recipients.client": "A recipient is required for role 'client' (Client)."
  }}}
```

Missing values are **not** errors by default: a field the office didn't supply simply stays for the office to fill when it signs (and fields for external roles are left for the signer). Send `"strict": true` to require all required office fields up front.

## Try it without creating anything

```bash
curl -s $BASE/v1/templates/$TPL/check -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"data": {"customer": {"name": "Rita Shah"}, "fee": "lots"}, "field_map": {"pan": "customer.tax_id"}}'
```

```json
{"values": {"client_name": "Rita Shah"},
 "errors": {"fee": "'lots' is not a valid number"},
 "warnings": [],
 "unfilled_fields": ["pan", "plan"],
 "valid": false}
```

## Generate a schema for your side

`GET /v1/templates/{id}/schema` returns a JSON Schema (`required` = the office-role fields the template needs, `enum` for dropdown/radio, `format` for email/date, `pattern`/limits from validation) that you can use to validate payloads before sending or to generate forms and client types.

## Spreadsheets

In the app, **Envelopes → New envelope → Many at once** (and `POST /v1/envelopes/batch`) take one row per envelope: column headers are treated as data keys, so a column named `Client Name` fills the field `client_name` automatically.
