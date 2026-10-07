"""Creating jobs, input validation, job status / progress."""
import pytest

from app.models import Certificate, CertStatus, Job, JobStatus
from app.services.processor import process_job, resume_incomplete_jobs
from tests.conftest import valid_recipients


# ---------- creating a job ----------

def test_create_job_returns_202_and_job_details(client, post_job):
    r = post_job(client)
    assert r.status_code == 202
    body = r.json()
    assert body["total"] == 3 and body["event_name"] == "Python Bootcamp 2026"
    assert body["certificates_url"] == f"/api/jobs/{body['id']}/certificates/"
    assert body["download_all_url"] == f"/api/jobs/{body['id']}/download"


def test_job_completes_in_background(client, post_job):
    job_id = post_job(client).json()["id"]
    job = client.get(f"/api/jobs/{job_id}/").json()
    assert job["status"] == "COMPLETED"
    assert (job["total"], job["succeeded"], job["failed"], job["pending"]) == (3, 3, 0, 0)
    assert job["progress_percent"] == 100.0
    assert job["started_at"] and job["finished_at"]


def test_uses_defaults_for_optional_job_fields(client):
    r = client.post("/api/jobs/", json={"event_name": "Workshop", "recipients": valid_recipients(1)})
    assert r.status_code == 202 and r.json()["issued_by"] and r.json()["issue_date"]


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope/").status_code == 404
    assert client.get("/api/jobs/nope/certificates/").status_code == 404
    assert client.get("/api/jobs/nope/download").status_code == 404


# ---------- job-level validation (whole request rejected) ----------

@pytest.mark.parametrize(
    "payload",
    [
        {"recipients": valid_recipients(1)},                                  # no event_name
        {"event_name": "", "recipients": valid_recipients(1)},                # blank event_name
        {"event_name": "X"},                                                  # no recipients
        {"event_name": "X", "recipients": []},                                # empty list
        {"event_name": "X", "recipients": "Asha"},                            # not a list
        {"event_name": "X", "issue_date": "yesterday", "recipients": valid_recipients(1)},
        {"event_name": "X" * 151, "recipients": valid_recipients(1)},
    ],
)
def test_invalid_job_payload_is_rejected(client, payload):
    assert client.post("/api/jobs/", json=payload).status_code == 422


def test_too_many_recipients(make_client, post_job):
    client = make_client(max_recipients=3)
    assert post_job(client, valid_recipients(4)).status_code == 413
    assert post_job(client, valid_recipients(3)).status_code == 202


# ---------- recipient-level validation (only the bad entry fails) ----------

BAD_RECIPIENTS = [
    ({"email": "a@b.com"}, "name"),                                  # missing name
    ({"name": "   ", "email": "a@b.com"}, "name"),                   # blank name
    ({"name": "Asha", "email": "not-an-email"}, "email"),
    ({"name": "Asha"}, "email"),                                     # missing email
    ({"name": "Line\nBreak", "email": "a@b.com"}, "control"),
    ({"name": "N" * 101, "email": "a@b.com"}, "name"),               # too long
    ({"name": "Asha", "email": "a@b.com", "remarks": "r" * 121}, "remarks"),
    ("just a string", "recipient"),                                  # wrong type
    (None, "recipient"),
]


@pytest.mark.parametrize("bad,expected_hint", BAD_RECIPIENTS)
def test_invalid_recipient_fails_alone(client, post_job, bad, expected_hint):
    good = {"name": "Good Person", "email": "good@example.com"}
    job_id = post_job(client, [good, bad]).json()["id"]

    job = client.get(f"/api/jobs/{job_id}/").json()
    assert job["status"] == "COMPLETED_WITH_ERRORS"
    assert (job["succeeded"], job["failed"]) == (1, 1)

    results = client.get(f"/api/jobs/{job_id}/certificates/").json()["results"]
    assert results[0]["status"] == "GENERATED"
    assert results[1]["status"] == "FAILED"
    assert results[1]["position"] == 1
    assert expected_hint in results[1]["error"]
    assert results[1]["download_url"] is None


def test_duplicate_email_in_one_job_is_rejected(client, post_job):
    people = [{"name": "A", "email": "same@x.com"}, {"name": "B", "email": "SAME@x.com"}]
    job_id = post_job(client, people).json()["id"]
    results = client.get(f"/api/jobs/{job_id}/certificates/").json()["results"]
    assert [r["status"] for r in results] == ["GENERATED", "FAILED"]
    assert "duplicate" in results[1]["error"]


def test_all_recipients_invalid_marks_job_failed(client, post_job):
    r = post_job(client, [{"name": ""}, {"email": "x"}])
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "FAILED" and body["failed"] == 2 and body["finished_at"]


def test_email_is_normalised_and_whitespace_trimmed(client, post_job):
    job_id = post_job(client, [{"name": "  Asha Rao ", "email": " Asha@Example.COM "}]).json()["id"]
    r = client.get(f"/api/jobs/{job_id}/certificates/").json()["results"][0]
    assert r["recipient_name"] == "Asha Rao" and r["recipient_email"] == "asha@example.com"


# ---------- progress & resilience ----------

def test_progress_is_committed_while_processing(client, post_job):
    """Counters visible to other readers advance during the run (batch_size=1)."""
    job_id = post_job(client, valid_recipients(4)).json()["id"]
    sf = client.app.state.session_factory
    with sf() as db:  # simulate a fresh job: everything pending again
        for c in db.query(Certificate).filter_by(job_id=job_id):
            c.status, c.file_path = CertStatus.PENDING.value, None
        job = db.get(Job, job_id)
        job.status, job.succeeded = JobStatus.PENDING.value, 0
        db.commit()

    observed = []

    def spying_generator(data, dest):
        with sf() as other:  # a different DB session, like an API request would use
            observed.append(other.get(Job, job_id).succeeded)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-fake")

    process_job(sf, client.app.state.settings.storage_dir, job_id, spying_generator, batch_size=1)
    assert observed == [0, 1, 2, 3]
    assert client.get(f"/api/jobs/{job_id}/").json()["status"] == "COMPLETED"


def test_interrupted_job_is_resumed_without_redoing_finished_work(client, post_job):
    job_id = post_job(client, valid_recipients(4)).json()["id"]
    sf = client.app.state.session_factory
    with sf() as db:  # pretend the server died after 2 of 4 certificates
        certs = db.query(Certificate).filter_by(job_id=job_id).order_by(Certificate.position).all()
        for c in certs[2:]:
            c.status, c.file_path = CertStatus.PENDING.value, None
        db.get(Job, job_id).status = JobStatus.PROCESSING.value
        db.commit()

    calls = []
    def counting_generator(data, dest):
        calls.append(data.recipient_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-fake")

    resume_incomplete_jobs(sf, client.app.state.settings.storage_dir, counting_generator)
    assert calls == ["Person 3", "Person 4"]
    job = client.get(f"/api/jobs/{job_id}/").json()
    assert job["status"] == "COMPLETED" and job["succeeded"] == 4
