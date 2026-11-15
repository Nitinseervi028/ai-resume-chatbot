# Deployment

## Docker image

One image (`Dockerfile`) is shared by both services:

- Base image `python:3.11-slim`. No extra system packages: `pypdf` and
  `python-docx` are pure Python, and BeautifulSoup uses the standard
  library's `html.parser`.
- Copies only `job_resume_langgraph.py`, `streamlit_chat_app.py`, and
  `fastapi_backend.py`. `mcp_server.py` is **not** included.
- Runs as the non-root `appuser`. `/app/data` exists and belongs to that user.
- Sets `CHECKPOINT_DB_PATH=/app/data/resume_pipeline_checkpoints.db` and the
  Streamlit headless, telemetry, and watcher variables.
- Exposes `8000` and `8501`. There is **no default command**. Run with no
  command, it only prints a usage hint. Compose supplies the command for
  each service.

Build and run by hand:

```bash
docker build -t ai-resume-tailor .
docker volume create resume_data

docker run -d --name backend -p 8000:8000 --env-file .env \
  -v resume_data:/app/data ai-resume-tailor \
  uvicorn fastapi_backend:app --host 0.0.0.0 --port 8000

docker run -d --name chat -p 8501:8501 -e API_BASE_URL=http://host.docker.internal:8000 \
  ai-resume-tailor streamlit run streamlit_chat_app.py --server.address=0.0.0.0 --server.port=8501
```

On Linux, add `--add-host=host.docker.internal:host-gateway` to the chat
container, or put both containers on a shared user-defined network and use
`http://backend:8000`.

## `docker-compose.yml` (build locally)

| Service | Command | Ports | Volumes | Env |
|---|---|---|---|---|
| `backend` | `uvicorn fastapi_backend:app --host 0.0.0.0 --port 8000` | `8000:8000` | `resume_data:/app/data` | `.env` |
| `chat` | `streamlit run streamlit_chat_app.py --server.address=0.0.0.0 --server.port=8501` | `8501:8501` | — | `API_BASE_URL=http://backend:8000` |

- `backend` has a healthcheck that calls `/health` with Python's `urllib`
  every 15 s (5 retries, 10 s start period).
- `chat` waits for `depends_on: backend: condition: service_healthy`.
- Both services use `restart: unless-stopped`.

```bash
docker compose up --build -d     # start
docker compose logs -f backend   # follow backend logs
docker compose down              # stop (data volume kept)
docker compose down -v           # stop and DELETE all saved results
```

## Docker Hub image

`docker-compose.hub.yml` runs the prebuilt image
`seervinitinseervi/ai-resume-tailor:latest` instead of building one. Its
volume is named `resume_data_hub_test`, so its data is separate from the
local-build setup.

```bash
cp .env.example .env    # fill in keys
docker compose -f docker-compose.hub.yml up -d
```

To publish a new version:

```bash
docker build -t seervinitinseervi/ai-resume-tailor:latest .
docker push seervinitinseervi/ai-resume-tailor:latest
```

## Backups

All persistent state is one SQLite file on the volume:

```bash
docker run --rm -v ai-resume-chatbot_resume_data:/data -v "$PWD":/backup alpine \
  cp /data/resume_pipeline_checkpoints.db /backup/
```

The volume name is prefixed with the Compose project name, which is usually
the directory name. Check it with `docker volume ls`.

## Production hardening

The defaults are meant for local or personal use. Before you expose the
app publicly:

| Area | Current state | Recommendation |
|---|---|---|
| Authentication | None on any endpoint | Put the API behind an auth proxy, or add an API-key/OAuth dependency in FastAPI. Anyone who can reach it can spend your OpenRouter and Firecrawl credits |
| CORS | `allow_origins=["*"]` with credentials | Limit it to your frontend's domains |
| Job IDs | UUID4, which works as a bearer secret | Fine for personal use. Add per-user ownership for multi-user use |
| Upload limits | No size or type check on the server | Limit the request body size at the proxy and check extensions server-side |
| `ats_threshold` / `max_attempts` | Not validated on the server | Add `Form(..., ge=..., le=...)` limits so callers can't request very long loops |
| Concurrency | 4 worker threads, SQLite | Switch to `langgraph-checkpoint-postgres` and a real job queue for many users |
| Interrupted jobs | Jobs running at shutdown are never resumed | Resubmit them, or add startup logic that requeues unfinished threads |
| Data retention | Checkpoints (resumes = personal data) kept forever | Add a cleanup job and tell users how long data is kept |
| TLS | Plain HTTP | Terminate TLS at a reverse proxy (nginx, Caddy, Traefik, or a cloud load balancer) |
| Exposed ports | Backend `8000` published | If only the chat UI is public, stop publishing `8000` and keep the backend on the internal Docker network |
