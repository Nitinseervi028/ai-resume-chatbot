# AI Resume Tailor — Documentation

Complete documentation for the AI Resume Tailor: a LangGraph pipeline that
turns a job posting URL plus your existing resume into a tailored,
ATS-scored resume, served through a FastAPI backend, a Streamlit chat UI,
and an MCP server.

## Contents

| Document | What it covers |
|---|---|
| [Getting Started](getting-started.md) | Prerequisites, API keys, running with Docker or locally, first resume |
| [Architecture](architecture.md) | Components, data flow, state persistence, design decisions |
| [Pipeline](pipeline.md) | Every LangGraph node, the state schema, prompts, ATS scoring loop |
| [API Reference](api-reference.md) | Every FastAPI endpoint with request/response schemas and examples |
| [Chat UI](chat-ui.md) | Using the Streamlit app: commands, sidebar settings, details panel |
| [MCP Server](mcp-server.md) | Using the pipeline from Claude Desktop / any MCP client |
| [Configuration](configuration.md) | Environment variables, models, tunable constants |
| [Deployment](deployment.md) | Docker image, Compose files, Docker Hub, production hardening |
| [Development](development.md) | Project layout, running the pipeline as a script, extending it |
| [Troubleshooting](troubleshooting.md) | Common errors and how to fix them |

## At a glance

```
┌──────────────────────┐    HTTP    ┌──────────────────────┐   Python   ┌───────────────────────────┐
│ streamlit_chat_app.py│ ─────────► │  fastapi_backend.py  │ ─────────► │  job_resume_langgraph.py  │
│   (chat UI, :8501)   │            │   (REST API, :8000)  │            │  (LangGraph pipeline,     │
└──────────────────────┘            │                      │            │   all LLM calls)          │
┌──────────────────────┐    HTTP    │  SQLite checkpoints  │            └─────────────┬─────────────┘
│    mcp_server.py     │ ─────────► │  (thread_id=job_id)  │                          │
│ (MCP tools, stdio)   │            └──────────────────────┘                          ▼
└──────────────────────┘                                          OpenRouter · Firecrawl · GitHub API
```

- **Input:** a job posting URL + either a resume file (`.pdf`, `.docx`,
  `.txt`, `.md`) or a free-text description of your background.
- **Output:** a tailored Markdown resume, an ATS score (0–100), keyword
  coverage, matched/missing keywords, and any relevant GitHub projects that
  were used as evidence.
