# Configuration

## Environment variables

| Variable | Used by | Required | Default | Description |
|---|---|---|---|---|
| `OPENROUTER_API_KEY` | backend / pipeline | **yes** | — | Key for every LLM call, sent to `https://openrouter.ai/api/v1`. Read on every call, so you can change it without restarting the process |
| `FIRECRAWL_API_KEY` | backend / pipeline | **yes** (with the default scraper) | — | Key for `fetch_job_posting_firecrawl` |
| `GITHUB_TOKEN` | backend / pipeline | no | — | Raises the GitHub REST rate limit from 60/hr to 5000/hr. No scopes needed |
| `CHECKPOINT_DB_PATH` | backend / pipeline `__main__` | no | `resume_pipeline_checkpoints.db` (Docker: `/app/data/resume_pipeline_checkpoints.db`) | SQLite checkpoint file |
| `API_BASE_URL` | chat UI, MCP server | no | `http://localhost:8000` (Compose: `http://backend:8000`) | Backend base URL |
| `STREAMLIT_SERVER_HEADLESS` | chat UI | no | `true` in the image | Don't try to open a browser |
| `STREAMLIT_BROWSER_GATHER_USAGE_STATS` | chat UI | no | `false` in the image | Turn off Streamlit telemetry |
| `STREAMLIT_SERVER_FILE_WATCHER_TYPE` | chat UI | no | `none` in the image | Turn off the file watcher in containers |

`fastapi_backend.py` and `job_resume_langgraph.py` both call
`python-dotenv`'s `load_dotenv()`, so a `.env` file in the working directory
is loaded automatically. Variables already in the environment take
precedence.

## Models

The models are defined in `job_resume_langgraph.py`. Change them there.

| Function | Model | Temperature | `max_tokens` | Used for |
|---|---|---|---|---|
| `get_llm()` | `gpt-4o-mini` | 0.3 | 2000 | Resume generation, Skills polish, API edit and chat |
| `get_extraction_llm()` | `gpt-4o-mini` | 0 | 1000 | `JobInfo` extraction (forced tool call) |
| `get_candidate_extraction_llm()` | `gpt-4o-mini` | 0 | 1500 | `CandidateProfile` extraction (forced tool call) |
| `get_scoring_llm()` | `gpt-4o` | 0 | 800 | `ATSScore` scoring (forced tool call) |

All of them use `langchain_openai.ChatOpenAI` with
`base_url="https://openrouter.ai/api/v1"`. Any OpenRouter model that
supports tool calling should work. You can use the OpenRouter-style model
ID (for example, `openai/gpt-4o-mini` or `anthropic/claude-3.5-sonnet`).
The structured-extraction functions need a model that supports
`tool_choice` with a named tool.

To use a different OpenAI-compatible provider (OpenAI itself, Azure, a
local vLLM or Ollama server), change `base_url` and the key lookup in
`_get_api_key()`.

## Tunable constants

| Location | Constant | Value | Effect |
|---|---|---|---|
| pipeline | `ats_threshold` (state) | 80 | Default passing score |
| pipeline | `max_attempts` (state) | 3 | Default cap on generate→score cycles |
| pipeline | score cap | `min(raw, coverage + 10)` in `score_resume` | How far the LLM score can exceed keyword coverage |
| pipeline | raw job text limit | 15,000 characters | Scraped text passed to extraction |
| pipeline | raw resume text limit | 8,000 characters (stored) | `raw_candidate_resume_text` |
| pipeline | min job text | 200 characters | Below this, a scrape counts as failed |
| pipeline | min resume text | 50 characters | Below this, file parsing counts as failed |
| pipeline | GitHub top-N | 5 repos | `fetch_github_projects` |
| pipeline | `FIRECRAWL_API_URL` | `https://api.firecrawl.dev/v1/scrape` | |
| pipeline | `GITHUB_API_BASE` | `https://api.github.com` | |
| backend | `ThreadPoolExecutor(max_workers=...)` | 4 | Jobs that can run at once |
| backend | SQLite `timeout` | 30 s | Lock wait time |
| chat UI | `POLL_INTERVAL_SECONDS` / `POLL_TIMEOUT_SECONDS` | 1.5 / 300 | Job polling |
| MCP | `POLL_INTERVAL_SECONDS` / `POLL_TIMEOUT_SECONDS` | 2 / 300 | Job polling |

## `.streamlit/config.toml`

```toml
[server]
fileWatcherType = "poll"
```

Used only for local development (`.streamlit/` is in `.dockerignore`). See
[Troubleshooting](troubleshooting.md#streamlit-crashes-with-modulenotfounderror-on-startup).
