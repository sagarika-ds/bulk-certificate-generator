import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RecipientIn(BaseModel):
    """One recipient. Validated individually so one bad entry never rejects the batch."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=254)
    remarks: str | None = Field(default=None, max_length=120)

    @field_validator("name")
    @classmethod
    def name_must_be_printable(cls, v: str) -> str:
        if not v.isprintable():
            raise ValueError("must not contain control characters or line breaks")
        return v

    @field_validator("email")
    @classmethod
    def email_format(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("is not a valid email address")
        return v.lower()

    @field_validator("remarks")
    @classmethod
    def remarks_clean(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not v.isprintable():
            raise ValueError("must not contain control characters or line breaks")
        return v


class JobCreate(BaseModel):
    """Job-level data is strict (422 on error); recipients are checked one by one later."""

    model_config = ConfigDict(str_strip_whitespace=True)

    event_name: str = Field(min_length=1, max_length=150, examples=["Python Bootcamp 2026"])
    issued_by: str = Field(default="Acme Academy", min_length=1, max_length=100)
    issue_date: date = Field(default_factory=date.today)
    recipients: list[Any] = Field(min_length=1)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_name: str
    issued_by: str
    issue_date: date
    status: str
    total: int
    succeeded: int
    failed: int
    pending: int
    progress_percent: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    certificates_url: str
    download_all_url: str


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int
    recipient_name: str | None
    recipient_email: str | None
    certificate_number: str
    status: str
    error: str | None
    download_url: str | None = None


class CertificatePage(BaseModel):
    job_id: str
    total: int
    limit: int
    offset: int
    results: list[CertificateOut]


def format_validation_error(exc) -> str:
    """Compact, client-friendly text for a pydantic ValidationError."""
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'recipient'}: {e['msg']}" for e in exc.errors()
    )
