import json
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Dataset, FillTemplate, User
from app.security import get_current_user
from app.services.access import read_upload
from app.services.dataset_service import parse_dataset
from app.services.storage import InvalidDocument, resolve

router = APIRouter(prefix="/datasets", tags=["Datasets"])

MAX_DATASETS = 25


def dataset_out(d: Dataset) -> dict:
    return {
        "id": d.id, "name": d.name, "columns": json.loads(d.columns),
        "row_count": d.row_count, "created_at": d.created_at.isoformat(),
    }


def get_owned_dataset(db: Session, dataset_id: str, user_id: str) -> Dataset:
    ds = db.get(Dataset, dataset_id)
    if not ds or ds.user_id != user_id:
        raise HTTPException(404, "Data sheet not found")
    return ds


def load_rows(ds: Dataset) -> list[list[str]]:
    try:
        return json.loads(resolve(ds.file_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, InvalidDocument):
        raise HTTPException(404, "Data sheet file unavailable")


@router.post("", status_code=201)
def upload_dataset(
    file: UploadFile = File(...),
    name: str = Form(""),
    sheet: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a client sheet (.xlsx or .csv). The first row must contain column headers."""
    count = db.scalar(select(func.count()).select_from(Dataset).where(Dataset.user_id == user.id))
    if count >= MAX_DATASETS:
        raise HTTPException(409, f"You can keep at most {MAX_DATASETS} data sheets. Delete one first.")
    data = read_upload(file, limit_mb=settings.max_dataset_mb)
    try:
        columns, rows = parse_dataset(data, file.filename or "", sheet.strip() or None)
    except InvalidDocument as e:
        raise HTTPException(422, str(e))
    rel = f"datasets/{uuid.uuid4()}.json"
    resolve(rel).write_text(json.dumps(rows), encoding="utf-8")
    label = (name.strip() or (file.filename or "Data sheet").rsplit(".", 1)[0])[:120]
    ds = Dataset(user_id=user.id, name=label, columns=json.dumps(columns), row_count=len(rows), file_path=rel)
    db.add(ds)
    db.commit()
    return dataset_out(ds)


@router.get("")
def list_datasets(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.scalars(
        select(Dataset).where(Dataset.user_id == user.id).order_by(Dataset.created_at.desc())
    ).all()
    return [dataset_out(d) for d in rows]


@router.get("/{dataset_id}")
def get_dataset(
    dataset_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ds = get_owned_dataset(db, dataset_id, user.id)
    return {**dataset_out(ds), "offset": offset, "rows": load_rows(ds)[offset:offset + limit]}


@router.delete("/{dataset_id}", status_code=204)
def delete_dataset(dataset_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ds = get_owned_dataset(db, dataset_id, user.id)
    db.execute(update(FillTemplate).where(FillTemplate.dataset_id == ds.id).values(dataset_id=None))
    try:
        resolve(ds.file_path).unlink(missing_ok=True)
    except InvalidDocument:
        pass
    db.delete(ds)
    db.commit()
