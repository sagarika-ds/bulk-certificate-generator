import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def make_client(tmp_path):
    """Factory so a test can plug in its own (e.g. failing) certificate generator."""
    stack = []

    def _make(generator=None, **settings_overrides):
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            storage_dir=tmp_path / "certs",
            **settings_overrides,
        )
        kwargs = {"generator": generator} if generator else {}
        client = TestClient(create_app(settings, **kwargs))
        client.__enter__()  # runs startup (creates tables)
        stack.append(client)
        return client

    yield _make
    for c in stack:
        c.__exit__(None, None, None)


@pytest.fixture()
def client(make_client):
    return make_client()


def valid_recipients(n=3):
    return [{"name": f"Person {i}", "email": f"person{i}@example.com"} for i in range(1, n + 1)]


@pytest.fixture()
def post_job():
    def _post(client, recipients=None, **overrides):
        body = {"event_name": "Python Bootcamp 2026", "issued_by": "Acme Academy",
                "issue_date": "2026-10-01",
                "recipients": valid_recipients() if recipients is None else recipients}
        body.update(overrides)
        return client.post("/api/jobs/", json=body)
    return _post
