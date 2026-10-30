"""Parse XLSX/CSV client sheets into (columns, rows) of plain strings."""
import csv
import io
import re
import zipfile
from datetime import date, datetime, time

from app.config import settings
from app.services.storage import InvalidDocument

MAX_CELL = 2000
MAX_UNZIPPED = 150 * 1024 * 1024  # guard against zip bombs


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y") if value.time() == time(0) else value.strftime("%d-%m-%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.10g}"
    return re.sub(r"\s+", " ", str(value)).strip()[:MAX_CELL]


def _headers(raw: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, h in enumerate(raw, 1):
        name = h.strip() or f"Column {i}"
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return out


def _read_xlsx(data: bytes, sheet: str | None) -> tuple[list[str], list[list[str]]]:
    import openpyxl

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
                raise InvalidDocument("The spreadsheet is too large when unpacked.")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except InvalidDocument:
        raise
    except Exception:
        raise InvalidDocument("This is not a valid .xlsx file. (Old .xls files must be saved as .xlsx or .csv.)")
    try:
        if sheet and sheet not in wb.sheetnames:
            raise InvalidDocument(f"Sheet '{sheet}' not found.")
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next((r for r in rows if any(c not in (None, "") for c in r)), None)
        if header is None:
            raise InvalidDocument("The sheet is empty.")
        width = max((i for i, c in enumerate(header, 1) if c not in (None, "")), default=0)
        columns = _headers([_fmt(c) for c in header[:width]])
        body = []
        for r in rows:
            cells = [_fmt(c) for c in r[:width]] + [""] * max(0, width - len(r))
            if any(cells):
                body.append(cells)
                if len(body) > settings.max_dataset_rows:
                    break
        return columns, body
    finally:
        wb.close()


def _read_csv(data: bytes) -> tuple[list[str], list[list[str]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [[_fmt(c) for c in r] for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise InvalidDocument("The file is empty.")
    columns = _headers(rows[0])
    width = len(columns)
    body = [(r + [""] * width)[:width] for r in rows[1:settings.max_dataset_rows + 2]]
    return columns, body


def parse_dataset(data: bytes, filename: str, sheet: str | None = None) -> tuple[list[str], list[list[str]]]:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        columns, rows = _read_xlsx(data, sheet)
    elif name.endswith((".csv", ".txt")):
        columns, rows = _read_csv(data)
    else:
        raise InvalidDocument("Upload an .xlsx or .csv file.")
    if not columns:
        raise InvalidDocument("No column headers found in the first row.")
    if len(columns) > settings.max_dataset_columns:
        raise InvalidDocument(f"At most {settings.max_dataset_columns} columns are supported.")
    if len(rows) > settings.max_dataset_rows:
        raise InvalidDocument(f"At most {settings.max_dataset_rows} rows are supported per sheet.")
    if not rows:
        raise InvalidDocument("The sheet has headers but no data rows.")
    return columns, rows


def normalise(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


def auto_match(labels: list[str], columns: list[str]) -> dict[str, str | None]:
    """Match tag labels to column headers ignoring case, spaces and punctuation."""
    by_key = {}
    for c in columns:
        by_key.setdefault(normalise(c), c)
    return {label: by_key.get(normalise(label)) for label in labels}
