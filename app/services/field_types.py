"""Field type registry: what the office can place on a template, and how values are validated,
normalised and displayed. Adding a type here makes it available in the designer, the API and the docs."""
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from email_validator import EmailNotValidError, validate_email


class FieldError(ValueError):
    """A value that doesn't suit its field; the message is safe to show to API users."""


@dataclass(frozen=True)
class FieldType:
    id: str
    label: str
    category: str
    kind: str            # text | choice | signature | auto
    w: float             # default size as a fraction of the page
    h: float
    multiline: bool = False
    has_options: bool = False
    desc: str = ""


CATEGORIES = ["People", "Text", "Numbers", "Dates", "Choices", "Signing"]

_T = FieldType
TYPES: list[FieldType] = [
    _T("full_name", "Full name", "People", "text", .30, .026, desc="A person's full name"),
    _T("first_name", "First name", "People", "text", .20, .026, desc="Given name; derived from a full name if only that is supplied"),
    _T("last_name", "Last name", "People", "text", .20, .026, desc="Family name; derived from a full name if only that is supplied"),
    _T("email", "Email", "People", "text", .30, .026, desc="Validated email address"),
    _T("phone", "Phone", "People", "text", .22, .026, desc="7-15 digits, optional leading +"),
    _T("company", "Company", "People", "text", .30, .026, desc="Organisation name"),
    _T("job_title", "Job title", "People", "text", .25, .026),
    _T("address", "Address", "People", "text", .40, .060, multiline=True, desc="Multi-line address"),
    _T("text", "Text", "Text", "text", .30, .026, desc="Single line of free text"),
    _T("description", "Description", "Text", "text", .50, .100, multiline=True, desc="Multi-line text that wraps inside its box"),
    _T("id_number", "ID number", "Text", "text", .22, .026, desc="PAN, passport, policy or any identifier; add a pattern to enforce its format"),
    _T("number", "Number", "Numbers", "text", .15, .026, desc="Plain number"),
    _T("amount", "Amount", "Numbers", "text", .18, .026, desc="Money: currency symbol, decimals and lakh/million grouping are styleable"),
    _T("percentage", "Percentage", "Numbers", "text", .10, .026),
    _T("date", "Date", "Dates", "text", .18, .026, desc="Any common date format in, one display format out"),
    _T("date_signed", "Date signed", "Dates", "auto", .18, .026, desc="Filled automatically when the recipient signs"),
    _T("checkbox", "Checkbox", "Choices", "choice", .022, .016, desc="true/false"),
    _T("radio", "Radio option", "Choices", "choice", .022, .016, has_options=True, desc="One box per choice; boxes sharing a name form a group"),
    _T("dropdown", "Dropdown", "Choices", "choice", .25, .026, has_options=True, desc="One value from a list"),
    _T("signature", "Signature", "Signing", "signature", .25, .060, desc="Drawn, typed or uploaded by the signer, or applied from the office's saved signature"),
    _T("initials", "Initials", "Signing", "signature", .10, .045),
]
BY_ID = {t.id: t for t in TYPES}


def catalog() -> list[dict]:
    return [{"id": t.id, "label": t.label, "category": t.category, "kind": t.kind, "default_width": t.w,
             "default_height": t.h, "multiline": t.multiline, "has_options": t.has_options, "description": t.desc}
            for t in TYPES]


# ---------------- validation / normalisation ----------------

_TRUE = {"true", "yes", "y", "1", "on", "checked", "x", "✓", "t"}
_FALSE = {"false", "no", "n", "0", "off", "unchecked", "", "f"}
_DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
                 "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%y"]


def _text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    return re.sub(r"[ \t]+", " ", str(raw).replace("\r\n", "\n")).strip()


def _decimal(raw: Any) -> Decimal:
    text = _text(raw).replace(",", "").replace(" ", "")
    text = re.sub(r"^[^\d\-+.]+", "", text)          # leading currency symbol
    try:
        return Decimal(text)
    except InvalidOperation:
        raise FieldError(f"'{_text(raw)}' is not a valid number")


def parse_date(raw: Any) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = _text(raw)
    if re.match(r"^\d{4}-\d{2}-\d{2}T", text):
        text = text[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise FieldError(f"'{text}' is not a recognised date (use YYYY-MM-DD or DD/MM/YYYY)")


def coerce(ftype: str, raw: Any, *, options: list[str] | None = None, validation: dict | None = None) -> str:
    """Return the canonical stored value, or raise FieldError. Empty input returns ''."""
    v = validation or {}
    text = _text(raw)
    if ftype in ("signature", "initials"):
        raise FieldError("Signatures can't be supplied as data")
    if ftype == "checkbox":
        low = text.lower()
        if low in _TRUE:
            return "true"
        if low in _FALSE:
            return "false"
        raise FieldError(f"'{text}' is not true/false")
    if not text:
        return ""
    if ftype == "email":
        try:
            return validate_email(text, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            raise FieldError(f"'{text}' is not a valid email address")
    elif ftype == "phone":
        digits = re.sub(r"[\s\-().]", "", text)
        if not re.fullmatch(r"\+?\d{7,15}", digits):
            raise FieldError(f"'{text}' is not a valid phone number")
        text = digits
    elif ftype in ("number", "amount", "percentage"):
        d = _decimal(text)
        if "min" in v and d < Decimal(str(v["min"])):
            raise FieldError(f"must be at least {v['min']}")
        if "max" in v and d > Decimal(str(v["max"])):
            raise FieldError(f"must be at most {v['max']}")
        text = format(d.normalize(), "f") if d == d.to_integral() else format(d, "f")
        text = text.rstrip("0").rstrip(".") if "." in text else text
    elif ftype in ("date", "date_signed"):
        text = parse_date(text).isoformat()
    elif ftype in ("dropdown", "radio"):
        choices = options or []
        match = next((o for o in choices if o.casefold() == text.casefold()), None)
        if choices and match is None:
            raise FieldError(f"'{text}' is not one of: {', '.join(choices)}")
        text = match or text
    if "min_length" in v and len(text) < int(v["min_length"]):
        raise FieldError(f"must be at least {v['min_length']} characters")
    if "max_length" in v and len(text) > int(v["max_length"]):
        raise FieldError(f"must be at most {v['max_length']} characters")
    if v.get("pattern") and not re.fullmatch(str(v["pattern"]), text):
        raise FieldError(v.get("pattern_message") or f"'{text}' doesn't match the required format")
    if len(text) > 4000:
        raise FieldError("is too long (max 4000 characters)")
    return text


# ---------------- display formatting ----------------

def _group(int_part: str, grouping: str) -> str:
    if grouping == "none" or len(int_part) <= 3:
        return int_part
    if grouping == "indian":
        head, tail = int_part[:-3], int_part[-3:]
        head = re.sub(r"(?<=\d)(?=(\d{2})+$)", ",", head)
        return f"{head},{tail}"
    return re.sub(r"(?<=\d)(?=(\d{3})+$)", ",", int_part)


def format_number(value: str, *, decimals: int | None = None, grouping: str = "intl") -> str:
    d = Decimal(value)
    if decimals is not None:
        d = round(d, decimals)
    text = format(d, f".{decimals}f" if decimals is not None else "f")
    sign = "-" if text.startswith("-") else ""
    int_part, _, frac = text.lstrip("-").partition(".")
    return f"{sign}{_group(int_part, grouping)}{'.' + frac if frac else ''}"


def display(ftype: str, value: str, style: dict | None = None) -> str:
    """The text printed on the PDF for a stored value."""
    s = style or {}
    if not value:
        return ""
    try:
        if ftype in ("date", "date_signed"):
            return date.fromisoformat(value).strftime(s.get("date_format") or "%d %b %Y")
        if ftype == "amount":
            body = format_number(value, decimals=int(s.get("decimals", 2)), grouping=s.get("grouping", "intl"))
            cur = s.get("currency", "")
            return f"{body} {cur}".strip() if s.get("currency_position") == "suffix" else f"{cur}{body}"
        if ftype == "number":
            dec = s.get("decimals")
            return format_number(value, decimals=int(dec) if dec is not None else None, grouping=s.get("grouping", "none"))
        if ftype == "percentage":
            return f"{format_number(value, decimals=s.get('decimals'), grouping='none')}%"
    except (InvalidOperation, ValueError):
        return value
    text = value.upper() if s.get("uppercase") else value
    return text
