import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(Text)  # relative to storage_root
    file_hash: Mapped[str] = mapped_column(String(64))
    pages: Mapped[int] = mapped_column(Integer, default=1)
    # uploaded | scanned | signed | tagged
    source: Mapped[str] = mapped_column(String(30), default="uploaded")
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Job(Base):
    """Background accessibility-tagging job."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    service: Mapped[str] = mapped_column(String(40), default="pdf_tagging")
    # queued | processing | completed | failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class SignatureRequest(Base):
    __tablename__ = "signature_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # email | in_person
    mode: Mapped[str] = mapped_column(String(20))
    recipient_name: Mapped[str] = mapped_column(String(120), default="")
    recipient_email: Mapped[str] = mapped_column(String(255), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # pending | signing | signed | cancelled | expired
    status: Mapped[str] = mapped_column(String(20), default="pending")
    document_hash: Mapped[str] = mapped_column(String(64))
    signed_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )
    signed_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SignatureEvent(Base):
    """Append-only, hash-chained audit trail per signature request."""

    __tablename__ = "signature_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(ForeignKey("signature_requests.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(50))
    details: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DocumentTag(Base):
    """A labelled region on a document page (coordinates normalised 0-1, top-left origin)."""

    __tablename__ = "document_tags"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    label: Mapped[str] = mapped_column(String(80))
    value: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ApiKey(Base):
    """Integration credential. Only the SHA-256 of the key is stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(12))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    secret: Mapped[str] = mapped_column(String(64))  # HMAC key; shown once at creation
    events: Mapped[str] = mapped_column(Text)  # comma-separated
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WebhookDelivery(Base):
    """Outbox row: written in the same transaction as the event, sent by the worker."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhooks.id"), index=True)
    event: Mapped[str] = mapped_column(String(50))
    payload: Mapped[str] = mapped_column(Text)
    # pending | delivered | failed
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentText(Base):
    """OCR output per page, stored against the searchable (result) document."""

    __tablename__ = "document_texts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)


class UserFont(Base):
    """A user-uploaded font for typed signatures. Private to its owner."""

    __tablename__ = "user_fonts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    file_path: Mapped[str] = mapped_column(Text)  # relative to storage_root
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Dataset(Base):
    """An uploaded client sheet (XLSX/CSV). Rows live in a JSON file under storage/datasets."""

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    columns: Mapped[str] = mapped_column(Text)  # JSON list[str]
    row_count: Mapped[int] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(Text)  # relative to storage_root
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FillTemplate(Base):
    """Saved fill settings for a tagged document: font/style, tag-to-column mapping, last dataset."""

    __tablename__ = "fill_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    font: Mapped[str] = mapped_column(String(80), default="bundled:Lato")  # bundled:<name> | user:<id>
    color: Mapped[str] = mapped_column(String(7), default="#111111")
    align: Mapped[str] = mapped_column(String(8), default="left")
    max_font_size: Mapped[int] = mapped_column(Integer, default=0)  # 0 = fit to box height
    mapping: Mapped[str] = mapped_column(Text, default="{}")  # JSON {tag label: column | null}
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"), nullable=True)
    filename_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SmtpConfig(Base):
    """A user's own outgoing mail account. The password is stored encrypted."""

    __tablename__ = "smtp_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=587)
    security: Mapped[str] = mapped_column(String(8), default="starttls")  # starttls | ssl | none
    username: Mapped[str] = mapped_column(String(255), default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    from_email: Mapped[str] = mapped_column(String(255))
    from_name: Mapped[str] = mapped_column(String(120), default="")
    daily_limit: Mapped[int] = mapped_column(Integer, default=400)  # 0 = unlimited
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class EmailOutbox(Base):
    """Signature-related emails waiting to be sent by the worker. Bodies are rendered at send time."""

    __tablename__ = "email_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)  # sender whose mailbox is used
    request_id: Mapped[str] = mapped_column(ForeignKey("signature_requests.id"), index=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("bulk_batches.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20))  # invite | reminder | signed_sender | signed_signer
    to_email: Mapped[str] = mapped_column(String(255))
    # pending | sent | failed | not_configured | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BulkBatch(Base):
    __tablename__ = "bulk_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    personalize: Mapped[bool] = mapped_column(Boolean, default=False)
    subject: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    expires_days: Mapped[int] = mapped_column(Integer, default=7)
    total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BulkItem(Base):
    __tablename__ = "bulk_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("bulk_batches.id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("signature_requests.id"), unique=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))  # the copy this person signs
    row_index: Mapped[int] = mapped_column(Integer)  # 1-based row in the data sheet
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255))


class SignerSession(Base):
    """One emailed verification code, and the access token issued once it is entered correctly."""

    __tablename__ = "signer_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(ForeignKey("signature_requests.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    code_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    access_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# ======================= Templates & envelopes =======================
# A Template is designed once by the office: a base PDF plus typed fields (page coordinates are
# fractions 0-1, top-left origin, pages are 1-based) assigned to roles. An Envelope is one use of a
# template: the document, a frozen copy of the fields, the data, the recipients and the audit trail.


class Template(Base):
    __tablename__ = "templates"
    __table_args__ = (Index("ix_templates_user_status", "user_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | archived
    roles: Mapped[str] = mapped_column(Text, default="[]")      # JSON [{id,label,kind,order}]
    defaults: Mapped[str] = mapped_column(Text, default="{}")   # JSON default style for text fields
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class TemplateField(Base):
    __tablename__ = "template_fields"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    template_id: Mapped[str] = mapped_column(ForeignKey("templates.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))          # the key used in API data
    label: Mapped[str] = mapped_column(String(120), default="")
    type: Mapped[str] = mapped_column(String(30))
    role: Mapped[str] = mapped_column(String(40), default="office")
    page: Mapped[int] = mapped_column(Integer, default=1)  # 1-based
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    default_value: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[str] = mapped_column(Text, default="[]")      # JSON list (dropdown / radio choices)
    option: Mapped[str] = mapped_column(String(120), default="")  # radio: the choice this box represents
    aliases: Mapped[str] = mapped_column(Text, default="[]")      # JSON list of alternative data keys
    style: Mapped[str] = mapped_column(Text, default="{}")        # JSON
    validation: Mapped[str] = mapped_column(Text, default="{}")   # JSON
    position: Mapped[int] = mapped_column(Integer, default=0)


class Envelope(Base):
    __tablename__ = "envelopes"
    __table_args__ = (
        Index("ix_envelopes_user_status_created", "user_id", "status", "created_at"),
        Index("ix_envelopes_user_created", "user_id", "created_at"),
        UniqueConstraint("user_id", "external_id", name="uq_envelopes_user_external"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    template_id: Mapped[str | None] = mapped_column(ForeignKey("templates.id"), nullable=True, index=True)
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    name: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text, default="")
    # draft | in_progress | completed | declined | cancelled | expired
    status: Mapped[str] = mapped_column(String(14), default="draft")
    signing_order: Mapped[str] = mapped_column(String(10), default="sequential")  # sequential | parallel
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    return_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    final_document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    final_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rev: Mapped[int] = mapped_column(Integer, default=0)  # bumps on every change; keys render caches
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EnvelopeRecipient(Base):
    __tablename__ = "envelope_recipients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    envelope_id: Mapped[str] = mapped_column(ForeignKey("envelopes.id"), index=True)
    role: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(10), default="signer")  # office | signer | cc
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    order: Mapped[int] = mapped_column(Integer, default=1)
    # waiting (not their turn) | pending | viewed | signed | declined
    status: Mapped[str] = mapped_column(String(10), default="pending")
    deliver: Mapped[str] = mapped_column(String(6), default="email")  # email | none (embedded signing)
    verify: Mapped[bool] = mapped_column(Boolean, default=True)       # require an emailed code before signing
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    declined_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    signer_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EnvelopeField(Base):
    """Frozen copy of a template field for one envelope, plus its value."""

    __tablename__ = "envelope_fields"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    envelope_id: Mapped[str] = mapped_column(ForeignKey("envelopes.id"), index=True)
    recipient_id: Mapped[str | None] = mapped_column(ForeignKey("envelope_recipients.id"), nullable=True, index=True)
    template_field_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(120), default="")
    type: Mapped[str] = mapped_column(String(30))
    role: Mapped[str] = mapped_column(String(40))
    page: Mapped[int] = mapped_column(Integer, default=1)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    options: Mapped[str] = mapped_column(Text, default="[]")
    option: Mapped[str] = mapped_column(String(120), default="")
    style: Mapped[str] = mapped_column(Text, default="{}")
    validation: Mapped[str] = mapped_column(Text, default="{}")
    value: Mapped[str] = mapped_column(Text, default="")
    sig_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # signature / initials PNG
    locked: Mapped[bool] = mapped_column(Boolean, default=False)       # pre-filled by the sender: signer can't edit
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EnvelopeEvent(Base):
    """Append-only, hash-chained audit trail per envelope."""

    __tablename__ = "envelope_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    envelope_id: Mapped[str] = mapped_column(ForeignKey("envelopes.id"), index=True)
    recipient_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40))
    details: Mapped[str] = mapped_column(Text, default="")
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RecipientSession(Base):
    """Emailed verification code for an envelope recipient, and the access token issued once verified."""

    __tablename__ = "recipient_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recipient_id: Mapped[str] = mapped_column(ForeignKey("envelope_recipients.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    code_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    access_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EnvelopeMail(Base):
    """Outbox for envelope emails (invites, reminders, completed copies), sent by the worker."""

    __tablename__ = "envelope_mail"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)  # sender whose mailbox is used
    envelope_id: Mapped[str] = mapped_column(ForeignKey("envelopes.id"), index=True)
    recipient_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(12))  # invite | reminder | completed
    to_email: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyKey(Base):
    """Makes POST /v1/envelopes safe to retry: the same key returns the same envelope."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idem_user_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    envelope_id: Mapped[str] = mapped_column(ForeignKey("envelopes.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SignatureProfile(Base):
    """The office's saved signature/initials, applied automatically to office fields."""

    __tablename__ = "signature_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True)
    signature_path: Mapped[str] = mapped_column(Text)
    initials_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class EnvelopeBatch(Base):
    """A bulk request: items live in a JSON file, the worker creates envelopes from them in chunks."""

    __tablename__ = "envelope_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("templates.id"))
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)  # queued | processing | completed
    total: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    options: Mapped[str] = mapped_column(Text, default="{}")  # JSON shared options for every item
    payload_path: Mapped[str] = mapped_column(Text)           # relative to storage_root
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
