from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User
from app.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=120)
    password: str = Field(min_length=10, max_length=128)


def _token_response(user: User) -> dict:
    return {
        "access_token": create_access_token(user.id),
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
    }


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    if not settings.allow_registration:
        raise HTTPException(403, "Registration is disabled.")
    user = User(
        email=body.email.lower(),
        name=body.name.strip(),
        password_hash=hash_password(body.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An account with this email already exists.")
    return _token_response(user)


@router.post("/token")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == form.username.strip().lower()))
    ok = verify_password(form.password, user.password_hash if user else None)
    if not user or not ok or not user.is_active:
        raise HTTPException(401, "Incorrect email or password.")
    return _token_response(user)


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "name": user.name}
