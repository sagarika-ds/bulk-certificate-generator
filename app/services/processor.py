"""Background worker: generates the PENDING certificates of one job.

Design notes
* Each certificate is wrapped in its own try/except -> one failure never stops the batch.
* Only PENDING certificates are processed, so calling this again after a crash
  simply resumes where it stopped (see ``resume_incomplete_jobs``).
* Counters are recomputed from the certificate rows (single source of truth) and
  committed every ``progress_batch_size`` certificates so ``GET /jobs/{id}`` shows live progress.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..models import Certificate, CertStatus, Job, JobStatus
from .pdf import CertificateData, generate_certificate_pdf

log = logging.getLogger(__name__)
Generator = Callable[[CertificateData, Path], None]


def refresh_counts(db: Session, job: Job) -> None:
    counts = dict(
        db.execute(
            select(Certificate.status, func.count()).where(Certificate.job_id == job.id)
            .group_by(Certificate.status)
        ).all()
    )
    job.succeeded = counts.get(CertStatus.GENERATED.value, 0)
    job.failed = counts.get(CertStatus.FAILED.value, 0)


def final_status(job: Job) -> JobStatus:
    if job.succeeded == 0:
        return JobStatus.FAILED
    return JobStatus.COMPLETED_WITH_ERRORS if job.failed else JobStatus.COMPLETED


def process_job(
    session_factory: sessionmaker, storage_dir: Path, job_id: str,
    generator: Generator = generate_certificate_pdf, batch_size: int = 10,
) -> None:
    db = session_factory()
    try:
        job = db.get(Job, job_id)
        if job is None:
            log.error("process_job: job %s not found", job_id)
            return
        job.status = JobStatus.PROCESSING.value
        job.started_at = job.started_at or datetime.now(timezone.utc)
        db.commit()

        pending = db.scalars(
            select(Certificate)
            .where(Certificate.job_id == job_id, Certificate.status == CertStatus.PENDING.value)
            .order_by(Certificate.position)
        ).all()

        for n, cert in enumerate(pending, start=1):
            rel_path = f"{job_id}/{cert.id}.pdf"
            try:
                generator(
                    CertificateData(
                        recipient_name=cert.recipient_name,
                        event_name=job.event_name,
                        issued_by=job.issued_by,
                        issue_date=job.issue_date,
                        certificate_number=cert.certificate_number,
                        remarks=cert.remarks,
                    ),
                    storage_dir / rel_path,
                )
                cert.status, cert.file_path, cert.error = CertStatus.GENERATED.value, rel_path, None
            except Exception as exc:  # noqa: BLE001 - isolate every certificate
                log.warning("certificate %s failed: %s", cert.id, exc)
                (storage_dir / rel_path).unlink(missing_ok=True)
                cert.status = CertStatus.FAILED.value
                cert.error = f"generation failed: {exc}"[:500]
            if n % batch_size == 0:
                refresh_counts(db, job)
                db.commit()

        refresh_counts(db, job)
        job.status = final_status(job).value
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:  # noqa: BLE001 - infrastructure error (e.g. DB down)
        log.exception("job %s crashed", job_id)
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            refresh_counts(db, job)
            job.status = JobStatus.FAILED.value
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def resume_incomplete_jobs(
    session_factory: sessionmaker, storage_dir: Path, generator: Generator, batch_size: int = 10
) -> None:
    """Called at startup: finish jobs interrupted by a restart."""
    with session_factory() as db:
        ids = db.scalars(
            select(Job.id).where(Job.status.in_([JobStatus.PENDING.value, JobStatus.PROCESSING.value]))
        ).all()
    for job_id in ids:
        log.info("resuming interrupted job %s", job_id)
        process_job(session_factory, storage_dir, job_id, generator, batch_size)
