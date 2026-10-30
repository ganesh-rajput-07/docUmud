from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, initialize_database
from app.services.rate_limit import classify, limiter
from app.routers import api_keys, auth, datasets, documents, email_settings, fill, fonts, jobs, scanner, signing, tags, webhooks

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title=f"{settings.app_name} API",
    description=(
        "Authenticate with `X-API-Key: dm_...` (or `Authorization: Bearer dm_...`). "
        "Create keys in the dashboard under *API access*."
    ),
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

for r in (auth, api_keys, email_settings, documents, tags, fill, datasets, fonts, jobs, scanner, signing, webhooks):
    app.include_router(r.router)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path.startswith("/static"):
        return await call_next(request)
    key, limit = classify(request)
    allowed, remaining, reset = limiter.hit(key, limit)
    headers = {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining)}
    if not allowed:
        return JSONResponse(
            {"detail": f"Too many requests. Try again in {reset} seconds."},
            status_code=429, headers={**headers, "Retry-After": str(reset)},
        )
    response = await call_next(request)
    response.headers.update(headers)
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


def _asset_version() -> int:
    """Newest static-file mtime, appended to asset URLs so browsers never serve a stale copy."""
    return max(
        (int(f.stat().st_mtime) for f in (BASE / "static").rglob("*") if f.is_file() and f.suffix in (".css", ".js")),
        default=0,
    )


def _page(request: Request, name: str, **context):
    return templates.TemplateResponse(
        request=request, name=name,
        context={"app_name": settings.app_name, "asset_v": _asset_version(), **context},
    )


@app.get("/")
def login_page(request: Request):
    return _page(request, "login.html", allow_registration=settings.allow_registration)


@app.get("/dashboard")
def dashboard_page(request: Request):
    return _page(request, "dashboard.html")


@app.get("/scanner")
def scanner_page(request: Request):
    return _page(request, "scanner.html")


@app.get("/documents/{document_id}/sign")
def sign_own_page(document_id: str, request: Request):
    return _page(request, "sign_own.html", document_id=document_id)


@app.get("/documents/{document_id}/tagger")
def tag_page(document_id: str, request: Request):
    return _page(request, "tag_document.html", document_id=document_id)


@app.get("/documents/{document_id}/fill")
def fill_page(document_id: str, request: Request):
    return _page(request, "fill.html", document_id=document_id)


@app.get("/settings/email")
def email_settings_page(request: Request):
    return _page(request, "email_settings.html")


@app.get("/sign/{token}")
def recipient_page(token: str, request: Request):
    return _page(request, "sign_document.html", token=token)
