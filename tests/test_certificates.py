"""PDF generation, per-certificate failures and retrieval of generated certificates."""
import io
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.models import Job, JobStatus
from tests.conftest import valid_recipients


def pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    assert len(reader.pages) == 1
    return reader.pages[0].extract_text()


def first_certificate(client, job_id, index=0):
    return client.get(f"/api/jobs/{job_id}/certificates/").json()["results"][index]


# ---------- certificate generation ----------

def test_generated_pdf_contains_recipient_specific_data(client, post_job):
    job_id = post_job(
        client, [{"name": "Asha Rao", "email": "asha@x.com", "remarks": "With distinction"}],
        event_name="Data Science Summit", issued_by="Tech Academy", issue_date="2026-10-01",
    ).json()["id"]
    cert = first_certificate(client, job_id)
    assert cert["status"] == "GENERATED" and cert["certificate_number"].startswith("CERT-")

    r = client.get(cert["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    text = pdf_text(r.content)
    for expected in ("Asha Rao", "Data Science Summit", "Tech Academy", "With distinction",
                     "01 October 2026", cert["certificate_number"]):
        assert expected in text


def test_each_certificate_is_personalised(client, post_job):
    job_id = post_job(client, valid_recipients(3)).json()["id"]
    for i in range(3):
        c = first_certificate(client, job_id, i)
        assert f"Person {i + 1}" in pdf_text(client.get(c["download_url"]).content)


def test_markup_in_names_is_rendered_literally(client, post_job):
    job_id = post_job(client, [{"name": "<b>Tom</b> & Jerry", "email": "t@x.com"}]).json()["id"]
    text = pdf_text(client.get(first_certificate(client, job_id)["download_url"]).content)
    assert "<b>Tom</b> & Jerry" in text


def test_very_long_names_and_event_titles_still_render(client, post_job):
    job_id = post_job(client, [{"name": "W" * 100, "email": "w@x.com"}], event_name="E" * 150).json()["id"]
    assert first_certificate(client, job_id)["status"] == "GENERATED"


def test_unrenderable_characters_fail_only_that_certificate(client, post_job):
    people = [{"name": "Asha", "email": "a@x.com"}, {"name": "आशा", "email": "b@x.com"}]
    job_id = post_job(client, people).json()["id"]
    a, b = (first_certificate(client, job_id, i) for i in (0, 1))
    assert a["status"] == "GENERATED"
    assert b["status"] == "FAILED" and "cannot render" in b["error"]


def test_file_paths_never_use_user_supplied_text(client, post_job):
    job_id = post_job(client, [{"name": "../../etc/passwd", "email": "x@x.com"}]).json()["id"]
    storage: Path = client.app.state.settings.storage_dir
    files = list(storage.rglob("*.pdf"))
    assert len(files) == 1 and files[0].parent == storage / job_id
    assert "passwd" not in files[0].name


# ---------- failure of an individual certificate ----------

def failing_generator(real_generator):
    def _gen(data, dest):
        if data.recipient_name == "Boom":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"half-written")  # leaves partial output behind
            raise RuntimeError("renderer exploded")
        real_generator(data, dest)
    return _gen


def test_one_failing_certificate_does_not_stop_the_others(make_client, post_job):
    from app.services.pdf import generate_certificate_pdf
    client = make_client(generator=failing_generator(generate_certificate_pdf))
    people = [{"name": n, "email": f"{i}@x.com"} for i, n in enumerate(["Ann", "Boom", "Cid", "Dee"])]
    job_id = post_job(client, people).json()["id"]

    job = client.get(f"/api/jobs/{job_id}/").json()
    assert job["status"] == "COMPLETED_WITH_ERRORS"
    assert (job["succeeded"], job["failed"], job["pending"]) == (3, 1, 0)

    results = client.get(f"/api/jobs/{job_id}/certificates/").json()["results"]
    assert [r["status"] for r in results] == ["GENERATED", "FAILED", "GENERATED", "GENERATED"]
    assert "renderer exploded" in results[1]["error"]
    assert client.get(f"/api/jobs/{job_id}/certificates/{results[1]['id']}/download").status_code == 409
    assert client.get(results[2]["download_url"]).status_code == 200      # later ones still fine

    storage = client.app.state.settings.storage_dir
    assert len(list(storage.rglob("*.pdf"))) == 3                       # partial file was cleaned up


def test_job_where_every_certificate_fails_is_marked_failed(make_client, post_job):
    def always_fail(data, dest):
        raise RuntimeError("nope")
    client = make_client(generator=always_fail)
    job = client.get(f"/api/jobs/{post_job(client).json()['id']}/").json()
    assert job["status"] == "FAILED" and job["failed"] == 3 and job["succeeded"] == 0


# ---------- retrieving certificates ----------

def test_list_certificates_filter_and_pagination(client, post_job):
    people = valid_recipients(4) + [{"name": "", "email": "bad"}]
    job_id = post_job(client, people).json()["id"]

    page = client.get(f"/api/jobs/{job_id}/certificates/?limit=2&offset=1").json()
    assert page["total"] == 5 and [r["position"] for r in page["results"]] == [1, 2]

    failed = client.get(f"/api/jobs/{job_id}/certificates/?status=FAILED").json()
    assert failed["total"] == 1 and failed["results"][0]["position"] == 4
    assert client.get(f"/api/jobs/{job_id}/certificates/?status=BOGUS").status_code == 422


def test_download_certificate_errors(client, post_job):
    job_id = post_job(client).json()["id"]
    other_job = post_job(client).json()["id"]
    cert = first_certificate(client, job_id)
    assert client.get(f"/api/jobs/{job_id}/certificates/unknown/download").status_code == 404
    assert client.get(f"/api/jobs/{other_job}/certificates/{cert['id']}/download").status_code == 404  # wrong job


def test_download_all_as_zip(client, post_job):
    people = valid_recipients(2) + [{"name": "", "email": "bad"}]
    job_id = post_job(client, people).json()["id"]
    r = client.get(f"/api/jobs/{job_id}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["0001_Person_1.pdf", "0002_Person_2.pdf"]  # only generated ones
        assert all(zf.read(n).startswith(b"%PDF") for n in zf.namelist())


def test_download_all_requires_finished_job(client, post_job):
    job_id = post_job(client).json()["id"]
    with client.app.state.session_factory() as db:
        db.get(Job, job_id).status = JobStatus.PROCESSING.value
        db.commit()
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 409


def test_download_all_when_nothing_generated(client, post_job):
    job_id = post_job(client, [{"name": ""}]).json()["id"]
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 404
