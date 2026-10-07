import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..models import Certificate, CertStatus, Job, JobStatus
from ..schemas import (
    CertificateOut, CertificatePage, JobCreate, JobOut, RecipientIn, format_validation_error,
)
from ..services.processor import process_job

router = APIRouter(prefix="/api/jobs", tags=["certificate jobs"])

FINISHED = {JobStatus.COMPLETED.value, JobStatus.COMPLETED_WITH_ERRORS.value, JobStatus.FAILED.value}


def get_db(request: Request):
    db: Session = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def _job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id!r} not found")
    return job


def _job_out(job: Job) -> JobOut:
    done = job.succeeded + job.failed
    return JobOut(
        id=job.id, event_name=job.event_name, issued_by=job.issued_by, issue_date=job.issue_date,
        status=job.status, total=job.total, succeeded=job.succeeded, failed=job.failed,
        pending=job.total - done,
        progress_percent=round(100 * done / job.total, 1) if job.total else 100.0,
        created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at,
        certificates_url=f"/api/jobs/{job.id}/certificates/",
        download_all_url=f"/api/jobs/{job.id}/download",
    )


def _cert_out(cert: Certificate) -> CertificateOut:
    out = CertificateOut.model_validate(cert)
    if cert.status == CertStatus.GENERATED.value:
        out.download_url = f"/api/jobs/{cert.job_id}/certificates/{cert.id}/download"
    return out


def _best_effort_str(raw, key: str) -> str | None:
    value = raw.get(key) if isinstance(raw, dict) else None
    return value[:255] if isinstance(value, str) else None


@router.post("/", response_model=JobOut, status_code=202)
def create_job(
    payload: JobCreate, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)
):
    """Submit many recipients at once. Returns immediately (202); generation runs in the background.

    Recipients are validated one by one: invalid ones are recorded as FAILED (with the reason)
    while all valid ones are still generated.
    """
    settings = request.app.state.settings
    if len(payload.recipients) > settings.max_recipients:
        raise HTTPException(413, f"Too many recipients (max {settings.max_recipients} per job)")

    job = Job(
        id=uuid.uuid4().hex, event_name=payload.event_name, issued_by=payload.issued_by,
        issue_date=payload.issue_date, total=len(payload.recipients),
    )
    seen_emails: set[str] = set()
    certs: list[Certificate] = []
    for pos, raw in enumerate(payload.recipients):
        cert = Certificate(
            id=uuid.uuid4().hex, job_id=job.id, position=pos,
            certificate_number=f"CERT-{job.id[:6].upper()}-{pos + 1:05d}",
            recipient_name=_best_effort_str(raw, "name"),
            recipient_email=_best_effort_str(raw, "email"),
        )
        try:
            recipient = RecipientIn.model_validate(raw)
        except ValidationError as exc:
            cert.status, cert.error = CertStatus.FAILED.value, f"invalid recipient: {format_validation_error(exc)}"
        else:
            cert.recipient_name, cert.recipient_email, cert.remarks = (
                recipient.name, recipient.email, recipient.remarks,
            )
            if recipient.email in seen_emails:
                cert.status = CertStatus.FAILED.value
                cert.error = "invalid recipient: duplicate email in this job"
            else:
                seen_emails.add(recipient.email)
        certs.append(cert)

    job.failed = sum(c.status == CertStatus.FAILED.value for c in certs)
    if job.failed == job.total:  # nothing to generate -> finish immediately
        job.status = JobStatus.FAILED.value
        job.finished_at = datetime.now(timezone.utc)
    db.add(job)
    db.add_all(certs)
    db.commit()

    if job.status == JobStatus.PENDING.value:
        background.add_task(
            process_job, request.app.state.session_factory, settings.storage_dir, job.id,
            request.app.state.generator, settings.progress_batch_size,
        )
    return _job_out(job)


@router.get("/{job_id}/", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    """Status and progress of a job."""
    return _job_out(_job_or_404(db, job_id))


@router.get("/{job_id}/certificates/", response_model=CertificatePage)
def list_certificates(
    job_id: str,
    status: Literal["PENDING", "GENERATED", "FAILED"] | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Per-recipient result: status, error (if failed) and download link (if generated)."""
    _job_or_404(db, job_id)
    query = select(Certificate).where(Certificate.job_id == job_id)
    if status:
        query = query.where(Certificate.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Certificate.position).limit(limit).offset(offset)).all()
    return CertificatePage(
        job_id=job_id, total=total, limit=limit, offset=offset, results=[_cert_out(r) for r in rows]
    )


@router.get("/{job_id}/certificates/{certificate_id}/download")
def download_certificate(job_id: str, certificate_id: str, request: Request, db: Session = Depends(get_db)):
    """Download one certificate as a PDF."""
    cert = db.get(Certificate, certificate_id)
    if cert is None or cert.job_id != job_id:
        raise HTTPException(404, "Certificate not found")
    if cert.status != CertStatus.GENERATED.value:
        raise HTTPException(409, f"Certificate is {cert.status}" + (f": {cert.error}" if cert.error else ""))
    path = request.app.state.settings.storage_dir / cert.file_path
    if not path.is_file():
        raise HTTPException(410, "Certificate file is no longer available")
    return FileResponse(path, media_type="application/pdf", filename=f"{cert.certificate_number}.pdf")


def _slug(text: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")[:40] or "certificate"


@router.get("/{job_id}/download")
def download_all(job_id: str, request: Request, db: Session = Depends(get_db)):
    """Download every generated certificate of a finished job as one ZIP."""
    job = _job_or_404(db, job_id)
    if job.status not in FINISHED:
        raise HTTPException(409, f"Job is still {job.status}; try again when it has finished")
    certs = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job_id, Certificate.status == CertStatus.GENERATED.value)
        .order_by(Certificate.position)
    ).all()
    if not certs:
        raise HTTPException(404, "No certificates were generated for this job")

    storage = request.app.state.settings.storage_dir
    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
        for c in certs:
            file = storage / c.file_path
            if file.is_file():
                zf.write(file, f"{c.position + 1:04d}_{_slug(c.recipient_name)}.pdf")
    return FileResponse(
        tmp_path, media_type="application/zip", filename=f"certificates_{job_id[:8]}.zip",
        background=BackgroundTask(os.unlink, tmp_path),  # remove the temp file after sending
    )
