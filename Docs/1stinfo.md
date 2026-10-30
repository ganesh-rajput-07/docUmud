Let's build DocumentOps as a production-oriented Python application, not a disposable prototype.

We'll use FastAPI, proper separation of business logic, database models, background processing, authentication and private file storage. We'll keep the infrastructure lightweight enough for your 8 GB RAM Ubuntu laptop.

One important distinction: tonight we'll build a production-oriented foundation that is ready for controlled testing. We won't claim it is production-secure or legally compliant until deployment security, PDF validation, backups and access-control testing are completed.

# 1. Our technical architecture

## DocumentOps

Python document processing platform

Customer / Admin Interface

HTML + CSS + JavaScript

FastAPI Application

Authentication · Uploads · Jobs · Downloads · Admin

SQLite

Users and jobs

Private storage

Originals and results

Background Worker

PDF processing · Validation · Reports · Retries

PDF Processing Engine

OpenDataLoader + PDF inspection and validation

## Stack decisions

| Layer            | Technology                         |
| ---------------- | ---------------------------------- |
| Language         | Python 3.12                        |
| API              | FastAPI                            |
| Frontend         | HTML, CSS, JavaScript              |
| Templates        | Jinja2                             |
| Database         | SQLite initially                   |
| ORM              | SQLAlchemy 2                       |
| Authentication   | Password hashing + signed sessions |
| Background jobs  | Separate lightweight worker        |
| PDF processing   | OpenDataLoader                     |
| PDF validation   | veraPDF                            |
| Testing          | pytest                             |
| Deployment later | Docker + reverse proxy             |

Why not PostgreSQL, Redis, Celery and React immediately?

Because they add infrastructure and memory consumption before we have customers. We'll isolate the database and worker interfaces so we can upgrade later without rewriting the application.

This also fits your existing Python 3.12 learning environment and low-memory laptop.&#x20;

# 2. PDF accessibility: an important technical decision

We'll use OpenDataLoader's `tagged-pdf` output mode:

```
import opendataloader_pdfopendataloader_pdf.convert(    input_path=["input.pdf"],    output_dir="output/",    format="tagged-pdf",    quiet=True,    threads="1",)
```

The current documentation confirms that this can generate tagged PDFs locally without a GPU. Java 11+ is required.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://opendataloader.org\&sz=32)

OpenDataLoader PDF+2



Commercial limitation: generating a tagged PDF does not automatically mean it passes every PDF/UA accessibility requirement. The open-source auto-tagging feature is available, but the vendor lists PDF/UA export as an enterprise feature. We'll sell an initial tagging and remediation workflow with human review, not falsely advertise certified compliance.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://www.opendataloader.org\&sz=32)

Auto-Tag PDFs for Accessibility+1



# 3. Environment setup

We'll use your existing Python 3.12 environment if it is still available, but create a separate project environment so we don't break your AI learning setup.

Open your Ubuntu terminal.

### Step 1 — Install system dependencies

```
sudo apt update

sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    openjdk-17-jdk \
    git \
    curl
```

Verify:

```
python3 --version
java -version
git --version
```

### Step 2 — Create the project

```
mkdir -p ~/Projects/DocumentOps
cd ~/Projects/DocumentOps

python3 -m venv .venv

source .venv/bin/activate

python -m pip install --upgrade pip
```

### Step 3 — Create the directory structure

```
mkdir -p app/services
mkdir -p storage/uploads
mkdir -p storage/results
mkdir -p storage/tmp
mkdir -p scripts
mkdir -p tests

touch app/__init__.py
touch app/services/__init__.py
```

Our initial project structure:

```
DocumentOps/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   ├── security.py
│   ├── worker.py
│   └── services/
│       ├── __init__.py
│       └── pdf_processor.py
├── scripts/
│   └── create_admin.py
├── storage/
│   ├── uploads/
│   ├── results/
│   └── tmp/
├── tests/
├── requirements.txt
├── .env
├── .gitignore
└── README.md
```

# 4. Install Python dependencies

Create `requirements.txt`:

```

fastapi
uvicorn[standard]
sqlalchemy
pydantic-settings
python-multipart
pwdlib[argon2]
pypdf
opendataloader-pdf
httpx
pytest

```

Install:

```
pip install -r requirements.txt
```

We'll keep the dependencies minimal. No React, Redis, Celery, Docker or GPU dependencies tonight.

# 5. Configuration and database

### File 1: `.env`

Create your configuration:

```
APP_NAME=DocumentOps
ENVIRONMENT=development

SECRET_KEY=REPLACE_WITH_A_RANDOM_SECRET

DATABASE_URL=sqlite:///./storage/documentops.db

STORAGE_ROOT=storage

MAX_UPLOAD_MB=25
MAX_PDF_PAGES=100

ACCESS_TOKEN_MINUTES=720
POLL_SECONDS=3

ALLOW_REGISTRATION=false
```

Generate a secure secret:

```
openssl rand -hex 32
```

Replace `REPLACE_WITH_A_RANDOM_SECRET` with its output.

### File 2: `.gitignore`

```
.venv/
__pycache__/
*.pyc
.env

storage/*
!storage/.gitkeep

.pytest_cache/
.coverage
```

### File 3: `app/config.py`

```
from pathlib import Pathfrom pydantic_settings import BaseSettings, SettingsConfigDictclass Settings(BaseSettings):    app_name: str = "DocumentOps"    environment: str = "development"    secret_key: str    database_url: str = "sqlite:///./storage/documentops.db"    storage_root: Path = Path("storage")    max_upload_mb: int = 25    max_pdf_pages: int = 100    access_token_minutes: int = 720    poll_seconds: int = 3    allow_registration: bool = False    model_config = SettingsConfigDict(        env_file=".env",        env_file_encoding="utf-8",        extra="ignore",    )settings = Settings()settings.storage_root.mkdir(parents=True, exist_ok=True)for folder in ("uploads", "results", "tmp"):    (settings.storage_root / folder).mkdir(
```

### File 4: `app/database.py`

```
from sqlalchemy import create_enginefrom sqlalchemy.orm import DeclarativeBase, sessionmakerfrom app.config import settingsconnect_args = {}if settings.database_url.startswith("sqlite"):    connect_args = {"check_same_thread": False}engine = create_engine(    settings.database_url,    connect_args=connect_args,    pool_pre_ping=True,)SessionLocal = sessionmaker(    bind=engine,    autoflush=False,    expire_on_commit=False,)class Base(DeclarativeBase):    passdef get_db():    db = SessionLocal()    try:        yield db    finally:        db.close()
```

### File 5: `app/models.py`

This defines our database structure.

We need two principal entities:

- Users: customers and administrators.
- Jobs: individual document processing requests.

```
import uuidfrom datetime import datetime, timezonefrom sqlalchemy import (    DateTime,    ForeignKey,    Integer,    String,    Text,)from sqlalchemy.orm import Mapped, mapped_columnfrom app.database import Basedef utc_now():    return datetime.now(timezone.utc)class User(Base):    __tablename__ = "users"    id: Mapped[str] = mapped_column(        String(36),        primary_key=True,        default=lambda: str(uuid.uuid4()),    )    email: Mapped[str] = mapped_column(        String(255),        unique=True,        index=True,        nullable=False,    )
```

Our job status lifecycle:

```
queued
  |
  v
processing
  |
  +------> completed
  |
  +------> failed
```

This gives us a foundation for future job retries and customer dashboards.

# 6. Authentication

We'll use signed bearer tokens rather than storing plaintext passwords or introducing a separate JWT library.

### File 6: `app/security.py`

```
from datetime import datetime, timezonefrom pwdlib import PasswordHashfrom itsdangerous import (    BadSignature,    SignatureExpired,    URLSafeTimedSerializer,)from fastapi import Depends, HTTPException, statusfrom fastapi.security import OAuth2PasswordBearerfrom sqlalchemy.orm import Sessionfrom app.config import settingsfrom app.database import get_dbfrom app.models import Userpassword_hash = PasswordHash.recommended()serializer = URLSafeTimedSerializer(    settings.secret_key,    salt="documentops-auth-v1",)oauth2_scheme = OAuth2PasswordBearer(    tokenUrl="/auth/token")def hash_password(password: str) -> str:    return password_hash.hash(password)def verify_password(    plain_password: str,
```

Why this approach?

- Passwords are hashed using Argon2.
- Tokens are cryptographically signed.
- Tokens expire.
- Database stores only password hashes.
- Admin and customer roles are separated.

For a public launch, we'll add login throttling, token revocation, account verification and stronger session/device management.

# 7. PDF processing engine

This is the core of our first paid service.

### File 7: `app/services/pdf_processor.py`

```
import jsonimport shutilfrom pathlib import Pathimport opendataloader_pdffrom pypdf import PdfReaderfrom app.config import settingsdef process_pdf(    input_path: Path,    job_id: str,) -> tuple[Path, dict]:    output_dir = (        settings.storage_root / "tmp" / job_id    )    output_dir.mkdir(        parents=True,        exist_ok=True,    )    # Run local PDF accessibility auto-tagging.    opendataloader_pdf.convert(        input_path=[str(input_path)],        output_dir=str(output_dir),        format="tagged-pdf",        quiet=True,        threads="1",    )    generated_files = list(        output_dir.rglob("*.pdf")
```

Notice that we aren't claiming PDF/UA certification simply because the PDF opens successfully. The report explicitly records that certification hasn't been established.

# 8. Background processing worker

We should never make customers wait for the PDF engine to finish inside the upload HTTP request.

Instead:

1. Upload endpoint saves the file.
2. Database creates a queued job.
3. Worker finds queued jobs.
4. Worker processes the PDF.
5. Database records the result.
6. Customer downloads the completed file.

### File 8: `app/worker.py`

```
import jsonimport timeimport loggingfrom datetime import datetime, timezonefrom sqlalchemy import select, updatefrom app.config import settingsfrom app.database import SessionLocal, initialize_databasefrom app.models import Jobfrom app.services.pdf_processor import process_pdflogging.basicConfig(    level=logging.INFO,    format="%(asctime)s %(levelname)s %(message)s",)logger = logging.getLogger("documentops.worker")def claim_job():    with SessionLocal() as db:        job = db.scalar(            select(Job)            .where(Job.status == "queued")            .order_by(Job.created_at)            .limit(1)        )        if not job:            return None        result = db.execute(            update(Job)
```

One worker process is sufficient for our initial setup. We should keep it that way until we move to PostgreSQL and a proper distributed queue.

# 9. FastAPI application

This is the main application connecting everything.

### File 9: `app/main.py`

```
import uuidimport refrom pathlib import Pathfrom datetime import datetime, timezonefrom fastapi import (    FastAPI,    Depends,    HTTPException,    UploadFile,    File,    status,)from fastapi.security import OAuth2PasswordRequestFormfrom fastapi.responses import FileResponsefrom pydantic import BaseModel, EmailStrfrom sqlalchemy import selectfrom sqlalchemy.orm import Sessionfrom pypdf import PdfReaderfrom app.config import settingsfrom app.database import get_db, initialize_databasefrom app.models import User, Jobfrom app.security import (    hash_password,    verify_password,    create_access_token,    get_current_user,    require_admin,)
```

One small correction to our dependencies: add `itsdangerous` and `email-validator` because our authentication and email validation use them.

Update `requirements.txt`:

```
itsdangerous
email-validator
```

Then install:

```
pip install itsdangerous email-validator
```

# 10. Create the first administrator

We have disabled public registration by default. That is intentional.

Initially, you create customers through the administrator account.

### File 10: `scripts/create_admin.py`

```
import getpassfrom sqlalchemy import selectfrom app.database import SessionLocal, initialize_databasefrom app.models import Userfrom app.security import hash_passworddef main():    initialize_database()    email = input("Admin email: ").strip().lower()    password = getpass.getpass(        "Admin password (minimum 12 characters): "    )    if len(password) < 12:        print("Password must contain at least 12 characters.")        return    with SessionLocal() as db:        existing = db.scalar(            select(User).where(User.email == email)        )        if existing:            print("This email already exists.")            return        admin = User(            email=email,            password_hash=hash_password(password),            role="admin",            is_active=True,
```

Run:

```
python scripts/create_admin.py
```

Create your administrator account.

Never commit this account's password or your `.env` file to GitHub.

# 11. Start DocumentOps

We'll run the API and worker in separate terminals.

Terminal 1 — API server

```
cd ~/Projects/DocumentOps

source .venv/bin/activate

uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --reload
```

Terminal 2 — Background worker

```
cd ~/Projects/DocumentOps

source .venv/bin/activate

python -m app.worker
```

Terminal 3 — Open the API interface

Visit:

[DocumentOps API documentation](http://127.0.0.1:8000/docs?utm_source=chatgpt.com)



Expected

&#x20;You should see the interactive FastAPI Swagger interface.



## 12. Create your first customer and test the complete workflow

### A. Get an admin access token

First, create the administrator (if you haven't already):

```
python scripts/create_admin.py
```

Get a token:

```
curl -X POST http://127.0.0.1:8000/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=YOUR_ADMIN_EMAIL&password=YOUR_ADMIN_PASSWORD"
```

You'll receive:

```
{
  "access_token": "YOUR_TOKEN",
  "token_type": "bearer",
  "expires_in": 43200
}
```

Keep this token private.

### B. Create a customer

```
curl -X POST http://127.0.0.1:8000/admin/users \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "customer@example.com",
    "password": "CustomerStrongPassword123!",
    "role": "customer"
  }'
```

### C. Generate a sample PDF

```
python - <<'PY'
from pypdf import PdfWriter

writer = PdfWriter()
writer.add_blank_page(width=595, height=842)

with open("sample.pdf", "wb") as f:
    writer.write(f)

print("Sample PDF created")
PY
```

This is only an upload smoke test, not a meaningful accessibility quality test. We'll need a real text-based PDF for that.

### D. Upload the document

Get a customer token using their email and password, then:

```
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Authorization: Bearer CUSTOMER_TOKEN" \
  -F "file=@sample.pdf"
```

Expected response:

```
{
  "message": "Document submitted successfully",
  "job": {
    "id": "JOB_UUID",
    "filename": "sample.pdf",
    "service": "pdf_accessibility",
    "status": "queued",
    "pages": 1
  }
}
```

### E. Check the job

```
curl http://127.0.0.1:8000/jobs \
  -H "Authorization: Bearer CUSTOMER_TOKEN"
```

Wait for the worker to complete processing.

### F. Download the result

```
curl \
  http://127.0.0.1:8000/jobs/JOB_UUID/download \
  -H "Authorization: Bearer CUSTOMER_TOKEN" \
  -o tagged-output.pdf
```

If all six steps work, we have successfully connected:

- Authentication
- Customer management
- File uploads
- Database persistence
- Background processing
- PDF tagging
- Private result downloads

## 13. Two small reliability improvements before testing

First, update the worker to recover jobs interrupted by an application crash.

In `app/worker.py`, add this import:

```
from pathlib import Pathfrom sqlalchemy import update
```

Replace the unusual `__import__("pathlib").Path(...)` expression in `process_job` with:

```
input_path=Path(job.input_path),
```

Then add this function:

```
def recover_interrupted_jobs():    with SessionLocal() as db:        db.execute(            update(Job)            .where(Job.status == "processing")            .values(status="queued")        )        db.commit()
```

Call it inside `main()`, immediately after `initialize_database()`:

```
initialize_database()recover_interrupted_jobs()
```

This is appropriate for our single-worker development setup. Don't run multiple workers with this recovery strategy.

Second, ensure the dependency file is complete. Your final `requirements.txt` should be:

```
fastapi
uvicorn[standard]
sqlalchemy
pydantic-settings
python-multipart
pwdlib[argon2]
itsdangerous
email-validator
pypdf
opendataloader-pdf
httpx
pytest
```

Reinstall:

```
pip install -r requirements.txt
```

# 14. Testing checklist

Use this checklist before considering the first version functional.

MVP testing progress

# 0/12

API starts without errors

Database creates successfully

Admin account can log in

Customer account can be created

Customer can upload a PDF

Invalid file formats are rejected

Oversized files are rejected

Worker picks up queued jobs

PDF processor generates tagged output

Completed job can be downloaded

Customer cannot access another customer's job

Failed jobs are recorded correctly

# 15. What is still missing before selling?

I want to be clear about this. We have designed an application foundation, not a finished commercial platform.

| Requirement                      | Current position   |
| -------------------------------- | ------------------ |
| FastAPI backend                  | Implemented        |
| User authentication              | Implemented        |
| Customer accounts                | Implemented        |
| Upload management                | Implemented        |
| PDF auto-tagging                 | Implemented        |
| Background worker                | Implemented        |
| Private downloads                | Implemented        |
| Customer-facing visual dashboard | Not yet built      |
| Admin visual dashboard           | Not yet built      |
| Payment gateway                  | Not yet built      |
| PDF/UA validation                | Not yet integrated |
| Human review workflow            | Not yet built      |
| Database migrations              | Not yet configured |
| Production hosting               | Not yet configured |
| Automated backups                | Not yet configured |

Before accepting real customer documents, we also need malware scanning, upload throttling, request-size limits at the reverse proxy, processing timeouts, audit logging, secure backups, retention controls and proper recovery testing.

For deployment, I'd eventually move to PostgreSQL and private object storage. Keep SQLite for tonight.

## 16. Our next development milestones

1. Tonight: Working backend

   Get authentication, uploads, background processing and downloads running locally.
2. Next: Customer and admin dashboards

   Build a proper website with a professional upload interface, job tracking and downloadable reports.
3. Next: Accessibility quality control

   Integrate veraPDF, visual inspection and human approval. Clearly distinguish tagged PDF output from verified PDF/UA compliance.
4. Next: Other two services

   Add AI/ML annotation and business document classification as separate processing modules.
5. Finally: Commercial launch

   Domain, hosting, payments, service agreements, pricing, privacy policy and customer acquisition.

## My recommendation for tonight

Don't start building the other services yet. First get this PDF pipeline working end-to-end.

Start with Step 1 and run the setup commands.

When you reach the installation stage, if OpenDataLoader or Java throws an error, send me the complete terminal output. We'll troubleshoot your actual environment rather than guessing.

Useful official documentation:

- [OpenDataLoader Python installation and API](https://opendataloader.org/docs/quick-start-python?utm_source=chatgpt.com)
- [PDF accessibility auto-tagging](https://a11y.opendataloader.org/?utm_source=chatgpt.com)
- [OpenDataLoader GitHub and licensing](https://github.com/opendataloader-project/opendataloader-pdf/blob/main/README.md?utm_source=chatgpt.com)
- [FastAPI documentation](https://fastapi.tiangolo.com/?utm_source=chatgpt.com)

The immediate goal: one real PDF goes in, one tagged PDF comes out, and the customer can securely download it. That's the first thing worth selling.