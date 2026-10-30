from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "DocUMud"
    environment: str = "development"
    secret_key: str
    database_url: str = "sqlite:///./storage/documud.db"
    storage_root: Path = Path("storage")
    max_upload_mb: int = 25
    max_pdf_pages: int = 100
    access_token_minutes: int = 720
    poll_seconds: int = 3
    allow_registration: bool = False
    public_base_url: str = "http://127.0.0.1:8000"
    signing_link_days: int = 7
    tagging_timeout_seconds: int = 300

    # Requests per minute. Counters are per process (fine for a single uvicorn worker).
    rate_limit_per_minute: int = 300          # per API key / session
    auth_rate_limit_per_minute: int = 10      # login & register, per IP
    public_rate_limit_per_minute: int = 120   # recipient signing pages, per IP
    webhook_max_attempts: int = 6
    ocr_max_pages: int = 30
    max_font_kb: int = 2048
    max_user_fonts: int = 10
    max_dataset_mb: int = 10
    max_dataset_rows: int = 5000
    max_dataset_columns: int = 100
    fill_max_rows: int = 500        # PDFs generated per run

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@documud.local"
    smtp_security: str = "starttls"      # starttls | ssl | none (platform default SMTP)

    # Credentials users save for their own mailbox are encrypted with a key derived from this
    # (falls back to SECRET_KEY). Changing it makes saved SMTP passwords unreadable.
    encryption_key: str = ""

    email_delay_seconds: float = 0.5     # pause between messages on one connection
    email_max_attempts: int = 5
    auto_reminders: bool = True
    reminder_after_days: int = 3
    max_reminders: int = 2
    require_signer_otp: bool = True      # email signers verify a code (when the sender can send email)
    otp_minutes: int = 10
    otp_max_attempts: int = 5

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()

for _folder in ("uploads", "scans", "signatures", "results", "tmp", "fonts", "datasets"):
    (settings.storage_root / _folder).mkdir(parents=True, exist_ok=True)
