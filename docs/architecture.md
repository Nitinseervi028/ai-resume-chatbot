# Architecture

## Components

| File | Role | Talks to | Needs API keys? |
|---|---|---|---|
| `job_resume_langgraph.py` | The core LangGraph pipeline. Every LLM call happens here. | OpenRouter, Firecrawl, GitHub REST API | Yes |
| `fastapi_backend.py` | HTTP API around the pipeline. Runs jobs in the background and reads results from the checkpoint store. | The pipeline (Python import), SQLite | Yes, passed through to the pipeline |
| `streamlit_chat_app.py` | Chat UI. Only an HTTP client of the backend. | Backend over HTTP | No |
| `mcp_server.py` | MCP server that exposes the backend as four tools. Only an HTTP client of the backend. | Backend over HTTP | No |

The backend is the **single source of truth** for pipeline and LLM logic.
The two clients never import the pipeline. That means:

- API keys only need to be configured on the backend.
- Every client (the chat UI, the MCP server, `curl`, or a future mobile app)
  behaves the same way.
- In Docker, the `chat` container needs neither the `.env` file nor the
  data volume.

## Request lifecycle

```
Client                      fastapi_backend.py                      job_resume_langgraph.py
  │  POST /jobs (form)            │                                          │
  │ ─────────────────────────────►│ save upload to a temp file               │
  │                               │ job_id = uuid4()                         │
  │                               │ JOB_FLAGS[job_id] = queued               │
  │                               │ executor.submit(_run_job) ──────────────►│ graph.invoke(state,
  │◄──────── {job_id, queued} ────│                                          │   thread_id=job_id)
  │                               │                                          │ each node writes a
  │  GET /jobs/{id}  (poll)       │                                          │ checkpoint to SQLite
  │ ─────────────────────────────►│ graph.get_state(thread_id) ◄─────────────│
  │◄──── status + partial state ──│                                          │
  │            ...                │                                          │
  │                               │ JOB_FLAGS[job_id] = completed / failed ◄─│ returns final state
  │  GET /jobs/{id}               │                                          │ temp file deleted
  │◄──── status=completed + resume│                                          │
```

1. `POST /jobs` checks that the request has either `resume_file` or
   `candidate_profile`, writes an uploaded file to a `NamedTemporaryFile`,
   creates a UUID `job_id`, and hands `_run_job` to a
   `ThreadPoolExecutor(max_workers=4)`. It returns right away.
2. `_run_job` builds a graph backed by **its own** SQLite connection and calls
   `graph.invoke(initial_state, config={"configurable": {"thread_id": job_id}}`.
3. LangGraph saves a checkpoint after every node, so polling with
   `GET /jobs/{job_id}` shows partial progress: company name, then candidate
   name, then attempt count and score, and so on.
4. When the run ends, the in-memory flag becomes `completed` or `failed` and
   the temp upload is deleted.

## State persistence

- **Checkpoint store:** `langgraph.checkpoint.sqlite.SqliteSaver`, a file at
  `CHECKPOINT_DB_PATH`. The default is `resume_pipeline_checkpoints.db` in the
  working directory. Docker uses `/app/data/resume_pipeline_checkpoints.db` on
  the `resume_data` volume.
- **Key:** LangGraph's `thread_id`, which is the backend's `job_id`.
- **What is stored:** the full `ResumeState` after every node. That includes
  the scraped job text, the extracted candidate data (from up to 8,000
  characters of raw resume text), every resume attempt, and the scores.
  Treat the database as sensitive personal data.
- **In-memory flags (`JOB_FLAGS`):** a dict guarded by a `threading.Lock`
  that only tracks `queued` / `running` / `completed` / `failed` (plus the
  error message). It is lost on restart. `GET /jobs/{id}` then works out the
  status from the checkpoint itself:
  - `error` is set → `failed`
  - `resume_output` is set and `snapshot.next` is empty → `completed`
  - anything else → `running`

  A job that was **running when the server restarted** is never resumed. It
  keeps reporting `running` until you resubmit it.
- **Edits** (`POST /jobs/{id}/edit`) are saved with `graph.update_state`,
  which adds a new checkpoint on the same thread. Later reads see the edited
  resume.

## Concurrency model

- Pipeline runs are synchronous and blocking (LLM calls and HTTP scraping),
  so they run in a thread pool of **4 workers**. A fifth concurrent job
  queues inside the executor while its flag stays `queued`.
- Each job or request opens a **new** `sqlite3` connection with
  `timeout=30, check_same_thread=False`. `check_same_thread=False` is needed
  because LangGraph's Pregel executor runs nodes on its own worker threads,
  even inside one `.invoke()`. Connections are never shared across jobs.
- If you need more concurrency, swap `SqliteSaver` for
  `langgraph-checkpoint-postgres`, which has the same checkpointer interface.

## Key design decisions

| Decision | Why |
|---|---|
| **Forced tool calls for structured output** (`bind_tools([Model], tool_choice="Model")`) | Extraction and scoring always return parseable Pydantic data, never free text. |
| **LLM clients built lazily** (`get_llm()` and friends read `OPENROUTER_API_KEY` on every call) | A key set after import, for example by a UI, is still picked up. Building a client makes no network call. |
| **Deterministic guards around LLM output** | A regex fallback finds the GitHub URL, the ATS score is capped at keyword coverage + 10, and `_looks_like_a_skill_keyword` filters keywords before polishing. The pipeline does not fully trust the model. |
| **Errors are stored in state instead of raised** | Every node returns early when `state["error"]` is set, and the routers send errors to `END`. A failure produces a clear message instead of a stack trace. |
| **GitHub enrichment never fails the run** | Failures go into `github_note` and the run continues to generation. |
| **Firecrawl is the default scraper** | It handles JavaScript-rendered and bot-protected job boards (Naukri, LinkedIn, many ATS sites) and returns Markdown that LLMs parse well. |
| **Non-root container user** (`appuser`) | Basic container hardening. |
