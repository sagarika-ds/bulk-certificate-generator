# Bulk Certificate Generator

A FastAPI backend that accepts **one request containing many recipients**, generates a PDF
certificate for each from a predefined template, lets the client **track progress**, and lets it
**download** certificates individually or as a ZIP.

* Python 3.10+ · FastAPI · SQLAlchemy + SQLite (relational; swap via `DATABASE_URL`) · ReportLab (PDF)

---

## Setup

```bash
git clone <your-repo-url> && cd bulk-certificate-generator
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run the application

```bash
uvicorn app.main:app --reload
```

* API: http://127.0.0.1:8000 · Swagger UI: http://127.0.0.1:8000/docs
* DB and generated PDFs are stored under `./data/` (created automatically).
* Optional env vars: `DATABASE_URL` (e.g. `postgresql+psycopg://...`), `STORAGE_DIR`, `MAX_RECIPIENTS` (default 5000).

## Run the tests

```bash
pytest -q
```

39 tests cover: job creation, job-level and recipient-level validation, PDF content, per-certificate
failure isolation, progress reporting, crash-resume, listing/filtering and downloading.

---

## How to submit a certificate generation request

`POST /api/jobs/` → returns **202 Accepted** immediately; generation continues in the background.

```bash
curl -X POST http://127.0.0.1:8000/api/jobs/ \
  -H "Content-Type: application/json" \
  -d '{
        "event_name": "Python Bootcamp 2026",
        "issued_by": "Acme Academy",
        "issue_date": "2026-10-01",
        "recipients": [
          {"name": "Asha Rao",  "email": "asha@example.com", "remarks": "With distinction"},
          {"name": "Ravi Kumar","email": "ravi@example.com"},
          {"name": "",          "email": "not-an-email"}
        ]
      }'
```

| field | required | rules |
|---|---|---|
| `event_name` | yes | 1–150 chars |
| `issued_by` | no (default `Acme Academy`) | 1–100 chars |
| `issue_date` | no (default today) | `YYYY-MM-DD` |
| `recipients[]` | yes, ≥ 1 and ≤ `MAX_RECIPIENTS` | each: `name` (1–100, printable), `email` (valid format), `remarks` (optional, ≤ 120) |

Response (`202`):
```json
{
  "id": "2934881c95af4feba65bd3583fb7ea4f",
  "event_name": "Python Bootcamp 2026", "issued_by": "Acme Academy", "issue_date": "2026-10-01",
  "status": "PENDING", "total": 3, "succeeded": 0, "failed": 1, "pending": 2,
  "progress_percent": 33.3,
  "certificates_url": "/api/jobs/2934881c…/certificates/",
  "download_all_url": "/api/jobs/2934881c…/download"
}
```

### Check progress — `GET /api/jobs/{id}/`
`status` is one of `PENDING → PROCESSING → COMPLETED | COMPLETED_WITH_ERRORS | FAILED`,
plus `total / succeeded / failed / pending / progress_percent / started_at / finished_at`.

### See per-recipient results — `GET /api/jobs/{id}/certificates/?status=FAILED&limit=100&offset=0`
```json
{"job_id": "2934…", "total": 1, "limit": 100, "offset": 0, "results": [
  {"id": "06a0…", "position": 2, "recipient_name": "", "recipient_email": "not-an-email",
   "certificate_number": "CERT-293488-00003", "status": "FAILED",
   "error": "invalid recipient: name: String should have at least 1 character; email: Value error, is not a valid email address",
   "download_url": null}
]}
```
`status` filter: `PENDING`, `GENERATED`, `FAILED`.

## How to retrieve generated certificates

| What | Request |
|---|---|
| One PDF | `GET /api/jobs/{job_id}/certificates/{certificate_id}/download` (the `download_url` from the list above) |
| All PDFs as ZIP | `GET /api/jobs/{job_id}/download` (only once the job has finished; contains successful certificates only) |

```bash
curl -OJ http://127.0.0.1:8000/api/jobs/<job_id>/download
```

Error codes: `404` unknown job/certificate · `409` certificate failed / job not finished ·
`413` too many recipients · `422` invalid job-level payload.

---

## Design decisions

### Architecture
```
app/
  main.py               app factory, startup (create tables, resume interrupted jobs)
  routers/jobs.py       HTTP endpoints, request validation, downloads
  services/pdf.py       the predefined certificate template (ReportLab)
  services/processor.py background worker that generates a job's certificates
  models.py             Job, Certificate (SQLAlchemy)
  schemas.py            Pydantic request/response models
tests/
```

### Synchronous vs background processing — **background**
A job may contain thousands of recipients. Doing the work inside the request would hold the HTTP
connection open for a long time, risk client/proxy timeouts, and make retrying dangerous. So
`POST /api/jobs/` validates, stores everything (one DB transaction), returns `202`, and FastAPI's
`BackgroundTasks` runs the worker after the response is sent.

*Alternatives considered:* Celery/RQ + Redis (more robust and horizontally scalable, but
heavy infrastructure for this scope); synchronous generation (simplest, but doesn't scale).
Because the DB is the source of truth, swapping `BackgroundTasks` for a real queue is a
one-line change in the router.

### Partial success instead of all-or-nothing
* Job-level problems (missing `event_name`, empty list) → whole request rejected (`422`).
* **Recipient-level problems** → that recipient is recorded as `FAILED` with a readable reason;
  all other recipients are still generated. Duplicate emails inside one job are treated as invalid to prevent double-issuing.
* **Generation failures**: every certificate is processed in its own `try/except`; partial files are deleted;
  the error is saved on the certificate row. Final job status is `COMPLETED`, `COMPLETED_WITH_ERRORS` or `FAILED`.

### Progress tracking
Counters on the job are recomputed from certificate rows (single source of truth, no drift) and committed
every 10 certificates, so polling shows live progress without a DB write per certificate.

### Crash safety
Only `PENDING` certificates are processed, so the worker is idempotent. At startup the app
re-runs jobs left in `PENDING`/`PROCESSING` by a crash/restart and continues where they stopped.
(Assumes a single app process; with several workers you would add a job lock/lease or use a real queue.)

### Template & safety
* One predefined A4-landscape template drawn with ReportLab; long names/titles shrink to fit.
* Text is drawn directly (never parsed as markup), so input such as `<b>x</b>` is printed literally.
* Files are stored as `<job_id>/<certificate_id>.pdf` — user input is never used in file paths
  (no path traversal); PDFs are written to a temp file and renamed atomically.
* The built-in PDF fonts only cover Western (Latin-1) characters. A name with unsupported characters
  fails *that* certificate with a clear message instead of printing garbage.

### Data model
`jobs` (1) → (N) `certificates`, indexed by `(job_id, position)` and `(job_id, status)` so
listing, filtering and counting stay fast for large jobs.

---

## Learnings & future scope

* Learned how `BackgroundTasks`, DB sessions and threads interact (each worker needs its own session) and how to
  make a worker idempotent so it can resume.
* Future: Celery/RQ + Redis; embedding a Unicode font (e.g. Noto) for non-Latin names; e-mailing certificates;
  QR-code verification endpoint; multiple templates; authentication and per-user jobs; webhooks on completion;
  CSV upload of recipients; retry endpoint for failed certificates; cleanup of old files; Docker + CI.
