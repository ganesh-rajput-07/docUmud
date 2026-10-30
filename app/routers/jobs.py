import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Document, Job, User
from app.security import get_current_user

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get("")
def list_jobs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.execute(
        select(Job, Document.filename)
        .join(Document, Document.id == Job.document_id)
        .where(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
        .limit(50)
    ).all()
    return [
        {
            "id": j.id,
            "filename": filename,
            "document_id": j.document_id,
            "service": j.service,
            "status": j.status,
            "error": j.error,
            "report": json.loads(j.report) if j.report else None,
            "result_document_id": j.result_document_id,
            "created_at": j.created_at.isoformat(),
        }
        for j, filename in rows
    ]
