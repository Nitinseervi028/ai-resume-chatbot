# Chat UI (`streamlit_chat_app.py`)

A Streamlit chat interface. It is a **pure HTTP client** of the backend: it
never imports the pipeline and never needs API keys.

```bash
API_BASE_URL=http://localhost:8000 streamlit run streamlit_chat_app.py
# open http://localhost:8501
```

## Conversation flow

The app collects two things, a **job URL** and a **candidate profile**
(an uploaded file or typed background), in any order, in one message or
several. As soon as it has both, it starts generating.

| You send | The app does |
|---|---|
| A message containing a URL (`https?://...`) | Saves the first URL as the job URL |
| A file attachment (`.pdf`, `.docx`, `.txt`, `.md`) | Saves it as the resume (replaces any typed background) |
| Plain text after the URL, with no resume yet | Saves that text as your background and starts generating |
| Both pieces present | Calls `POST /jobs`, then polls `GET /jobs/{id}` every 1.5 s (timeout after 5 minutes) |

While a job runs, a progress line is inferred from the fields that have
been filled in so far:

`Queued...` → `Fetching job posting...` → `Job details extracted -- reading your resume...`
→ `Resume parsed -- checking GitHub, then writing resume...` → `Writing tailored resume (attempt N)...`
→ `Scored attempt N (score/100) -- refining...` → `Done!`

The result message shows:

1. An ATS summary: score, attempts, keyword coverage, any note, and the matched and missing keywords
2. A GitHub summary, if a GitHub URL was found in the resume
3. The full resume in Markdown

A **Download latest resume (.md)** button appears under the reply.

## Chat commands

Commands are matched by keywords in your message (case-insensitive):

| Intent | Example phrases | Needs a result? | Action |
|---|---|---|---|
| Regenerate | `regenerate`, `start over`, `redo the resume`, `run again`, `generate again` | no (but needs a URL and profile) | Starts a new job with the current inputs and settings |
| Set threshold | `set threshold to 85` (must contain "threshold" and a number) | no | Sets the threshold (limited to 50–100). Say `regenerate` to use it |
| Show ATS analysis | `ats score`, `show score`, `my score`, `missing keyword`, `matched keyword`, `show analysis`, `show details` | yes | Shows the ATS summary again |
| Show GitHub | `github`, `repo`, `repos`, `projects used`, `which projects` | yes | Lists the repos used as evidence |
| Edit resume | `shorten`, `make it shorter`, `make it longer`, `add `, `remove `, `rewrite`, `change the`, `emphasize`, `focus more on`, `reword`, `edit the resume`, `update the resume`, `tweak`, `revise` | yes | Calls `POST /jobs/{id}/edit` with your message |
| Anything else | `why is my score low?` | no | Calls `POST /jobs/{id}/chat` with the last 8 messages |

The keyword matching is loose. For example, *"can you **add**ress …"* does
not match (`add ` needs a trailing space), but *"I want to **add** a
question"* is treated as an edit once a resume exists. Rephrase if a
message is routed the wrong way.

Commands are checked in the order of the table. Regenerate wins over
everything else, and the threshold command wins over show and edit.

## Sidebar

| Control | Default | Description |
|---|---|---|
| Backend API URL | `$API_BASE_URL` or `http://localhost:8000` | Change it to point at another backend. Kept across resets |
| Backend status | — | Green when `GET /health` answers within 3 s |
| ATS score threshold | 80 (50–100, step 5) | Sent as `ats_threshold` |
| Max generate/score attempts | 3 (1–5) | Sent as `max_attempts` |
| Captured so far | — | Shows the current job URL and resume source |
| Reset conversation | — | Clears messages, inputs, and results (keeps the backend URL) |
| File uploader | — | Fallback for older Streamlit versions that don't support files in `st.chat_input` |

## Details panel

After a successful run, an expandable **Details** section shows three tabs:

- **Job Info:** company, role, description, responsibilities, requirements
- **Candidate Profile:** name, GitHub URL, and matched repos (file input only)
- **ATS Scoring:** final score, keyword coverage %, attempts used, any
  warning note, and the matched and missing keyword lists

## Streamlit compatibility

- `st.chat_input(..., accept_file=True, file_type=[...])` needs a recent
  Streamlit version. On older versions the app catches the `TypeError` and
  falls back to a text-only chat input. Use the sidebar uploader to attach
  your resume in that case.
- `.streamlit/config.toml` sets `fileWatcherType = "poll"` for local
  development (see [Troubleshooting](troubleshooting.md#streamlit-crashes-with-modulenotfounderror-on-startup)).
  The Docker image turns the watcher off (`STREAMLIT_SERVER_FILE_WATCHER_TYPE=none`).

## State

All UI state lives in `st.session_state`, which is per browser tab and lost
on refresh. The results themselves stay on the backend under the `job_id`,
so you can still fetch them through the API after a refresh.
