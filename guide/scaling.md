# Scaling & limits

This page is deliberately specific: what is built into the code, what was measured, what will limit you, and what to change for very high volume. Nothing here is a promise about your workload; measure yours.

## Built for volume

| Concern | What the code does |
|---|---|
| **Large lists** | Cursor pagination everywhere (no `OFFSET`), composite indexes on `(user, status, created_at)` and `(user, created_at)`, a unique index on `(user, external_id)` for exact lookups, and a one-query stats endpoint. Time to fetch a page does not grow with depth. |
| **Retries are safe** | `Idempotency-Key` on envelope creation; unique `external_id`; webhook deliveries carry unique ids. |
| **Don't block the request** | `POST /v1/envelopes/batch` returns immediately; the worker creates envelopes in chunks, each item independent. Emails and webhooks are queued in outbox tables and sent by the worker with retries and backoff. |
| **Files** | Every stored PDF is sharded (`results/ab/cd/<id>.pdf`) so no directory ever holds millions of files. Fonts are subset when embedding (a Lato-filled PDF is tens of KB, not hundreds). |
| **Pages on demand** | The signing page renders one page at a time from the source PDF when it is requested; nothing is pre-rendered. A 50-page document costs no more to sign than a 1-page one. |
| **Integrity** | Source PDFs are hash-checked before use; each envelope has a hash-chained audit trail; the final PDF's SHA-256 is recorded. |
| **Isolation** | Every query is scoped to the account; other accounts' objects return `404`. |

## Measured

Development laptop, **SQLite**, one process, calls made in-process (no network), no email sending, 1-page template with 4 fields. Treat as a baseline for relative comparison, not a capacity claim.

| What | Result |
|---|---|
| Create + send one envelope (`POST /v1/envelopes`) | ~29 per second sequentially (median 31 ms, p95 55 ms) |
| Same via the batch worker | ~45 per second |
| **Full lifecycle**: create, recipient signs, final PDF assembled | **~85-100 ms median, ~10 per second per process**; essentially the same for 1, 10 and 50-page documents |
| List first page with **50,000** envelopes | ~19 ms |
| Deep page via cursor (50,000 rows) | ~23 ms |
| Filter by status (50,000 rows) | ~20 ms |
| Status counts (50,000 rows) | ~15 ms |
| Substring search `q` (50,000 rows) | ~100 ms: it **scans**; use the indexed `external_id` filter for lookups |

## What will limit you first

1. **SQLite** allows one writer at a time. It is fine for a single server and moderate volume, and it is the first thing to replace for high volume (see below).
2. **Local disk storage.** Files live under `storage/` on one machine. Multiple servers need shared storage or an object store; the paths are already relative and sharded, but there is no S3 adapter yet.
3. **Rate-limit counters are in process memory**, so with several server processes each enforces its own limit, and they reset on restart.
4. **The worker polls the database.** It is simple and reliable, and one worker processes envelopes, emails and webhooks in sequence. Run more only after moving to a database with row-level locking (the current claim logic assumes a single worker).
5. **The final PDF is assembled inside the request** of the last signer. It is quick (tens of ms) but is CPU work on the web tier.
6. **Email is limited by the sender's own mailbox.** Emails go through each customer's SMTP account; Gmail allows roughly 500 a day (2,000 on Workspace). The per-account daily limit makes extra mail wait instead of failing. For volume, use a transactional provider (SendGrid, SES, Brevo… configured in **Email settings**) or embedded signing (`deliver: "none"`).
7. **Substring search** (`q`) scans. Fine to tens of thousands of rows, then use `external_id`, filters, or add a search index.

## Back-of-envelope for 1 million envelopes a day

1M/day is ~12 envelopes per second on average; plan for a peak of 5-10× (60-120/s). With ~10 full lifecycles per second per process (measured above), that is on the order of **6-12 CPU cores** of API capacity at peak, plus the worker(s) for email and webhooks, and a database that sustains the write rate. This is arithmetic from one laptop measurement: validate it with a load test against your own infrastructure before committing.

## Recommended architecture for high volume

| Layer | Change |
|---|---|
| Database | **PostgreSQL** (set `DATABASE_URL`; SQLAlchemy models are portable) behind a connection pooler. Partition `envelopes`, `envelope_events`, `envelope_fields` by month. Add a trigram index if you need fast substring search. Add Alembic migrations before the first production deployment. |
| Files | An **object store** (S3/GCS) with a CDN for downloads. Implement a storage adapter behind `app/services/storage.py` (all access goes through `resolve()` / `new_path()`). |
| Web tier | Several stateless uvicorn/gunicorn workers behind a load balancer. Move rate limiting to Redis. |
| Background work | Move the outbox/batch polling to a real queue (Redis streams, SQS, RabbitMQ) with many workers; use `SELECT … FOR UPDATE SKIP LOCKED` for claiming. |
| Final assembly | Make finalization an async job so signers aren't waiting on it for very large documents. |
| Operations | Metrics and tracing, structured logs, backups with restore drills, a retention/archival policy (completed envelopes older than N years to cold storage). |

## Not built yet (be aware)

- PostgreSQL and S3 adapters, schema migrations (Alembic).
- Multi-user organisations, roles/permissions, SSO/SAML, per-key scopes.
- Retention and legal-hold policies, data export/erasure tooling.
- Certificate-backed digital signatures (PAdES) and qualified e-signatures; signatures here are electronic signatures with an audit trail.
- Conditional fields, formulas, attachments, several documents per envelope.
- Async finalization and a queue-based worker pool.
