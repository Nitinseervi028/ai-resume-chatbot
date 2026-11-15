# Troubleshooting

Pipeline errors show up as `status: "failed"` with an `error` string from
`GET /jobs/{id}`, and as a ❌ message in the chat UI. Find your message below.

## Configuration / keys

### `FIRECRAWL_API_KEY environment variable is not set.`
The backend can't see the key. With Docker, check that `.env` sits next to
`docker-compose.yml` and contains `FIRECRAWL_API_KEY=...`, then run
`docker compose up -d --force-recreate backend`. Locally, export the
variable or create `.env` in the directory you start `uvicorn` from.

### `OPENROUTER_API_KEY is not set. ...`
Same fix as above, for the OpenRouter key.

### `Chat failed: Error code: 401 ...` / `Structured extraction failed (AuthenticationError) ...`
The OpenRouter key is invalid or out of credits. Check it at
https://openrouter.ai/keys.

## Job scraping

### `Firecrawl request failed (HTTPError): 402 ...` / `429 ...`
You are out of Firecrawl credits, or you hit its rate limit.

### `Firecrawl returned almost no content (N chars) for <url>.`
The page has no readable job description: it needs a login, has expired, or
redirects to a search page. Open the URL in a private browser window. Use
the job's own page, not a search-results link.

### `Fetched page but extracted almost no text ...` (requests scraper only)
The site renders with JavaScript or blocks bots. Switch back to the
Firecrawl scraper, or use `fetch_job_posting_playwright`.

## Resume parsing

### `Unsupported resume file type '.xyz'. Supported: .pdf, .docx, .txt, .md`
Convert the file. `.doc` (old Word format) is not supported. Save it as `.docx`.

### `Extracted almost no text (N chars) from the resume file. ...`
Usually a scanned or image-only PDF. Run OCR on it first (for example
`ocrmypdf in.pdf out.pdf`), export a text-based PDF, or paste your
background as text instead.

### Candidate details look incomplete
`.docx` parsing reads paragraph text only. Content inside tables, text
boxes, or headers is skipped. Export the resume to PDF and try again.

## Scoring

### The score never reaches the threshold
The final score is capped at **keyword coverage + 10**, and the pipeline
never invents skills you don't have. If the job asks for things your resume
doesn't show, the score stays low on purpose. You can:
- look at the missing keywords (`show score` in chat), and add any you
  really have to your source resume
- lower the threshold, or raise `max_attempts` (up to 5 in the UI)

When attempts run out, the best attempt is returned with a `final_note`
explaining what happened.

### `ATS scoring failed ...` / `Scorer did not return a tool call ...`
The scoring model (`gpt-4o`) failed or didn't return a tool call. Retry. If
it keeps happening, check that the model is available on OpenRouter for
your account.

## GitHub projects

The GitHub step never fails the run. Read `github_note` in the checkpoint
(see [Development → Debugging a run](development.md#debugging-a-run)):

| Note | Fix |
|---|---|
| `No GitHub URL found in the resume.` | Put the full `https://github.com/<user>` URL in your resume |
| `GitHub lookup failed ... 403 ...` | Rate limited. Set `GITHUB_TOKEN` |
| `... none matched the job's keywords.` | Add descriptions and topics to your repos |

GitHub lookup only runs when you upload a resume file, not for a typed
background.

## Chat UI

### Sidebar shows "Backend: unreachable"
- Locally: is `uvicorn` running on the URL shown in the sidebar? Try
  `curl http://localhost:8000/health`.
- Docker: run `docker compose ps`. `chat` only starts after `backend` is
  healthy. Inside Docker the URL must be `http://backend:8000`, not `localhost`.

### `Timed out waiting for the backend to finish (5 min).`
The job may still be running. Read its `job_id` from the backend logs and
check `GET /jobs/{job_id}`. Long runs usually mean a slow model or many
retry attempts.

### No paperclip / can't attach a file in the chat box
Your Streamlit version doesn't support `accept_file`. Use the sidebar
uploader, or upgrade with `pip install -U streamlit`.

### Streamlit crashes with `ModuleNotFoundError` on startup
Streamlit's default file watcher inspects every installed module, which can
fail when `transformers` or `torchvision` are installed. The included
`.streamlit/config.toml` (`fileWatcherType = "poll"`) avoids this. Make sure
you run `streamlit` from the project root so the file is found.

### My message was treated as an edit (or not)
Commands are matched by keywords (see [Chat UI → Chat commands](chat-ui.md#chat-commands)).
Rephrase, for example start with `edit the resume: ...` to force an edit, or
avoid words like `add`, `remove`, `rewrite` when you only want to ask a question.

## Backend / persistence

### A job is stuck in `running` after a restart
Jobs are not resumed after a restart. Submit it again with `POST /jobs`.

### `database is locked`
Too many concurrent writers for SQLite (the lock wait is 30 s). Reduce
concurrency, or switch to `langgraph-checkpoint-postgres`.

### Reset all data
```bash
docker compose down -v                      # Docker
rm resume_pipeline_checkpoints.db*          # local
```
