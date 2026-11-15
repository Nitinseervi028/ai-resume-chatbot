# MCP Server (`mcp_server.py`)

Exposes the backend as [Model Context Protocol](https://modelcontextprotocol.io)
tools, so MCP clients such as Claude Desktop or Claude Code can generate,
check, edit, and discuss resumes from inside a conversation.

Like the chat UI, it is a **pure API client**. It needs no LLM keys of its
own, but the FastAPI backend must already be running.

## Requirements

- The backend running and reachable (`uvicorn fastapi_backend:app --port 8000`
  or `docker compose up backend`)
- `fastmcp` and `requests` (both in `requirements.txt`)
- `mcp_server.py` is **not** copied into the Docker image. Run it on the
  machine where your MCP client runs.

## Running

```bash
API_BASE_URL=http://localhost:8000 python mcp_server.py   # stdio transport
```

To test it interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python mcp_server.py
```

## Client configuration

### Claude Desktop

Edit `claude_desktop_config.json`:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "resume-tailor": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["/absolute/path/to/ai-resume-chatbot/mcp_server.py"],
      "env": { "API_BASE_URL": "http://localhost:8000" }
    }
  }
}
```

On Windows, use paths like `C:\\path\\to\\venv\\Scripts\\python.exe`.
Restart Claude Desktop afterwards. The four tools then appear under the
tools icon.

### Claude Code

```bash
claude mcp add resume-tailor -e API_BASE_URL=http://localhost:8000 -- \
  /absolute/path/to/venv/bin/python /absolute/path/to/ai-resume-chatbot/mcp_server.py
```

## Tools

### `generate_tailored_resume`

| Argument | Type | Default | Description |
|---|---|---|---|
| `job_url` | str | — | Job posting URL |
| `resume_file_path` | str? | `None` | **Absolute local path** to the resume (`.pdf`/`.docx`/`.txt`/`.md`). The file is read by the MCP server and uploaded to the backend |
| `candidate_profile` | str? | `None` | Free-text background (used when there is no file) |
| `ats_threshold` | int | `80` | Score needed to pass |
| `max_attempts` | int | `3` | Maximum generate→score cycles |

**Blocks** while it polls every 2 s, until the job completes, fails, or
reaches the **300 s** timeout. On success it returns a text block with the
job ID, company, role, ATS score, attempts, coverage, note, keywords, GitHub
repos, and the full Markdown resume. On timeout it returns the `job_id` so
you can follow up with `check_job_status`.

### `check_job_status(job_id)`

Returns the formatted result when the job is completed, `Failed: <error>`
when it failed, or `Status: queued|running` otherwise.

### `edit_resume(job_id, edit_request)`

Calls `POST /jobs/{job_id}/edit` (90 s timeout) and returns the formatted,
re-scored result.

### `chat_about_resume(job_id, message)`

Calls `POST /jobs/{job_id}/chat` with an empty history (60 s timeout) and
returns the reply. Each call is independent, because the MCP client keeps
the conversation itself.

## Error handling

All HTTP calls go through `_safe_request`, which **never raises**. It
returns a readable error string for:

- Network errors: `Could not reach backend at ...`
- HTTP 4xx/5xx: `Backend returned HTTP 404: {"detail": ...}` (first 300 characters of the body)
- Bodies that aren't JSON, such as an HTML error page from a proxy: `Backend response wasn't valid JSON ...`

## Example prompts

> Tailor my resume at `/Users/me/Documents/resume.pdf` for https://example.com/jobs/123 with a threshold of 85.

> Check the status of job 3f0c….

> Edit that resume to emphasize my Kubernetes experience and shorten the summary.

> What keywords am I still missing for that job?

## Note on the file

The top of `mcp_server.py` contains an older, fully commented-out copy of
the server that imports `mcp.server.fastmcp`. Only the second, uncommented
half (which imports `fastmcp`) runs.
