import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from .config import Settings
from .database import init_db, make_engine
from .routers import jobs
from .services.pdf import generate_certificate_pdf
from .services.processor import resume_incomplete_jobs


def create_app(settings: Settings | None = None, generator=generate_certificate_pdf) -> FastAPI:
    """App factory. Tests pass their own settings and (optionally) a fake generator."""
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        init_db(engine)
        # Finish jobs interrupted by a crash/restart (in the background, don't block startup).
        threading.Thread(
            target=resume_incomplete_jobs,
            args=(session_factory, settings.storage_dir, app.state.generator, settings.progress_batch_size),
            daemon=True,
        ).start()
        yield
        engine.dispose()

    app = FastAPI(
        title="Bulk Certificate Generator",
        description="Submit a list of recipients, track progress, download the generated PDF certificates.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.generator = generator
    app.include_router(jobs.router)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
