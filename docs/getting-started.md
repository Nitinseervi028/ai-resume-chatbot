# Getting Started

This guide takes you from a fresh clone to your first tailored resume.

## 1. Prerequisites

| Requirement | Needed for |
|---|---|
| Docker + Docker Compose v2 | Recommended way to run everything |
| Python 3.11+ | Running without Docker (the Docker image uses `python:3.11-slim`) |
| An [OpenRouter](https://openrouter.ai) API key | All LLM calls |
| A [Firecrawl](https://www.firecrawl.dev) API key | Scraping job postings |
| A [GitHub token](https://github.com/settings/tokens) (optional) | Raises the GitHub API rate limit from 60/hr to 5000/hr |

The GitHub token needs no scopes. The pipeline only reads public repositories.

## 2. Clone and configure

```bash
git clone https://github.com/Nitinseervi028/ai-resume-chatbot.git
cd ai-resume-chatbot
cp .env.example .env
```

Edit `.env`:

```dotenv
OPENROUTER_API_KEY=sk-or-...
FIRECRAWL_API_KEY=fc-...
GITHUB_TOKEN=            # optional
```

`.env` is listed in both `.gitignore` and `.dockerignore`, so it is never
committed or baked into the image.

## 3a. Run with Docker (recommended)

```bash
docker compose up --build
```

This builds one image (`ai-resume-tailor:latest`) and starts two services
from it:

| Service | URL | Purpose |
|---|---|---|
| `backend` | http://localhost:8000 (docs at `/docs`) | FastAPI + pipeline |
| `chat` | http://localhost:8501 | Streamlit chat UI |

`chat` waits until `backend` passes its `/health` check before it starts.
Generated results go in the `resume_data` Docker volume and survive
`docker compose down` (but not `docker compose down -v`).

To use the prebuilt image from Docker Hub instead of building it yourself,
see [Deployment → Docker Hub](deployment.md#docker-hub-image).

## 3b. Run locally (without Docker)

```bash
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Both the backend and the pipeline call `load_dotenv()`, so a `.env` file in
the project root is picked up automatically. You can also export the
variables yourself:

```bash
export OPENROUTER_API_KEY=sk-or-...
export FIRECRAWL_API_KEY=fc-...
```

Start the two processes in separate terminals:

```bash
# Terminal 1 — backend
uvicorn fastapi_backend:app --reload --port 8000

# Terminal 2 — chat UI
API_BASE_URL=http://localhost:8000 streamlit run streamlit_chat_app.py
```

Running locally creates `resume_pipeline_checkpoints.db` in the working
directory, unless you set `CHECKPOINT_DB_PATH`.

## 4. Generate your first resume

1. Open http://localhost:8501. The sidebar should show **Backend: connected**.
2. Paste a job posting URL into the chat.
3. Attach your current resume with the paperclip in the chat box (or the
   sidebar uploader). You can also type a description of your background
   instead.
4. Once it has both, the app starts generating right away. A progress line
   updates every 1.5 seconds: *Fetching job posting…* → *Job details
   extracted…* → *Scored attempt 1 (72/100) — refining…* → *Done!*
5. The reply shows the ATS summary, any GitHub projects used, and the full
   resume. Click **Download latest resume (.md)** to save it.

Follow-ups you can type afterwards:

- `shorten the summary` / `emphasize AWS more` — edits the resume and scores it again
- `show score` — shows the ATS analysis again
- `which projects` — shows the GitHub repos that were used
- `set threshold to 85` then `regenerate` — runs the pipeline again with a stricter target
- Any other question, e.g. `why is my score low?`, goes to the assistant

See [Chat UI](chat-ui.md) for the full command list.

## 5. Use the API directly

```bash
# Start a job with a resume file
curl -F job_url="https://example.com/jobs/123" \
     -F resume_file=@my_resume.pdf \
     http://localhost:8000/jobs
# -> {"job_id":"3f0c...","status":"queued"}

# Poll until status is "completed" or "failed"
curl http://localhost:8000/jobs/3f0c...

# Download the Markdown resume
curl -OJ http://localhost:8000/jobs/3f0c.../download
```

See the [API Reference](api-reference.md) for all endpoints.
