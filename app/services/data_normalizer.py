"""Turn whatever data an integrator sends into values for template fields.

Accepted shapes (all handled by the same code path):
  {"client_name": "Asha"}                              flat object
  {"client": {"name": "Asha", "pan": "ABCDE1234F"}}    nested object   -> keys like client.name
  [{"name": "client_name", "value": "Asha"}]           form-style list of name/value pairs
  {"Client Name": "Asha"}                              labels, any case/spacing/punctuation
A field is looked up by, in order: an explicit field_map entry, its exact name, its aliases, its label,
then the last segment of a nested path (client.name -> "name") when that is unambiguous."""
import re
from typing import Any

from app.services.field_types import BY_ID, FieldError, coerce


def norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def to_pairs(data: Any) -> Any:
    if isinstance(data, list) and data and all(isinstance(i, dict) for i in data):
        out = {}
        for i in data:
            k = next((i[c] for c in ("name", "id", "key", "field") if c in i), None)
            if k is not None:
                out[str(k)] = i.get("value")
        return out
    return data


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """{'a': {'b': [1, 2]}} -> {'a.b[0]': 1, 'a.b[1]': 2}; leaves only."""
    flat: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flat.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flat.update(flatten(v, f"{prefix}[{i}]"))
    else:
        flat[prefix] = obj
    return flat


def _by_path(data: Any, path: str) -> Any:
    cur = data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            cur = cur[int(part[1:-1])]
        else:
            cur = cur[part]
    return cur


def resolve(data: Any, fields: list, field_map: dict[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Return ({field name: raw value}, warnings). `fields` are field-like objects with name, label, aliases."""
    data = to_pairs(data)
    if data is None:
        return {}, []
    if not isinstance(data, (dict, list)):
        raise FieldError("data must be an object or a list of name/value pairs")
    flat = flatten(data)
    full: dict[str, str] = {}        # normalised full path -> original path
    last: dict[str, list[str]] = {}  # normalised last segment -> original paths
    for path in flat:
        full.setdefault(norm(path), path)
        seg = re.split(r"[.\[]", path.rstrip("]"))
        last.setdefault(norm(next((s for s in reversed(seg) if s and not s.isdigit()), path)), []).append(path)

    values, used, warnings = {}, set(), []
    for f in fields:
        if f.name in values:
            continue
        keys = [f.name, *json_list(getattr(f, "aliases", "[]")), f.label]
        path = None
        if field_map and f.name in field_map:
            try:
                values[f.name] = _by_path(data, field_map[f.name])
                used.add(field_map[f.name])
            except (KeyError, IndexError, TypeError):
                warnings.append(f"field_map: '{field_map[f.name]}' not found in data (for '{f.name}')")
            continue
        for k in filter(None, keys):
            path = full.get(norm(k))
            if path:
                break
        if path is None:
            for k in filter(None, keys):
                cands = last.get(norm(k), [])
                if len(cands) == 1:
                    path = cands[0]
                    break
                if len(cands) > 1:
                    pick = min(cands, key=len)
                    warnings.append(f"'{k}' matched several keys ({', '.join(sorted(cands))}); used '{pick}'")
                    path = pick
                    break
        if path is not None:
            values[f.name] = flat[path]
            used.add(path)
    ignored = [p for p in flat if p not in used]
    if ignored:
        warnings.append("ignored keys that match no field: " + ", ".join(sorted(ignored)[:20]) + (" …" if len(ignored) > 20 else ""))
    return values, warnings


def json_list(raw: str) -> list:
    import json
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def derive_names(values: dict[str, Any], fields: list) -> None:
    """Fill first/last/full name fields from whichever the caller supplied."""
    by_type = {}
    for f in fields:
        by_type.setdefault(f.type, f.name)
    full, first, last = (by_type.get(t) for t in ("full_name", "first_name", "last_name"))
    have = lambda n: n and str(values.get(n) or "").strip()
    if full and have(full):
        parts = str(values[full]).split()
        if first and not have(first) and parts:
            values[first] = parts[0]
        if last and not have(last) and len(parts) > 1:
            values[last] = parts[-1]
    elif full and (have(first) or have(last)):
        values[full] = " ".join(p for p in (str(values.get(first) or ""), str(values.get(last) or "")) if p.strip())


def coerce_values(raw: dict[str, Any], fields: list) -> tuple[dict[str, str], dict[str, str]]:
    """Validate every supplied value against its field. Returns (clean values, {field: error})."""
    clean, errors = {}, {}
    for f in fields:
        if f.name not in raw or f.type in ("signature", "initials"):
            continue
        t = BY_ID.get(f.type)
        try:
            if f.type == "radio":
                continue                      # handled per group below
            clean[f.name] = coerce(f.type, raw[f.name], options=json_list(getattr(f, "options", "[]")),
                                   validation=_json(getattr(f, "validation", "{}")))
        except FieldError as e:
            errors[f.name] = str(e)
    for f in fields:                          # radio groups: the group's value is the chosen option
        if f.type == "radio" and f.name in raw and f.name not in clean:
            choices = [g.option for g in fields if g.type == "radio" and g.name == f.name and g.option]
            try:
                clean[f.name] = coerce("radio", raw[f.name], options=choices)
            except FieldError as e:
                errors[f.name] = str(e)
    return clean, errors


def _json(raw: str) -> dict:
    import json
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}
