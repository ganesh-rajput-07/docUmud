"""Envelope lifecycle: create from a template + data, route between recipients, collect signatures,
assemble the final PDF, and keep a tamper-evident audit trail."""
import base64
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Document, Envelope, EnvelopeEvent, EnvelopeField, EnvelopeMail, EnvelopeRecipient, SignatureProfile,
    Template, TemplateField, User, aware, new_id, utc_now,
)
from app.services import data_normalizer as dn
from app.services import mailer
from app.services.field_types import BY_ID, FieldError, coerce
from app.services.fill_service import FillReport
from app.services.render import draw_fields, finish
from app.services.signature_images import validate_png
from app.services.storage import InvalidDocument, hash_file, new_path, register_pdf, resolve
from app.services.template_service import effective_style, font_resolver, jl
from app.services.webhook_service import emit

AUTO_TYPES = {"date_signed"}
SIGN_TYPES = {"signature", "initials"}
TERMINAL = {"completed", "declined", "cancelled", "expired"}


class EnvelopeError(Exception):
    def __init__(self, status: int, message: str, errors: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.errors = status, message, errors or {}


# ---------------- tokens ----------------

def recipient_token(recipient_id: str) -> str:
    mac = hmac.new(settings.secret_key.encode(), b"env-token:" + recipient_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def signing_url(recipient_id: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/e/{recipient_token(recipient_id)}"


# ---------------- audit trail ----------------

def _last_hash(db: Session, envelope_id: str) -> str:
    rows = db.scalars(select(EnvelopeEvent).where(EnvelopeEvent.envelope_id == envelope_id).order_by(EnvelopeEvent.created_at)).all()
    rows += [o for o in db.new if isinstance(o, EnvelopeEvent) and o.envelope_id == envelope_id]
    return rows[-1].event_hash if rows else "0" * 64


def add_event(db: Session, env: Envelope, event_type: str, details: str = "", recipient_id: str | None = None) -> None:
    prev, now = _last_hash(db, env.id), utc_now()
    digest = hashlib.sha256(f"{prev}|{env.id}|{event_type}|{details}|{now.isoformat()}".encode()).hexdigest()
    db.add(EnvelopeEvent(envelope_id=env.id, recipient_id=recipient_id, event_type=event_type, details=details,
                         prev_hash=prev, event_hash=digest, created_at=now))


def verify_chain(events: list[EnvelopeEvent], envelope_id: str) -> bool:
    prev = "0" * 64
    for e in events:
        ts = e.created_at.replace(tzinfo=timezone.utc).isoformat() if e.created_at.tzinfo is None else e.created_at.isoformat()
        expect = hashlib.sha256(f"{prev}|{envelope_id}|{e.event_type}|{e.details}|{ts}".encode()).hexdigest()
        if e.prev_hash != prev or not hmac.compare_digest(e.event_hash, expect):
            return False
        prev = e.event_hash
    return True


# ---------------- loading ----------------

def env_fields(db: Session, envelope_id: str, page: int | None = None) -> list[EnvelopeField]:
    q = select(EnvelopeField).where(EnvelopeField.envelope_id == envelope_id)
    if page is not None:
        q = q.where(EnvelopeField.page == page)
    return list(db.scalars(q.order_by(EnvelopeField.page, EnvelopeField.y, EnvelopeField.x)).all())


def env_recipients(db: Session, envelope_id: str) -> list[EnvelopeRecipient]:
    return list(db.scalars(select(EnvelopeRecipient).where(EnvelopeRecipient.envelope_id == envelope_id)
                           .order_by(EnvelopeRecipient.order, EnvelopeRecipient.id)).all())


def _source_path(db: Session, env: Envelope) -> Path:
    doc = db.get(Document, env.document_id)
    path = resolve(doc.file_path)
    if not path.is_file() or hash_file(path) != doc.file_hash:
        raise EnvelopeError(409, "The source document is missing or has changed since the template was made.")
    return path


def _is_open_field(f: EnvelopeField) -> bool:
    """A field its recipient still has to complete."""
    return not f.locked and f.type not in AUTO_TYPES and not (f.value or f.sig_path)


# ---------------- creation ----------------

def _save_png(env_id: str, name: str, data: bytes) -> str:
    validate_png(data)
    rel = f"signatures/{env_id[:2]}/{env_id}/{name}.png"
    path = resolve(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def _profile_image(db: Session, user_id: str, kind: str) -> bytes | None:
    prof = db.scalar(select(SignatureProfile).where(SignatureProfile.user_id == user_id))
    rel = prof and (prof.initials_path if kind == "initials" else prof.signature_path)
    if not rel:
        return None
    path = resolve(rel)
    return path.read_bytes() if path.is_file() else None


def create_envelope(db: Session, user: User, template: Template, *, recipients: list[dict], data, field_map: dict | None,
                    name: str | None, message: str, signing_order: str, expires_in_days: int, external_id: str | None,
                    metadata: dict, return_url: str | None, send: bool, office_auto_sign: bool, strict: bool
                    ) -> tuple[Envelope, list[str]]:
    """Create (and optionally send) an envelope. Raises EnvelopeError(422, ..., errors) for bad input."""
    roles = jl(template.roles, [])
    tfields = list(db.scalars(select(TemplateField).where(TemplateField.template_id == template.id)
                              .order_by(TemplateField.position, TemplateField.id)).all())
    if not tfields:
        raise EnvelopeError(422, "This template has no fields yet. Add fields in the designer or via the API first.")
    by_role = {r["id"]: r for r in roles}
    used_roles = {f.role for f in tfields}

    # ---- recipients: one per used role, plus optional cc ----
    given: dict[str, dict] = {}
    for r in recipients:
        role = r.get("role")
        if role in by_role:
            if role in given:
                raise EnvelopeError(422, f"Role '{role}' has more than one recipient.")
            given[role] = r
    errors = {}
    plan: list[dict] = []
    for role in roles:
        if role["id"] not in used_roles:
            continue
        if role["kind"] == "office":
            plan.append({"role": role["id"], "kind": "office", "name": user.name or user.email, "email": user.email,
                         "deliver": "none", "verify": False, "order": role["order"]})
            continue
        r = given.get(role["id"])
        if not r:
            errors[f"recipients.{role['id']}"] = f"A recipient is required for role '{role['id']}' ({role['label']})."
            continue
        email = (r.get("email") or "").strip().lower()
        deliver = r.get("deliver", "email")
        if deliver == "email" and not email:
            errors[f"recipients.{role['id']}.email"] = "Email is required unless deliver is 'none' (embedded signing)."
        if email:
            try:
                from email_validator import validate_email
                email = validate_email(email, check_deliverability=False).normalized.lower()
            except Exception:
                errors[f"recipients.{role['id']}.email"] = f"'{email}' is not a valid email address."
        plan.append({"role": role["id"], "kind": "signer", "name": (r.get("name") or "").strip()[:120], "email": email,
                     "deliver": deliver, "verify": bool(r.get("verify", True)), "order": int(r.get("order") or role["order"])})
    for r in recipients:
        if r.get("role") not in by_role:                      # cc: gets the completed copy
            if r.get("kind", "cc") != "cc" or not r.get("email"):
                errors[f"recipients.{r.get('role')}"] = f"Unknown role '{r.get('role')}'. Template roles: {', '.join(by_role)}; or use kind 'cc' with an email."
                continue
            plan.append({"role": r.get("role") or "cc", "kind": "cc", "name": (r.get("name") or "")[:120],
                         "email": r["email"].strip().lower(), "deliver": "email", "verify": False, "order": 999})

    # ---- data -> field values ----
    try:
        raw, warnings = dn.resolve(data, tfields, field_map)
    except FieldError as e:
        raise EnvelopeError(422, str(e))
    dn.derive_names(raw, tfields)
    clean, field_errors = dn.coerce_values(raw, tfields)
    errors.update({f"data.{k}": v for k, v in field_errors.items()})
    office_ids = {r["id"] for r in roles if r["kind"] == "office"}
    if strict:
        missing = sorted({f.name for f in tfields if f.role in office_ids and f.required and f.type not in SIGN_TYPES | AUTO_TYPES
                          and not clean.get(f.name) and not f.default_value})
        errors.update({f"data.{m}": "Required by the template (strict mode)." for m in missing})
    if errors:
        raise EnvelopeError(422, "The request has problems; nothing was created.", errors)

    now = utc_now()
    env = Envelope(
        user_id=user.id, template_id=template.id, template_version=template.version, document_id=template.document_id,
        name=(name or template.name)[:200], message=message[:2000], status="draft",
        signing_order=signing_order, external_id=external_id, metadata_json=json.dumps(metadata or {}),
        return_url=return_url, expires_at=now + timedelta(days=expires_in_days),
    )
    db.add(env)
    db.flush()
    recs: dict[str, EnvelopeRecipient] = {}
    for p in sorted(plan, key=lambda x: (x["order"], x["role"])):
        rid = new_id()
        rec = EnvelopeRecipient(id=rid, envelope_id=env.id, role=p["role"], kind=p["kind"], name=p["name"], email=p["email"],
                                order=p["order"], deliver=p["deliver"], verify=p["verify"],
                                token_hash=hash_token(recipient_token(rid)), status="waiting" if p["kind"] != "cc" else "pending")
        db.add(rec)
        recs[p["role"]] = rec
    db.flush()

    defaults = jl(template.defaults, {})
    for tf in tfields:
        value, locked = "", False
        if tf.type not in SIGN_TYPES | AUTO_TYPES:
            if tf.name in clean:
                value, locked = clean[tf.name], bool(clean[tf.name])
            elif tf.default_value:
                try:
                    value = coerce(tf.type, tf.default_value, options=jl(tf.options, []), validation=jl(tf.validation, {}))
                except FieldError:
                    value = tf.default_value
        if tf.type == "radio":                                # the group's value sits on every box
            value = clean.get(tf.name, value)
        db.add(EnvelopeField(
            envelope_id=env.id, recipient_id=recs[tf.role].id if tf.role in recs else None, template_field_id=tf.id,
            name=tf.name, label=tf.label, type=tf.type, role=tf.role, page=tf.page, x=tf.x, y=tf.y, w=tf.w, h=tf.h,
            required=tf.required, options=tf.options, option=tf.option,
            style=json.dumps(effective_style(defaults, tf.style)), validation=tf.validation, value=value, locked=locked))
    db.flush()
    add_event(db, env, "created", f"template={template.id}@v{template.version}; recipients={len(recs)}; by={user.email}")
    emit(db, user.id, "envelope.created", envelope_event_payload(env))

    if office_auto_sign:
        _auto_sign_office(db, env, user)
    if send:
        send_envelope(db, env, user)
    return env, warnings


def _auto_sign_office(db: Session, env: Envelope, user: User) -> None:
    office = next((r for r in env_recipients(db, env.id) if r.kind == "office"), None)
    if office is None:
        return
    mine = [f for f in env_fields(db, env.id) if f.recipient_id == office.id]
    missing_profile = [t for t in {f.type for f in mine if f.type in SIGN_TYPES} if _profile_image(db, user.id, t) is None]
    if missing_profile:
        raise EnvelopeError(422, "No saved office signature. Save one with PUT /v1/signature-profile or sign manually.",
                            {"office_auto_sign": "Missing a saved " + " and ".join(sorted(missing_profile))})
    for f in mine:
        if f.type in SIGN_TYPES:
            f.sig_path = _save_png(env.id, f"{office.id}-{f.type}", _profile_image(db, user.id, f.type))
            f.filled_at = utc_now()
        elif f.type == "date_signed":
            f.value, f.filled_at = date.today().isoformat(), utc_now()
    still_open = [f for f in mine if f.required and _is_open_field(f) and f.type not in AUTO_TYPES]
    if still_open:
        add_event(db, env, "auto_sign_partial", "office fields still need input: " + ", ".join(sorted({f.name for f in still_open})), office.id)
        return
    _mark_signed(db, env, office, ip="api", method="saved_signature")


# ---------------- routing ----------------

def active_recipients(recs: list[EnvelopeRecipient], order: str) -> list[EnvelopeRecipient]:
    waiting = [r for r in recs if r.kind != "cc" and r.status != "signed"]
    if order == "parallel":
        return waiting
    if not waiting:
        return []
    first = min(r.order for r in waiting)
    return [r for r in waiting if r.order == first]


def _idle(db: Session, env: Envelope, rec: EnvelopeRecipient) -> bool:
    """True when every field assigned to this recipient is already filled/locked: there is nothing for them to do."""
    mine = [f for f in env_fields(db, env.id) if f.recipient_id == rec.id]
    return not any(not f.locked and (f.type in AUTO_TYPES or not f.sig_path) for f in mine)


def advance(db: Session, env: Envelope, notify: bool = True) -> None:
    """Set who may sign now, email them, and finish the envelope when everyone has signed."""
    from app.services.envelope_mail import enqueue_mail

    if env.status in TERMINAL or env.status == "draft":
        return
    recs = env_recipients(db, env.id)
    live = active_recipients(recs, env.signing_order)
    for r in live:
        if _idle(db, env, r):                       # nothing to fill or sign: complete them and move on
            _mark_signed(db, env, r, ip="-", method="no_action_required")
            return                                  # _mark_signed re-enters advance()
    if not live:
        finalize(db, env)
        return
    for r in recs:
        if r.kind == "cc" or r.status in ("signed", "declined"):
            continue
        should = r in live
        if should and r.status == "waiting":
            r.status = "pending"
            if notify and r.deliver == "email" and r.email:
                enqueue_mail(db, env, r, "invite")
        elif not should:
            r.status = "waiting"
    env.rev += 1


def send_envelope(db: Session, env: Envelope, user: User) -> None:
    if env.status != "draft":
        raise EnvelopeError(409, f"Only draft envelopes can be sent (this one is {env.status}).")
    env.status, env.sent_at = "in_progress", utc_now()
    add_event(db, env, "sent", f"order={env.signing_order}; by={user.email}")
    for r in env_recipients(db, env.id):
        if r.kind != "cc" and r.status != "signed":
            r.status = "waiting"
    db.flush()
    emit(db, env.user_id, "envelope.sent", envelope_event_payload(env))
    advance(db, env)                                 # invites whoever's turn it is, or completes if nobody needs to act


def _mark_signed(db: Session, env: Envelope, rec: EnvelopeRecipient, ip: str, method: str = "signing_page") -> None:
    rec.status, rec.signed_at, rec.signer_ip = "signed", utc_now(), ip
    add_event(db, env, "signed", f"role={rec.role}; name={rec.name}; ip={ip}; method={method}", rec.id)
    emit(db, env.user_id, "recipient.signed", {**envelope_event_payload(env), "recipient": {
        "id": rec.id, "role": rec.role, "name": rec.name, "email": rec.email, "signed_at": rec.signed_at.isoformat()}})
    if env.status == "in_progress":
        advance(db, env)


# ---------------- recipient submission ----------------

def submit(db: Session, env: Envelope, rec: EnvelopeRecipient, values: dict, signature_png: bytes | None,
           initials_png: bytes | None, consent: bool, ip: str) -> None:
    if env.status != "in_progress":
        raise EnvelopeError(409, f"This envelope is {env.status}.")
    if rec.status not in ("pending", "viewed"):
        raise EnvelopeError(409, "It is not this recipient's turn to sign." if rec.status == "waiting" else f"Already {rec.status}.")
    if not consent:
        raise EnvelopeError(422, "Consent is required to sign electronically.", {"consent": "Please confirm you agree to sign electronically."})
    errors: dict[str, str] = {}
    mine = [f for f in env_fields(db, env.id) if f.recipient_id == rec.id]
    images = {"signature": signature_png, "initials": initials_png}
    for t, img in images.items():
        if img:
            try:
                validate_png(img)
            except InvalidDocument as e:
                errors[t] = str(e)
    today = date.today().isoformat()
    groups: dict[str, str] = {}
    for f in mine:
        if f.locked or f.type in AUTO_TYPES:
            continue
        if f.type in SIGN_TYPES:
            if f.sig_path:
                continue
            if images[f.type] is None:
                if f.required:
                    errors[f.name] = f"A {f.type} is required."
            continue
        raw = values.get(f.name)
        opts = jl(f.options, [])
        try:
            if f.type == "radio":
                if f.name in groups:
                    continue
                choices = [g.option for g in mine if g.type == "radio" and g.name == f.name]
                v = coerce("radio", raw, options=choices) if raw not in (None, "") else ""
                groups[f.name] = v
                if not v and f.required:
                    errors[f.name] = "Choose one option."
                continue
            v = coerce(f.type, raw, options=opts, validation=jl(f.validation, {}))
        except FieldError as e:
            errors[f.name] = str(e)
            continue
        if f.type == "checkbox":
            if f.required and v != "true":
                errors[f.name] = "This box must be ticked."
        elif f.required and not v:
            errors[f.name] = "This field is required."
        f.value = v
    if errors:
        raise EnvelopeError(422, "Please fix the highlighted fields.", errors)
    for f in mine:
        if f.type == "radio" and f.name in groups:
            f.value = groups[f.name]
        elif f.type in SIGN_TYPES and not f.sig_path and images[f.type]:
            f.sig_path = _save_png(env.id, f"{rec.id}-{f.type}", images[f.type])
        elif f.type == "date_signed":
            f.value = today
        if f.value or f.sig_path:
            f.filled_at = utc_now()
    if not rec.name:
        name_field = next((f.value for f in mine if f.type == "full_name" and f.value), "")
        rec.name = name_field[:120]
    env.rev += 1
    add_event(db, env, "consent", "agreed to sign electronically", rec.id)
    _mark_signed(db, env, rec, ip)


def decline(db: Session, env: Envelope, rec: EnvelopeRecipient, reason: str, ip: str) -> None:
    if env.status != "in_progress" or rec.status not in ("pending", "viewed"):
        raise EnvelopeError(409, "This request can no longer be declined.")
    rec.status, rec.declined_reason, rec.signer_ip = "declined", reason[:500], ip
    env.status = "declined"
    env.rev += 1
    add_event(db, env, "declined", f"role={rec.role}; reason={reason[:200]}; ip={ip}", rec.id)
    cancel_pending_mail(db, env.id)
    emit(db, env.user_id, "envelope.declined", {**envelope_event_payload(env), "reason": reason[:500],
                                                "recipient": {"id": rec.id, "role": rec.role, "email": rec.email}})


def cancel(db: Session, env: Envelope, by: str) -> None:
    if env.status in TERMINAL:
        raise EnvelopeError(409, f"This envelope is already {env.status}.")
    env.status = "cancelled"
    env.rev += 1
    add_event(db, env, "cancelled", f"by={by}")
    cancel_pending_mail(db, env.id)
    emit(db, env.user_id, "envelope.cancelled", envelope_event_payload(env))


def cancel_pending_mail(db: Session, envelope_id: str) -> None:
    for m in db.scalars(select(EnvelopeMail).where(EnvelopeMail.envelope_id == envelope_id, EnvelopeMail.status == "pending", EnvelopeMail.kind != "completed")):
        m.status, m.last_error = "cancelled", "Envelope is no longer active"


# ---------------- rendering / completion ----------------

def render_png(db: Session, env: Envelope, page: int, hide_ids: set[str] = frozenset()) -> bytes:
    src = _source_path(db, env)
    fields = [f for f in env_fields(db, env.id, page) if f.id not in hide_ids and (f.value or f.sig_path)]
    with pymupdf.open(src) as pdf:
        if not 1 <= page <= pdf.page_count:
            raise EnvelopeError(404, "Page out of range")
        draw_fields(pdf, fields, font_resolver(db, env.user_id, None), _image_loader(), FillReport())
        return pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes("png")


def _image_loader():
    def load(f):
        return resolve(f.sig_path).read_bytes() if f.sig_path else None
    return load


def finalize(db: Session, env: Envelope) -> None:
    from app.services.envelope_mail import enqueue_mail

    if env.status == "completed":
        return
    src = _source_path(db, env)
    fields = env_fields(db, env.id)
    with pymupdf.open(src) as pdf:
        report = draw_fields(pdf, fields, font_resolver(db, env.user_id, None), _image_loader(), FillReport())
        data = finish(pdf)
    out, rel = new_path("results")
    out.write_bytes(data)
    doc = db.get(Document, env.document_id)
    final = register_pdf(db, user_id=env.user_id, path=out, relative=rel, filename=f"{doc.filename.removesuffix('.pdf')}-completed.pdf",
                         source="envelope", parent_id=doc.id)
    env.final_document_id, env.final_hash = final.id, final.file_hash
    env.status, env.completed_at = "completed", utc_now()
    env.rev += 1
    add_event(db, env, "completed", f"final_sha256={final.file_hash}; overflow={report.overflow}")
    owner = db.get(User, env.user_id)
    sent_to = set()
    for r in env_recipients(db, env.id):
        if r.email and r.email not in sent_to:
            sent_to.add(r.email)
            enqueue_mail(db, env, r, "completed")
    if owner.email not in sent_to:
        enqueue_mail(db, env, None, "completed", to=owner.email)
    emit(db, env.user_id, "envelope.completed", {**envelope_event_payload(env), "final_document_id": final.id, "final_sha256": final.file_hash})


def expire_due(db: Session) -> int:
    n = 0
    for env in db.scalars(select(Envelope).where(Envelope.status.in_(("in_progress",)), Envelope.expires_at <= utc_now())).all():
        env.status = "expired"
        env.rev += 1
        add_event(db, env, "expired", "")
        cancel_pending_mail(db, env.id)
        emit(db, env.user_id, "envelope.expired", envelope_event_payload(env))
        n += 1
    db.commit()
    return n


# ---------------- serialisation ----------------

def envelope_event_payload(env: Envelope) -> dict:
    return {"envelope_id": env.id, "name": env.name, "status": env.status, "external_id": env.external_id,
            "template_id": env.template_id, "metadata": jl(env.metadata_json, {})}


def recipient_out(r: EnvelopeRecipient, env: Envelope) -> dict:
    live = env.status in ("draft", "in_progress") and r.kind != "cc" and r.status not in ("signed", "declined")
    return {"id": r.id, "role": r.role, "kind": r.kind, "name": r.name, "email": r.email, "order": r.order, "status": r.status,
            "deliver": r.deliver, "verify": r.verify, "signed_at": aware(r.signed_at).isoformat() if r.signed_at else None,
            "signing_url": signing_url(r.id) if live else None}


def envelope_out(db: Session, env: Envelope, *, with_fields: bool = False, warnings: list[str] | None = None) -> dict:
    recs = env_recipients(db, env.id)
    out = {
        "id": env.id, "name": env.name, "status": env.status, "template_id": env.template_id,
        "template_version": env.template_version, "signing_order": env.signing_order, "external_id": env.external_id,
        "metadata": jl(env.metadata_json, {}), "message": env.message, "return_url": env.return_url,
        "recipients": [recipient_out(r, env) for r in recs],
        "created_at": aware(env.created_at).isoformat(), "expires_at": aware(env.expires_at).isoformat(),
        "sent_at": aware(env.sent_at).isoformat() if env.sent_at else None,
        "completed_at": aware(env.completed_at).isoformat() if env.completed_at else None,
        "final_document_id": env.final_document_id, "final_sha256": env.final_hash,
        "document_url": f"/v1/envelopes/{env.id}/document",
    }
    if with_fields:
        out["fields"] = [{"id": f.id, "name": f.name, "label": f.label, "type": f.type, "role": f.role, "page": f.page,
                          "value": f.value, "signed": bool(f.sig_path), "required": f.required, "locked": f.locked,
                          "recipient_id": f.recipient_id} for f in env_fields(db, env.id)]
    if warnings:
        out["warnings"] = warnings
    return out
