# DocUMud developer guide

DocUMud turns a PDF into a reusable **template**, fills it from your data, and collects signatures from the **office** (you) and anyone else, with a full audit trail. Everything the screens do is available through the API.

## The model in one picture

```
 PDF ──► TEMPLATE ───────────────┐
         fields placed by the    │   data (JSON in any shape)
         office: names, amounts, ├──► ENVELOPE ──► recipients sign ──► final PDF
         dates, checkboxes,      │    one use of    in order or       + certificate
         signatures, …           │    a template    in parallel       + audit trail
         assigned to ROLES ──────┘
```

| Concept | What it is |
|---|---|
| **Template** | A PDF plus typed **fields** (name, amount, checkbox, signature, …) placed by the office and assigned to **roles**. Designed once, reused for every envelope. |
| **Role** | A participant in the signing process, e.g. `office` (you, the account owner) and `client`. Fields belong to a role. |
| **Envelope** | One use of a template: the document, a frozen copy of the fields, the data, the recipients and the audit trail. |
| **Recipient** | A person filling a role in an envelope. Gets a `signing_url`. |
| **Data** | Values for the fields, sent as JSON in any shape (flat, nested, name/value pairs). Matched to fields by name, label or alias. |

## Where to start

1. **[Quickstart](/guide/quickstart)**: send your first envelope with `curl` in five minutes.
2. **[Templates & fields](/guide/templates)**: every field type, styling, validation, roles, and placing fields by anchor text instead of coordinates.
3. **[Sending data](/guide/data)**: how any JSON shape is matched, validated and reported.
4. **[API reference](/guide/api)**: every endpoint, state machine, embedded signing, batches, errors, idempotency.
5. **[Webhooks](/guide/webhooks)**: events, payloads, signature verification.
6. **[Scaling & limits](/guide/scaling)**: measured numbers, what to change for very high volume, and what is not built yet.

The interactive, always-current endpoint reference is at [`/docs`](/docs).

## Two ways to work

- **In the app**: *Templates* to design, *Envelopes* to send and track, *Email settings* to connect your own mailbox.
- **Through the API**: authenticate with an API key (Dashboard → Developers) and send `X-API-Key: dm_…` on every request.

> Signatures are **electronic signatures with an audit trail** (who, when, from which IP, document hashes, optional email-code verification). They are not certificate-backed digital signatures (PAdES) or qualified electronic signatures.
