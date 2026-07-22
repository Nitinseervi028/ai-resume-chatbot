# AI Resume Tailor

A LangGraph pipeline that takes a job posting URL + your old resume, and
produces a tailored, ATS-scored resume — with a chat UI and a FastAPI
backend, fully containerized.

## Architecture

```
streamlit_chat_app.py  ──HTTP──►  fastapi_backend.py  ──►  job_resume_langgraph.py
   (pure UI client)                 (single source of         (LangGraph pipeline,
                                      truth for LLM work)        all LLM calls happen here)
```

- **`job_resume_langgraph.py`** — the core pipeline: scrapes the job posting
  (Firecrawl), parses your old resume (PDF/DOCX/TXT/MD), checks your GitHub
  for relevant public projects, generates a tailored resume, scores it
  ATS-style, and loops (generate → score → retry) until it passes a
  threshold or runs out of attempts.
- **`fastapi_backend.py`** — exposes the pipeline over HTTP. Jobs run in a
  background thread pool; status/results are read from a SQLite checkpoint
  store keyed by `job_id`, so results survive a server restart.
- **`streamlit_chat_app.py`** — a chat interface. Never touches the LLM or
  pipeline directly — it's a pure client of the FastAPI backend.

## Quick start (Docker — recommended)

```bash
cp .env.example .env
# edit .env: fill in OPENROUTER_API_KEY and FIRECRAWL_API_KEY (required),
# GITHUB_TOKEN (optional, raises GitHub API rate limit)

docker compose up --build
```

- Chat UI: http://localhost:8501
- Backend API + docs: http://localhost:8000/docs

## Quick start (without Docker, for local development)

```bash
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

export OPENROUTER_API_KEY=sk-or-...
export FIRECRAWL_API_KEY=fc-...

# Terminal 1
uvicorn fastapi_backend:app --reload --port 8000

# Terminal 2
API_BASE_URL=http://localhost:8000 streamlit run streamlit_chat_app.py
```

`.streamlit/config.toml` sets the file watcher to polling mode — this
avoids a known crash when `transformers`/`torchvision` happen to be
installed elsewhere in your environment and Streamlit's default watcher
tries to introspect them. Safe to remove if you don't hit that issue.

## API keys required

| Variable | Used for | Get one at |
|---|---|---|
| `OPENROUTER_API_KEY` | All LLM calls (extraction, generation, scoring, edits) | https://openrouter.ai |
| `FIRECRAWL_API_KEY` | Scraping job postings (handles JS-rendered/bot-protected sites) | https://www.firecrawl.dev |
| `GITHUB_TOKEN` (optional) | Raises GitHub REST API rate limit 60/hr → 5000/hr | https://github.com/settings/tokens |

## Project files

```
.
├── Dockerfile              # shared image for both services
├── docker-compose.yml      # orchestrates backend + chat, shared checkpoint volume
├── requirements.txt
├── .env.example            # copy to .env and fill in
├── .dockerignore
├── .streamlit/config.toml  # local-dev file watcher fix (see above)
├── job_resume_langgraph.py # the pipeline
├── fastapi_backend.py      # HTTP API over the pipeline
└── streamlit_chat_app.py   # chat UI, pure API client
```

## Backend API reference

- `POST /jobs` — start a generation (`job_url` + `resume_file` upload OR `candidate_profile` text, optional `ats_threshold`/`max_attempts`) → `{job_id, status}`
- `GET /jobs/{job_id}` — poll status/result
- `POST /jobs/{job_id}/edit` — `{"edit_request": "..."}` free-text edit, auto re-scores
- `POST /jobs/{job_id}/chat` — `{"message": "...", "history": [...]}` freeform Q&A
- `GET /jobs/{job_id}/download` — resume as a downloadable `.md` file
- `GET /health`

Full interactive docs at `/docs` once the backend is running.
