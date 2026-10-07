import enum
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"                            # accepted, not started
    PROCESSING = "PROCESSING"                      # worker is generating
    COMPLETED = "COMPLETED"                        # every certificate generated
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"  # finished, some failed
    FAILED = "FAILED"                              # finished, none generated


class CertStatus(str, enum.Enum):
    PENDING = "PENDING"
    GENERATED = "GENERATED"
    FAILED = "FAILED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_name: Mapped[str] = mapped_column(String(150))
    issued_by: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING.value, index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Certificate.position"
    )


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        Index("ix_cert_job_position", "job_id", "position"),
        Index("ix_cert_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    position: Mapped[int] = mapped_column(Integer)  # index in the submitted list (0-based)
    recipient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remarks: Mapped[str | None] = mapped_column(String(255), nullable=True)
    certificate_number: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default=CertStatus.PENDING.value)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(255), nullable=True)  # relative to storage_dir

    job: Mapped[Job] = relationship(back_populates="certificates")
