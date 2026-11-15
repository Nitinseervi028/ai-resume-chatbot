# API Reference (`fastapi_backend.py`)

- **Base URL:** `http://localhost:8000` (in Docker, other containers reach it at `http://backend:8000`)
- **Interactive docs:** `/docs` (Swagger UI), `/redoc`, and the OpenAPI schema at `/openapi.json`
- **Auth:** none. See [Deployment → Production hardening](deployment.md#production-hardening).
- **CORS:** every origin, method, and header is allowed (`allow_origins=["*"]`).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Service info |
| `GET` | `/health` | Liveness check |
| `POST` | `/jobs` | Start a resume generation |
| `GET` | `/jobs/{job_id}` | Poll status and results |
| `POST` | `/jobs/{job_id}/edit` | Apply a free-text edit, then score again |
| `POST` | `/jobs/{job_id}/chat` | Free-form Q&A about the job and resume |
| `GET` | `/jobs/{job_id}/download` | Download the resume as `.md` |

---

### `GET /`

```json
{"service": "AI Resume Tailor API", "docs": "/docs", "health": "/health"}
```

### `GET /health`

```json
{"status": "ok"}
```

The Docker Compose healthcheck uses this endpoint, and so does the chat UI's
"Backend: connected" indicator.

---

### `POST /jobs`

Starts a pipeline run in the background and returns right away.

**Content type:** `multipart/form-data`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `job_url` | string | yes | — | Job posting URL |
| `resume_file` | file | one of these two | — | Old resume: `.pdf`, `.docx`, `.txt`, `.md` |
| `candidate_profile` | string | one of these two | — | Free-text background |
| `ats_threshold` | int | no | `80` | Score needed to pass (not validated by the server; the UI limits it to 50–100) |
| `max_attempts` | int | no | `3` | Maximum generate→score cycles (the UI limits it to 1–5) |

If both `resume_file` and `candidate_profile` are sent, **the file wins** and
the text is ignored. The file is saved to a temp file with the original
extension (`.txt` if the name has none) and deleted when the job finishes.

**Response `200`** (`JobCreateResponse`):

```json
{"job_id": "56649931-053c-4e67-9a26-8dfa41e2b226", "status": "queued"}
```

**Errors:**

| Code | When |
|---|---|
| `400` | Neither `resume_file` nor a non-blank `candidate_profile` was sent: `{"detail": "Provide either resume_file (upload) or candidate_profile (text background)."}` |
| `422` | `job_url` is missing, or a field has the wrong type (FastAPI validation error) |

**Examples:**

```bash
# With a resume file
curl -F job_url="https://example.com/jobs/123" -F resume_file=@resume.pdf \
     -F ats_threshold=85 -F max_attempts=4 http://localhost:8000/jobs

# With a text profile
curl -F job_url="https://example.com/jobs/123" \
     -F candidate_profile="3 years Python backend, FastAPI, AWS, PostgreSQL. B.Tech CS." \
     http://localhost:8000/jobs
```

```python
import requests, time

r = requests.post(
    "http://localhost:8000/jobs",
    data={"job_url": "https://example.com/jobs/123"},
    files={"resume_file": ("resume.pdf", open("resume.pdf", "rb"))},
)
job_id = r.json()["job_id"]
while (s := requests.get(f"http://localhost:8000/jobs/{job_id}").json())["status"] not in ("completed", "failed"):
    time.sleep(2)
print(s["ats_score"], s["resume_output"])
```

---

### `GET /jobs/{job_id}`

Returns the current status plus whatever state the pipeline has saved so
far. Poll this endpoint (the chat UI polls every 1.5 s, the MCP server every
2 s).

**Response `200`** (`JobStatusResponse`). Every field except `job_id` and
`status` may be `null`:

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | |
| `status` | string | `queued` \| `running` \| `completed` \| `failed` |
| `error` | string | Set when `status == "failed"` |
| `company_name`, `job_role`, `job_description` | string | Available once `extract_job_info` has run |
| `key_responsibilities`, `requirements` | string[] | |
| `candidate_name` | string | Only with file input |
| `github_url` | string | Only with file input |
| `github_projects` | object[] | `{name, description, language, url, stars, relevance_score}` |
| `ats_score` | int | Capped final score |
| `ats_keyword_coverage` | int | 0–100 |
| `attempt_count` | int | |
| `matched_keywords`, `missing_keywords` | string[] | |
| `final_note` | string | E.g. the threshold was not reached |
| `resume_output` | string | Markdown. **Only filled in when `status == "completed"`** |

Fields such as `ats_raw_llm_score`, `ats_feedback`, the `candidate_*` lists,
and the `best_*` fields are stored in the checkpoint but are **not**
returned by the API.

**Example (failed run):**

```json
{
  "job_id": "56649931-053c-4e67-9a26-8dfa41e2b226",
  "status": "failed",
  "error": "FIRECRAWL_API_KEY environment variable is not set.",
  "company_name": null,
  "...": "...",
  "resume_output": null
}
```

**Errors:** `404 {"detail": "Job not found"}` when there is no in-memory
flag and no checkpoint for that ID.

**How status is decided after a restart:** see
[Architecture → State persistence](architecture.md#state-persistence).

---

### `POST /jobs/{job_id}/edit`

Applies a natural-language edit to the current resume, scores it again,
polishes the Skills section, and saves the result as a new checkpoint.
**This call is synchronous** and usually takes 10–40 s (three LLM calls).
The chat UI and MCP server use a 90 s client timeout.

**Body (`application/json`):**

```json
{"edit_request": "Shorten the summary to two lines and emphasize AWS."}
```

**Response `200`:** the same `JobStatusResponse` as `GET /jobs/{job_id}`,
with the updated resume and scores.

Behavior notes:

- The edit prompt only allows rewording, reorganizing, shortening, or
  emphasizing content that is already true. Nothing new may be invented.
- Scoring again increments `attempt_count`.
- If scoring or polishing fails, the edited resume is still saved (the
  scoring is best-effort).

**Errors:**

| Code | When |
|---|---|
| `404` | No saved resume exists for this `job_id` yet: `{"detail": "No completed resume found for this job_id yet."}` |
| `502` | The edit LLM call failed: `{"detail": "Edit failed: ..."}` |
| `422` | `edit_request` is missing |

---

### `POST /jobs/{job_id}/chat`

Free-form Q&A that uses the job's saved state as context. It never changes
the resume. It works even for an unknown `job_id` (it just answers without
context).

**Body (`application/json`):**

```json
{
  "message": "Why is my ATS score low?",
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

- `history` is optional (default `[]`). Only the **last 8** messages are used.
- When a resume exists, the system prompt includes the role, the company,
  the current ATS score, the missing keywords, and the full resume.

**Response `200`** (`ChatResponse`):

```json
{"reply": "Your score is capped by keyword coverage: Kubernetes and Terraform are missing..."}
```

**Errors:** `502 {"detail": "Chat failed: ..."}` when the LLM call fails
(for example, a missing or invalid `OPENROUTER_API_KEY`).

---

### `GET /jobs/{job_id}/download`

Returns the completed resume as a file download.

- `Content-Type: text/markdown`
- `Content-Disposition: attachment; filename="<Company_Name>_resume.md"`
  (spaces in the company name become underscores; the fallback name is
  `tailored_resume.md`)

**Errors:** `404 {"detail": "Resume not ready yet (or job hasn't completed)."}`.
For an unknown ID, the response is `404 {"detail": "Job not found"}`.

```bash
curl -OJ http://localhost:8000/jobs/<job_id>/download
```
