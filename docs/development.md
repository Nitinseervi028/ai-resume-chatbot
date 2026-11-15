# Development

## Project layout

```
.
├── job_resume_langgraph.py   # LangGraph pipeline: state, Pydantic tool schemas, LLM factories, nodes, routers, build_graph()
├── fastapi_backend.py        # REST API, background executor, checkpoint-backed job status
├── streamlit_chat_app.py     # Chat UI (HTTP client of the backend)
├── mcp_server.py             # MCP tools (HTTP client of the backend)
├── requirements.txt          # Python dependencies (the bottom, uncommented block is what gets installed)
├── Dockerfile                # Shared image for backend + chat
├── docker-compose.yml        # Local-build stack
├── docker-compose.hub.yml    # Stack using the Docker Hub image
├── .env.example              # Template for .env
├── .streamlit/config.toml    # Local-dev Streamlit watcher fix
├── .dockerignore / .gitignore
└── docs/                     # This documentation
```

## Dependencies (`requirements.txt`)

| Group | Packages |
|---|---|
| Pipeline | `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-openai`, `langchain-core`, `pydantic`, `requests`, `beautifulsoup4`, `python-dotenv` |
| Resume parsing | `pypdf`, `python-docx` |
| Chat UI | `streamlit` |
| MCP | `fastmcp` |
| API | `fastapi`, `uvicorn[standard]`, `python-multipart` |
| Optional (not listed) | `playwright` for `fetch_job_posting_playwright` |

The top half of `requirements.txt` is an older block that is commented out
(it listed `mcp` instead of `fastmcp`). pip ignores it.

## Local setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
uvicorn fastapi_backend:app --reload --port 8000
streamlit run streamlit_chat_app.py
```

## Running the pipeline without the API

```bash
python job_resume_langgraph.py
```

Edit the `__main__` block first: `resume_file_path`, `job_url`, and the
`thread_id` in `config`. It prints the parsed candidate profile, the
extracted job info, the ATS results, and the final resume.

You can also work interactively, for example in a notebook:

```python
from job_resume_langgraph import build_graph
app = build_graph()  # stateless
state = app.invoke({"job_url": "...", "candidate_profile": "..."})
```

Nodes are plain functions `ResumeState -> ResumeState`, so you can test them
one at a time:

```python
from job_resume_langgraph import load_text_from_file, _looks_like_a_skill_keyword, _score_repo_relevance

load_text_from_file("resume.pdf")
_looks_like_a_skill_keyword("Currently pursuing a degree")   # False
_score_repo_relevance({"name": "fastapi-demo", "language": "Python"}, ["FastAPI", "Python"])  # 2
```

## Debugging a run

Every backend job is saved under its `job_id`:

```python
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
from job_resume_langgraph import build_graph

conn = sqlite3.connect("resume_pipeline_checkpoints.db", check_same_thread=False)
app = build_graph(checkpointer=SqliteSaver(conn))
config = {"configurable": {"thread_id": "<job_id>"}}

state = app.get_state(config).values           # latest full state (includes fields the API hides)
print(state.get("ats_raw_llm_score"), state.get("ats_feedback"), state.get("github_note"))

for snap in app.get_state_history(config):      # newest first
    print(snap.metadata.get("step"), snap.next, list((snap.metadata.get("writes") or {}).keys()))
```

## Extending

### Add a pipeline node

1. Write `def my_node(state: ResumeState) -> ResumeState`. Start with
   `if state.get("error"): return state`, and return `{**state, ...}`.
2. Add any new fields to `ResumeState`.
3. In `build_graph()`, call `graph.add_node("my_node", my_node)` and update
   the edges. Use `check_error` as the router if the node can fail.
4. To expose the new fields over HTTP, add them to `JobStatusResponse` and
   `_read_job_state` in `fastapi_backend.py`.

### Switch the scraper

```python
from job_resume_langgraph import build_graph, fetch_job_posting
app = build_graph(fetch_node=fetch_job_posting)  # no Firecrawl key needed, but weaker on JS sites
```

The backend calls `build_graph(checkpointer=...)` in
`_graph_with_fresh_connection()`. Pass `fetch_node=` there to change the
scraper for the API.

### Get structured output from the LLM

Follow the existing pattern: define a Pydantic model with descriptive
`Field(description=...)` text, then bind it with
`.bind_tools([Model], tool_choice="Model")` and parse
`response.tool_calls[0]["args"]` into the model. Treat a missing tool call
as an error.

### Add an API endpoint

Open a fresh connection with `_graph_with_fresh_connection()` for each
request, close it in `finally`, and read or write state with
`graph_app.get_state(config)` / `graph_app.update_state(config, values)`.

## Known limitations / ideas

- The repo has no automated tests, linter config, or CI.
- In `/jobs/{id}/chat`, `assistant` messages in `history` are passed to the
  model as `SystemMessage` instead of `AIMessage`.
- `.docx` parsing reads paragraphs only (no tables, headers, or text boxes).
  Scanned PDFs need OCR.
- The GitHub lookup only runs for file input, not for a typed profile, even
  if that text contains a GitHub URL.
- Jobs that were running during a backend restart are not resumed.
- `mcp_server.py` is left out of the Docker image, and the top of the file
  holds an old copy that is commented out.
