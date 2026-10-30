We're changing the scope of DocumentOps into something considerably more useful: an all-in-one document workspace, rather than a PDF tagging-only service.

The new product will let customers:
- Scan physical documents using their phone camera.
- Upload existing PDFs and images.
- Merge multiple pages into a PDF.
- Edit PDFs by adding text, dates and annotations.
- Draw or type their own signatures.
- Send documents to other people for signatures.
- Track who has signed and who is pending.
- Download completed documents.
- Request PDF accessibility tagging and document classification.
I recommend keeping the name DocumentOps.

One architectural decision is important: signing, scanning and document intelligence must be separate modules connected through a common document-management system. That will make it easier to add features without turning main.py into a massive application.
1. Revised architecture
DocumentOps architecture
Web application
Desktop browser + mobile browser

Scanner
Camera / image upload

PDF editor
Preview / edit / sign

Signature
Personal / requested

Intelligence
Tag / classify / extract



FastAPI application
Authentication · Document API · Signing API · Processing API

SQLite
Users, documents, jobs, signers

Private storage
Originals, scans, signatures, results



Background processing
PDF conversion · Accessibility · OCR · Notifications

2. Revised technology stack
Component	Technology	Why
Backend	FastAPI	Your existing backend
Frontend	HTML, CSS, JS	Low resource consumption
PDF viewer	PDF.js	Render PDF pages
PDF editing	pdf-lib	Add text, images and signatures
Scanner	Browser Camera API	No mobile app required
Image processing	Pillow initially	Lightweight image operations
OCR	Tesseract, later	Extract scanned text
PDF processing	OpenDataLoader	Accessibility tagging
PDF manipulation	PyMuPDF	Merge, inspect and stamp PDFs
Database	SQLite initially	Minimal infrastructure
Authentication	Existing auth module	Reuse
Background jobs	Existing worker	Extend

We won't run a React development server, Redis, PostgreSQL or multiple Docker containers on your laptop tonight.
3. Revised project structure
Keep your existing authentication, jobs and PDF processor. Add these modules:
DocumentOps/
│
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   ├── security.py
│   │
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── documents.py
│   │   ├── scanner.py
│   │   └── signing.py
│   │
│   ├── services/
│   │   ├── pdf_processor.py
│   │   ├── pdf_editor.py
│   │   ├── signing_service.py
│   │   └── scanner_service.py
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── editor.html
│   │   └── sign_document.html
│   │
│   └── static/
│       ├── css/
│       │   └── app.css
│       └── js/
│           ├── scanner.js
│           ├── editor.js
│           └── signing.js
│
├── storage/
│   ├── uploads/
│   ├── scans/
│   ├── signatures/
│   ├── results/
│   └── tmp/
│
└── tests/


4. Database changes
We need three new database entities.
Table	Purpose
Document	Stores customer-owned documents
SignatureRequest	Stores recipients and signing status
SignatureEvent	Stores a record of important signing actions

A signature request must not simply store an image and declare the document signed. It needs a record of who was invited, when they signed, which document version they signed and the final document hash.

Add the following to app/models.py, below your existing models:
Document model
class Document(Base):    __tablename__ = "documents"    id: Mapped[str] = mapped_column(        String(36), primary_key=True,        default=lambda: str(uuid.uuid4())    )    user_id: Mapped[str] = mapped_column(        ForeignKey("users.id"), index=True    )    filename: Mapped[str] = mapped_column(String(255))    file_path: Mapped[str] = mapped_column(Text)    file_hash: Mapped[str] = mapped_column(String(64))    document_type: Mapped[str] = mapped_column(        String(50), default="pdf"    )    status: Mapped[str] = mapped_column(        String(30), default="active"    )    created_at: Mapped[datetime] = mapped_column(        DateTime(timezone=True), default=utc_now    )



SignatureRequest model
class SignatureRequest(Base):    __tablename__ = "signature_requests"    id: Mapped[str] = mapped_column(        String(36), primary_key=True,        default=lambda: str(uuid.uuid4())    )    document_id: Mapped[str] = mapped_column(        ForeignKey("documents.id"), index=True    )    sender_id: Mapped[str] = mapped_column(        ForeignKey("users.id")    )    recipient_email: Mapped[str] = mapped_column(        String(255)    )    token_hash: Mapped[str] = mapped_column(        String(64), unique=True, index=True    )    status: Mapped[str] = mapped_column(        String(30), default="pending"    )    expires_at: Mapped[datetime] = mapped_column(        DateTime(timezone=True)    )    signed_at: Mapped[datetime | None] = mapped_column(        DateTime(timezone=True), nullable=True    )



SignatureEvent model
class SignatureEvent(Base):    __tablename__ = "signature_events"    id: Mapped[str] = mapped_column(        String(36), primary_key=True,        default=lambda: str(uuid.uuid4())    )    request_id: Mapped[str] = mapped_column(        ForeignKey("signature_requests.id"),        index=True    )    event_type: Mapped[str] = mapped_column(        String(50)    )    details: Mapped[str] = mapped_column(Text)    created_at: Mapped[datetime] = mapped_column(        DateTime(timezone=True), default=utc_now    )



These are additive models, but because your existing application uses create_all, existing installations won't receive schema changes to already-created tables automatically. For tonight, these new tables will be created on startup. Before deployment, we'll introduce Alembic migrations.
5. Scanner implementation
The scanner should work from a phone browser.

The flow:
1. Open DocumentOps on your phone.
2. Tap Scan Document.
3. Give camera permission.
4. Capture the page.
5. Preview and retake if necessary.
6. Add more pages.
7. Crop and rotate.
8. Convert the images into a PDF.
9. Save it to the document workspace.




















7










Install the image-processing dependency
pip install pillow


Create app/services/scanner_service.py
from pathlib import Pathfrom PIL import Image, ImageOpsfrom pypdf import PdfWriterfrom uuid import uuid4def images_to_pdf(    image_paths: list[Path],    output_dir: Path,) -> Path:    pages = []    for image_path in image_paths:        with Image.open(image_path) as image:            image = ImageOps.exif_transpose(image)            image = image.convert("RGB")            pages.append(image.copy())    if not pages:        raise ValueError("No images supplied")    output_path = output_dir / f"{uuid4()}.pdf"    first, *remaining = pages    first.save(        output_path,        format="PDF",        save_all=True,        append_images=remaining,        resolution=150,    )    for page in pages:        page.close()



Create app/routers/scanner.py
import uuidimport hashlibfrom pathlib import Pathfrom fastapi import (    APIRouter,    UploadFile,    File,    Depends,    HTTPException,)from sqlalchemy.orm import Sessionfrom app.config import settingsfrom app.database import get_dbfrom app.models import User, Documentfrom app.security import get_current_userfrom app.services.scanner_service import images_to_pdfrouter = APIRouter(    prefix="/scanner",    tags=["Scanner"],)@router.post("/convert")async def convert_scans(    files: list[UploadFile] = File(...),    db: Session = Depends(get_db),    user: User = Depends(get_current_user),):    if not files or len(files) > 20:        raise HTTPException(



This first version accepts multiple image pages and converts them into one PDF. It doesn't yet do automatic perspective correction or document-edge detection. We'll add those after the basic camera workflow works.
6. Personal signatures and signature requests
This is the most important new feature.

We need two distinct workflows.
A. Sign my own document







pdf.wondershare.com


- Open a PDF.
- Choose Draw Signature or Type Signature.
- Place the signature on the page.
- Add date and name.
- Save a new PDF copy.
  
  B. Request another person's signature
  
  
  
  
  
  
  
  raq.com
- Select an existing PDF.
- Enter recipient email.
- Create a signing invitation.
- Recipient opens a secure link.
- Recipient reviews and signs.
- Store the signed PDF and audit events.

6.1 Install PDF manipulation
pip install pymupdf


We'll use PyMuPDF for stamping a signature image onto a selected PDF page.
6.2 Add fields to SignatureRequest
Add these fields to the model created earlier:
document_hash: Mapped[str] = mapped_column(    String(64))signed_file_path: Mapped[str | None] = mapped_column(    Text,    nullable=True)signed_file_hash: Mapped[str | None] = mapped_column(    String(64),    nullable=True)



These record which version of the document was signed and the hash of the final result.
6.3 Create app/services/signing_service.py
import hashlibimport secretsimport iofrom datetime import datetime, timedelta, timezonefrom pathlib import Pathimport fitzfrom PIL import Imagedef create_signing_token():    token = secrets.token_urlsafe(32)    token_hash = hashlib.sha256(        token.encode()    ).hexdigest()    return token, token_hashdef hash_file(path: Path) -> str:    digest = hashlib.sha256()    with path.open("rb") as file:        for chunk in iter(            lambda: file.read(1024 * 1024),            b"",        ):            digest.update(chunk)    return digest.hexdigest()def apply_signature(



This function creates a visible signature mark. It does not create a certificate-backed cryptographic digital signature.
6.4 Create app/routers/signing.py
This router handles both creating invitations and receiving signatures.
import hashlibimport uuidfrom datetime import datetime, timedelta, timezonefrom pathlib import Pathfrom fastapi import (    APIRouter,    Depends,    HTTPException,    UploadFile,    File,    Form,)from fastapi.responses import FileResponsefrom sqlalchemy import selectfrom sqlalchemy.orm import Sessionfrom app.config import settingsfrom app.database import get_dbfrom app.models import (    User,    Document,    SignatureRequest,    SignatureEvent,)from app.security import get_current_userfrom app.services.signing_service import (    create_signing_token,    hash_file,    apply_signature,)



This gives us a first working signing workflow, with an important limitation: the recipient's identity is not yet verified by email OTP or another strong authentication method. A random invitation link alone is not proof of identity.

For production, we must also add consent text, an immutable event log, request cancellation, rate limits, a race-safe one-time signing operation, and a secure signing email workflow. We should not market this initial implementation as a legally certified e-signature service.
7. Connect the new modules to FastAPI
Now update app/main.py.

Add these imports:
from fastapi.staticfiles import StaticFilesfrom fastapi.templating import Jinja2Templatesfrom app.routers import scanner, signing



Immediately after creating your FastAPI application:
app.mount(    "/static",    StaticFiles(directory="app/static"),    name="static",)templates = Jinja2Templates(    directory="app/templates")app.include_router(scanner.router)app.include_router(signing.router)



Also add a recipient signing-page route:
from fastapi import Requestfrom app.routers.signing import find_request@app.get("/sign/{token}")def signing_page(    token: str,    request: Request,    db: Session = Depends(get_db),):    signing_request = find_request(token, db)    if signing_request.status != "pending":        raise HTTPException(            409,            "This signing request is no longer available",        )    return templates.TemplateResponse(        request=request,        name="sign_document.html",        context={            "token": token,            "recipient": signing_request.recipient_email,        },    )



For the frontend dependencies, add:
jinja2
pillow
pymupdf


Then:
pip install -r requirements.txt


8. Recipient signing interface
Create:

app/templates/sign_document.html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport"
          content="width=device-width, initial-scale=1">

    <title>Sign Document | DocumentOps</title>

    <link rel="stylesheet"
          href="/static/css/app.css">

    <style>
        body {
            background: #f5f6f8;
            font-family: Arial, sans-serif;
            margin: 0;
        }

        .container {
            max-width: 760px;
            margin: 40px auto;
            padding: 24px;
            background: white;
            border-radius: 12px;
        }

        canvas {
            width: 100%;
            max-width: 600px;
            border: 1px solid #aaa;
            touch-action: none;
        }

        button {
            padding: 12px 18px;
            margin: 8px 4px 8px 0;
            cursor: pointer;
        }

        input {
            padding: 10px;
            width: 90%;
            margin: 5px 0 15px;
        }
    </style>
</head>

<body>

<div class="container">

    <h1>DocumentOps</h1>

    <h2>Review and sign your document</h2>

    <p>Signing invitation for: {{ recipient }}</p>

    <div id="document-preview">
        <p>Loading document...</p>
    </div>

    <h3>Your signature</h3>

    <canvas id="signature-pad"
            width="600"
            height="200"></canvas>

    <div>
        <button id="clear-signature">
            Clear
        </button>
    </div>

    <h3>Signature placement</h3>

    <label>Page number (starting at 1)</label>
    <input id="page-number"
           type="number"
           value="1"
           min="1">

    <label>Horizontal position (0–1)</label>
    <input id="position-x"
           type="number"
           value="0.60"
           step="0.01"
           min="0"
           max="1">

    <label>Vertical position (0–1)</label>
    <input id="position-y"
           type="number"
           value="0.80"
           step="0.01"
           min="0"
           max="1">

    <label>Signature width (0–1)</label>
    <input id="signature-width"
           type="number"
           value="0.25"
           step="0.01"
           min="0"
           max="1">

    <label>Signature height (0–1)</label>
    <input id="signature-height"
           type="number"
           value="0.08"
           step="0.01"
           min="0"
           max="1">

    <p>
        <label>
            <input type="checkbox" id="consent">
            I have reviewed this document and intend
            to apply my signature electronically.
        </label>
    </p>

    <button id="submit-signature">
        Sign Document
    </button>

    <p id="status"></p>

</div>

<script>
    window.DOCUMENTOPS_SIGNING_TOKEN =
        "{{ token }}";
</script>

<script src="/static/js/signing.js"></script>

</body>
</html>


Create app/static/js/signing.js:
const canvas = document.getElementById("signature-pad");
const ctx = canvas.getContext("2d");

let drawing = false;
let hasSignature = false;

ctx.lineWidth = 3;
ctx.lineCap = "round";
ctx.strokeStyle = "#111";

function getPosition(event) {
    const rect = canvas.getBoundingClientRect();

    return {
        x: (event.clientX - rect.left)
            * canvas.width / rect.width,

        y: (event.clientY - rect.top)
            * canvas.height / rect.height
    };
}

canvas.addEventListener("pointerdown", event => {
    drawing = true;
    canvas.setPointerCapture(event.pointerId);

    const pos = getPosition(event);

    ctx.beginPath();
    ctx.moveTo(pos.x, pos.y);
});

canvas.addEventListener("pointermove", event => {
    if (!drawing) return;

    const pos = getPosition(event);

    ctx.lineTo(pos.x, pos.y);
    ctx.stroke();

    hasSignature = true;
});

canvas.addEventListener("pointerup", () => {
    drawing = false;
});

canvas.addEventListener("pointercancel", () => {
    drawing = false;
});

document.getElementById("clear-signature")
    .addEventListener("click", () => {

        ctx.clearRect(
            0, 0, canvas.width, canvas.height
        );

        hasSignature = false;
    });


document.getElementById("submit-signature")
    .addEventListener("click", async () => {

        const status = document.getElementById("status");

        if (!hasSignature) {
            status.textContent = "Please draw your signature.";
            return;
        }

        if (!document.getElementById("consent").checked) {
            status.textContent = "Please provide consent.";
            return;
        }

        const blob = await new Promise(resolve => {
            canvas.toBlob(resolve, "image/png");
        });

        const form = new FormData();

        form.append("signature", blob, "signature.png");

        form.append(
            "page_number",
            Number(document.getElementById("page-number").value) - 1
        );

        form.append("x", document.getElementById("position-x").value);
        form.append("y", document.getElementById("position-y").value);
        form.append("width", document.getElementById("signature-width").value);
        form.append("height", document.getElementById("signature-height").value);
        form.append("consent", "true");

        status.textContent = "Submitting signature...";

        try {
            const response = await fetch(
                `/signing/requests/${window.DOCUMENTOPS_SIGNING_TOKEN}/sign`,
                {
                    method: "POST",
                    body: form
                }
            );

            const result = await response.json();

            if (!response.ok) {
                throw new Error(result.detail || "Signing failed");
            }

            status.textContent = "Document signed successfully.";

        } catch (error) {
            status.textContent = error.message;
        }
    });


This is a working first-pass signature pad and signing form. It deliberately uses normalized placement coordinates rather than a drag-and-drop editor. We'll make the signature position visually selectable in the next frontend iteration.

Also, the recipient page still needs an actual PDF preview embedded into it before commercial release. We'll add PDF.js for that rather than asking people to sign documents they cannot review.
9. Scanner interface
Create app/templates/scanner.html:
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport"
          content="width=device-width, initial-scale=1">

    <title>Document Scanner | DocumentOps</title>
</head>

<body>

<h1>Scan documents</h1>

<p>Capture pages using your phone camera or upload images.</p>

<input
    type="file"
    id="scan-files"
    accept="image/jpeg,image/png"
    capture="environment"
    multiple
>

<div id="preview"></div>

<button id="create-pdf">
    Create PDF
</button>

<p id="scan-status"></p>

<script src="/static/js/scanner.js"></script>

</body>
</html>


Create app/static/js/scanner.js:
const fileInput = document.getElementById("scan-files");
const preview = document.getElementById("preview");

let selectedFiles = [];

fileInput.addEventListener("change", () => {
    selectedFiles = Array.from(fileInput.files);

    preview.innerHTML = "";

    selectedFiles.forEach((file, index) => {
        const image = document.createElement("img");

        image.src = URL.createObjectURL(file);
        image.alt = `Scanned page ${index + 1}`;
        image.style.width = "140px";
        image.style.margin = "8px";

        preview.appendChild(image);
    });
});

document.getElementById("create-pdf")
    .addEventListener("click", async () => {

        if (!selectedFiles.length) {
            alert("Please capture or upload pages.");
            return;
        }

        const form = new FormData();

        selectedFiles.forEach(file => {
            form.append("files", file);
        });

        const status = document.getElementById("scan-status");

        status.textContent = "Creating PDF...";

        try {
            const token = sessionStorage.getItem("documentops_token");

            const response = await fetch("/scanner/convert", {
                method: "POST",
                headers: {
                    Authorization: `Bearer ${token}`
                },
                body: form
            });

            const result = await response.json();

            if (!response.ok) {
                throw new Error(result.detail || "Scan failed");
            }

            status.textContent =
                `PDF created successfully. Document ID: ${result.document_id}`;

        } catch (error) {
            status.textContent = error.message;
        }
    });


For tonight, the scanner uses the mobile camera/file picker. The next upgrade will use a live camera preview with getUserMedia, page cropping, rotation and automatic image enhancement.
10. Personal PDF signing
We also need to let an authenticated customer sign their own uploaded document, without creating an invitation.

Create app/routers/documents.py:
import uuidimport hashlibfrom pathlib import Pathfrom fastapi import (    APIRouter,    Depends,    UploadFile,    File,    Form,    HTTPException,)from fastapi.responses import FileResponsefrom sqlalchemy import selectfrom sqlalchemy.orm import Sessionfrom pypdf import PdfReaderfrom app.config import settingsfrom app.database import get_dbfrom app.models import User, Documentfrom app.security import get_current_userfrom app.services.signing_service import (    apply_signature,    hash_file,)router = APIRouter(    prefix="/documents",    tags=["Documents"],)



This preserves the original document and creates a separate signed copy. That's important: customers should never lose their original document just because they edited or signed it.
10.1 Update main.py imports
from app.routers import documents, scanner, signingapp.include_router(documents.router)app.include_router(scanner.router)app.include_router(signing.router)



Also create a scanner page route:
@app.get("/scanner")def scanner_page(request: Request):    return templates.TemplateResponse(        request=request,        name="scanner.html",        context={},    )



And update the recipient page's document preview section:
<iframe
    src="/signing/requests/{{ token }}/document"
    width="100%"
    height="650"
    style="border: 1px solid #ddd;"
    title="Document to review">
</iframe>


Now the recipient can actually review the PDF before signing.
11. Important fixes to the signing implementation
Before running it, make these three corrections.

Fix 1 — Explicitly create the signing request ID.

In create_request():
request = SignatureRequest(    id=str(uuid.uuid4()),    document_id=document.id,    sender_id=user.id,    recipient_email=recipient_email.lower().strip(),    token_hash=token_hash,    document_hash=hash_file(source_path),    status="pending",    expires_at=datetime.now(timezone.utc)    + timedelta(days=7),)



This ensures the associated audit event receives a real request ID.

Fix 2 — Normalize SQLite timestamps.

In find_request() replace the expiration comparison with:
expiry = request.expires_atif expiry.tzinfo is None:    expiry = expiry.replace(tzinfo=timezone.utc)if expiry <= datetime.now(timezone.utc):    request.status = "expired"    db.commit()    raise HTTPException(410, "Signing link expired")



Fix 3 — Don't treat the invitation URL as identity verification.

For local testing, the invitation link is enough to exercise the workflow. For actual customers, we'll add recipient verification and protect the signing endpoint against simultaneous requests and replay attempts.

Also add a signing event for the invitation, signing and any cancellation. An audit log must be more than a text field that can be freely rewritten by the application.
12. Final configuration changes
Update .env:
APP_NAME=DocumentOps
ENVIRONMENT=development

SECRET_KEY=YOUR_GENERATED_SECRET

DATABASE_URL=sqlite:///./storage/documentops.db

STORAGE_ROOT=storage

MAX_UPLOAD_MB=25
MAX_PDF_PAGES=100

ACCESS_TOKEN_MINUTES=720
POLL_SECONDS=3

ALLOW_REGISTRATION=false

PUBLIC_BASE_URL=http://127.0.0.1:8000


Add to app/config.py:
public_base_url: str = "http://127.0.0.1:8000"



In signing.py, replace the hardcoded URL with:
signing_link = (    f"{settings.public_base_url}/sign/{token}")



For tonight, manually share the invitation URL with your test recipient. Email delivery comes next.

Also add an authenticated download endpoint for documents in documents.py:
@router.get("/{document_id}/download")def download_document(    document_id: str,    db: Session = Depends(get_db),    user: User = Depends(get_current_user),):    document = db.get(Document, document_id)    if not document or document.user_id != user.id:        raise HTTPException(404, "Document not found")    path = Path(document.file_path).resolve()    if not path.is_file():        raise HTTPException(404, "File unavailable")    if hash_file(path) != document.file_hash:        raise HTTPException(409, "Document integrity check failed")    return FileResponse(        path,        media_type="application/pdf",        filename=document.filename,    )



13. Run the revised project
Update dependencies:
pip install -r requirements.txt


Create the missing directories:
mkdir -p app/routers
mkdir -p app/templates
mkdir -p app/static/css
mkdir -p app/static/js

mkdir -p storage/scans
mkdir -p storage/signatures


Create app/routers/__init__.py:
# DocumentOps API routers



Start the application:

Terminal 1:
cd ~/Projects/DocumentOps
source .venv/bin/activate

uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --reload


Terminal 2:
cd ~/Projects/DocumentOps
source .venv/bin/activate

python -m app.worker


Your API documentation:
Open DocumentOps APIYour scanner:
Open DocumentOps ScannerThe scanner and signing endpoints require authentication where appropriate. You'll need a logged-in browser session or a temporary development token to test them.
14. What we can realistically finish tonight
I want to distinguish between the functional foundation and the complete product.
Feature	Tonight's target
PDF upload	Implement
Image-to-PDF scanning	Implement
Multi-page scanning	Implement
Draw signature	Implement
Personal signing	Implement
Signature invitations	Implement
Recipient signing	Implement
Signature audit foundation	Implement
PDF visual editor	Next iteration
Drag-and-drop signature placement	Next iteration
Automatic image cropping	Next iteration
OCR	Next iteration
Email notifications	Next iteration
Cryptographic digital signatures	Future milestone

The current code is a development implementation, not something to expose publicly yet. In particular, the signing invitation, audit records and token-consumption flow need additional security work before accepting real contracts.
15. The business roadmap
Once the document workspace is working, I would develop the product in this order:
Phase 1
Document workspace

Upload, scan, merge, preview and download.


Phase 2
PDF editing and signatures

Text, annotations, signature placement, invitation workflow, verification and audit trails.


Phase 3
Document intelligence

PDF accessibility, OCR, classification, extraction and AI/ML annotation.


Phase 4
Commercial platform

Customer portal, payment gateway, email, pricing, hosting and business operations.


16. One final recommendation: don't charge for everything separately
Our eventual business model could look like this:
Plan	Possible offering
Free	Limited scanning, PDF preview and personal signing
Pay per document	Signature requests and document processing
Business	Bulk documents, team workflows and audit history
Custom	Accessibility remediation and AI/ML annotation

These are proposed packaging options, not validated prices. We should research competitors and test willingness to pay before setting prices.

What I want you to do now
There is one practical issue: the previous implementation is spread across many files, and replacing everything at once makes debugging difficult.

Let's build and verify in this order:
1. Make sure your existing FastAPI application still starts.
2. Add the three database models.
3. Add the scanner and document routes.
4. Test image-to-PDF conversion.
5. Add personal signatures.
6. Add signature requests.
7. Test signing from a second browser or phone.
Start with the database and scanner. Don't deploy it publicly yet.

Our first milestone is a customer opening DocumentOps on their phone, scanning a paper document, signing the resulting PDF, and downloading it. Once that works, we'll have a genuinely useful first product rather than just another document-upload website.