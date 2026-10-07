"""Runtime settings, overridable through environment variables."""
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/certs.db")
    )
    storage_dir: Path = field(
        default_factory=lambda: Path(os.getenv("STORAGE_DIR", "./data/certificates"))
    )
    max_recipients: int = field(
        default_factory=lambda: int(os.getenv("MAX_RECIPIENTS", "5000"))
    )
    # Progress is committed to the DB every N certificates so status polling stays cheap.
    progress_batch_size: int = 10
