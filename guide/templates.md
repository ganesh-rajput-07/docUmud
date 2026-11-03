# Templates & fields

A template is a PDF plus the fields the office wants on it. The **office** decides where every field goes, who fills it, how it looks, and what is valid. Signers never place anything: they fill in or sign the fields they were given.

You can design in the app (**Templates → New template → designer**) or define everything as JSON. The designer's **Copy JSON** button gives you the exact JSON for any template, and `PUT /v1/templates/{id}` takes it back.

## Template JSON

```json
{
  "name": "Engagement letter",
  "description": "Standard CA engagement",
  "roles": [
    {"id": "office", "kind": "office", "label": "Our firm",  "order": 1},
    {"id": "client", "kind": "signer", "label": "Client",    "order": 2}
  ],
  "defaults": {"font": "bundled:Lato", "font_size": 0, "color": "#111111"},
  "fields": [ /* see below */ ]
}
```

| Key | Notes |
|---|---|
| `roles` | Optional. Default: `office` (the account owner) and `signer`. At most one role may have `kind: "office"`. Role ids: lowercase letters, digits and `_`. `order` sets who signs first. |
| `defaults` | Font, size, colour and alignment used by text fields that don't set their own. `font_size: 0` means "fit the box". |
| `fields` | The field list. Replacing it with `PUT` bumps the template `version`; envelopes already created keep the fields they were made with. |

## Field definition

```json
{
  "name": "client_name",
  "label": "Client name",
  "type": "full_name",
  "role": "office",
  "page": 1, "x": 0.24, "y": 0.16, "w": 0.35, "h": 0.026,
  "required": true,
  "default_value": "",
  "aliases": ["customer", "customer_name"],
  "style":      {"font": "bundled:Caveat", "color": "#1d4ed8", "uppercase": true},
  "validation": {"max_length": 80}
}
```

| Key | Meaning |
|---|---|
| `name` | The key your API data uses. Letters, digits, `_`; starts with a letter. Generated from `label` if omitted. Unique per template (radio groups share one). |
| `label` | Human label shown to signers and used for matching data keys. |
| `type` | One of the [field types](#field-types). |
| `role` | Who fills or signs it. Defaults to the first role. |
| `page`, `x`, `y`, `w`, `h` | Position: **fractions (0–1) of the page**, origin top-left, page numbers **start at 1**. `w`/`h` default to the type's usual size. |
| `anchor` | Place the field relative to printed text instead of using `x`/`y`. See [Anchors](#anchors). |
| `required` | Whether the person responsible must provide it. (`date_signed` is never required: it fills itself.) |
| `default_value` | Used when the data doesn't supply one. Signers can edit a default; they can't edit a value you supplied. |
| `aliases` | Extra data keys that should fill this field. |
| `options` | `dropdown`: the allowed values. `radio`: filled for you from the group's boxes. |
| `option` | `radio` only: the choice this box represents. |
| `style`, `validation` | See below. |

Coordinates are fractions so they survive any page size. Rotated pages are handled: fields appear upright on the page as displayed.

## Field types

`GET /v1/field-types` returns this list with default sizes, so a UI can be built from it.

| Category | Type | What it is |
|---|---|---|
| People | `full_name` | A person's full name. If your data has only first and last name (or only the full name), the others are derived. |
| | `first_name`, `last_name` | Given / family name (derived from a full name when not supplied). |
| | `email` | Validated and lower-cased. |
| | `phone` | 7–15 digits, optional leading `+`; spaces, dashes and brackets are stripped. |
| | `company`, `job_title` | Free text. |
| | `address` | Multi-line text. |
| Text | `text` | A single line of text. |
| | `description` | Multi-line text that wraps inside its box and starts at the top. |
| | `id_number` | PAN, passport, policy number… add `validation.pattern` to enforce its format. |
| Numbers | `number` | Plain number (`1,250` and `1250.5` are both accepted). |
| | `amount` | Money. Accepts `₹ 1,25,000.50`; prints with your currency symbol, decimals and grouping. |
| | `percentage` | Number printed with a `%`. |
| Dates | `date` | Any common input format; one display format out. |
| | `date_signed` | Filled automatically with the day the role signs. |
| Choices | `checkbox` | `true`/`false` (also `yes/no`, `1/0`, `on/off`, `x`). |
| | `radio` | One box per choice. Boxes with the same `name` form a group; the group's value is the chosen `option`. |
| | `dropdown` | One value from `options` (matched case-insensitively). |
| Signing | `signature` | Drawn, typed or uploaded by the signer, or applied from the office's saved signature. |
| | `initials` | Same, for initials. |

**Radio groups.** Define one field per box, all with the same `name`, each with its own `option`:

```json
{"name": "plan", "type": "radio", "option": "Silver", "role": "client", "page": 1, "x": 0.1, "y": 0.50},
{"name": "plan", "type": "radio", "option": "Gold",   "role": "client", "page": 1, "x": 0.1, "y": 0.54}
```

## Style

Set on the template (`defaults`) or per field (`style`). Unknown keys are rejected with a clear error.

| Option | Applies to | Values |
|---|---|---|
| `font` | text types | `bundled:<Name>` (e.g. `bundled:Lato`, `bundled:Caveat`) or `user:<id>` for a font you uploaded. List them with `GET /fonts/catalog`. |
| `font_size` | text types | `0`–`200`. `0` (default) fits the box, capped at 14 pt (11 pt for multi-line). The text always shrinks to fit its box. |
| `color` | text, checkbox, radio | `#rrggbb` |
| `align` | text types | `left`, `center`, `right` |
| `uppercase` | text types | `true` |
| `date_format` | `date`, `date_signed` | a `strftime` pattern: `%d %b %Y` (default, *05 Mar 2026*), `%d/%m/%Y`, `%Y-%m-%d`, `%B %d, %Y`… |
| `currency`, `currency_position` | `amount` | any symbol up to 6 chars; `prefix` (default) or `suffix` |
| `decimals` | `amount`, `number` | `0`–`6` (amount defaults to 2) |
| `grouping` | `amount`, `number` | `intl` (1,234,567), `indian` (12,34,567), `none` |
| `mark` | `checkbox`, `radio` | `check`, `cross`, `dot` |

> **Currency symbols and fonts.** If the chosen font has no glyph for a currency sign, the symbol is replaced with a readable fallback (`₹` → `Rs.`, `€` → `EUR`, `£` → `GBP`) instead of printing a blank. Lato, Great Vibes, Allura, Caveat and Dancing Script include `₹`; Open Sans and Roboto Mono do not. You can upload your own `.ttf`/`.otf` font in the app (Fill from data → *Add your own font…*) or `POST /fonts`.

## Validation

Applied to data and to what signers type.

| Option | Meaning |
|---|---|
| `pattern` | A regular expression the whole value must match. Add `pattern_message` for a friendlier error. |
| `min_length`, `max_length` | Character limits. |
| `min`, `max` | Numeric limits for `number`, `amount`, `percentage`. |

Types validate themselves too (an `email` must be an email, a `date` must parse, a `dropdown` value must be one of its options).

## Anchors

Instead of coordinates, place a field relative to text that is printed on the PDF:

```json
{"name": "client_sig", "type": "signature", "role": "client",
 "anchor": {"text": "Client signature:", "position": "right"}}
```

| Key | Meaning |
|---|---|
| `text` | Text to find (required). |
| `page` | Search only this page (1-based). Default: the whole document. |
| `occurrence` | Use the Nth match (default 1). |
| `position` | `right` (default), `left`, `below`, `over`. |
| `offset_x`, `offset_y` | Extra shift, as fractions of the page. |

The field is resolved **once, when the template is saved**; it is stored with real coordinates and you can still fine-tune it in the designer. If the text isn't found the request fails with `fields[3]: Anchor text '…' (occurrence 1) was not found in the document.`

## Roles and who does what

- The **office** role is the account owner. Data you send fills office fields; the office signs by hand in the app (**Sign as office**) or automatically with `office_auto_sign` using the [saved signature](/guide/api#office-signature).
- **External roles** (`signer`) need a recipient in every envelope. They fill the fields you leave blank and sign.
- A role with fields that are **all already filled** (for example a data-only office role) is completed automatically; nobody has to click.
- Extra people who should only receive the finished copy go in as `{"kind": "cc", "email": "…"}`.

## Editing a template through the API

| | |
|---|---|
| `POST /v1/templates` | Create from an uploaded document (`document_id`) and a field list. |
| `POST /v1/templates/upload` | Multipart: PDF + `definition` JSON in one call. |
| `PUT /v1/templates/{id}` | Replace roles, defaults and the whole field list (version +1). |
| `PATCH /v1/templates/{id}` | Rename, archive, change roles/defaults. |
| `POST /v1/templates/{id}/fields` | Add one field (what the designer uses). |
| `PATCH` / `DELETE /v1/templates/{id}/fields/{field_id}` | Change or remove one field. |
| `POST /v1/templates/{id}/duplicate` | Copy. |
| `GET /v1/templates/{id}/schema` | **JSON Schema** of the data the template accepts, plus each role's fields. |
| `POST /v1/templates/{id}/check` | Dry run: how would this data be matched and validated? |
| `GET /v1/templates/{id}/preview.png?page=1` | The page rendered with sample data. |
| `DELETE /v1/templates/{id}` | Archive (envelopes already created are unaffected). |
