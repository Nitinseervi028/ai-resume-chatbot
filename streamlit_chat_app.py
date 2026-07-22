"""
Chat-based Streamlit UI for the Job URL -> Resume Generator pipeline.

This is a PURE API CLIENT -- it never imports job_resume_langgraph.py or
calls any LLM directly. Every action (starting a generation, editing the
resume, asking a question) goes over HTTP to fastapi_backend.py, which is
the single source of truth for all pipeline/LLM logic. This means:
  - API keys (OPENROUTER_API_KEY, FIRECRAWL_API_KEY) only need to be
    configured on the backend, never typed into this UI.
  - Any other client (mobile app, another frontend) gets identical
    behavior, since they all go through the same API.

Run (standalone, pointing at a local backend):
    streamlit run streamlit_chat_app.py

Point at a specific backend URL:
    API_BASE_URL=http://localhost:8000 streamlit run streamlit_chat_app.py

In Docker (see docker-compose.yml), API_BASE_URL is set to
http://backend:8000 so the chat container reaches the backend container
by its service name over the Docker network.

Install:
    pip install streamlit requests --break-system-packages
"""

import os
import re
import time
import uuid

import requests
import streamlit as st

st.set_page_config(page_title="AI Resume Tailor - Chat", page_icon="\U0001F4AC", layout="wide")

URL_RE = re.compile(r"https?://\S+")
API_BASE_DEFAULT = os.getenv("API_BASE_URL", "http://localhost:8000")
POLL_INTERVAL_SECONDS = 1.5
POLL_TIMEOUT_SECONDS = 300  # 5 min safety cap


# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
DEFAULTS = {
    "messages": [],           # [{"role": "user"/"assistant", "content": str}]
    "job_url": None,
    "resume_bytes": None,
    "resume_filename": None,
    "candidate_profile_text": None,
    "ats_threshold": 80,
    "max_attempts": 3,
    "job_id": None,           # backend job_id, once a generation has started
    "final_state": None,      # last known JobStatusResponse (as dict)
    "session_id": uuid.uuid4().hex[:8],   # pre-job chat thread id fallback
    "api_base_url": API_BASE_DEFAULT,
    "sidebar_file_signature": None,
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


def reset_conversation():
    for k, v in DEFAULTS.items():
        if k == "session_id":
            st.session_state[k] = uuid.uuid4().hex[:8]
        elif k == "api_base_url":
            pass  # keep the user's backend URL setting across resets
        else:
            st.session_state[k] = v


def profile_ready() -> bool:
    return bool(st.session_state.resume_bytes) or bool(
        (st.session_state.candidate_profile_text or "").strip()
    )


def ready_to_generate() -> bool:
    return bool(st.session_state.job_url) and profile_ready()


def current_chat_thread_id() -> str:
    """Use the real backend job_id once one exists; otherwise a local
    placeholder so pre-generation chat still has a consistent thread to
    ask the backend's /chat endpoint about (it just has no context yet)."""
    return st.session_state.job_id or st.session_state.session_id


# ---------------------------------------------------------------------------
# API client helpers -- ALL LLM/pipeline work happens behind these calls
# ---------------------------------------------------------------------------
def api_base() -> str:
    return st.session_state.api_base_url.rstrip("/")


def api_create_job() -> str:
    data = {
        "job_url": st.session_state.job_url,
        "ats_threshold": str(st.session_state.ats_threshold),
        "max_attempts": str(st.session_state.max_attempts),
    }
    files = None
    if st.session_state.resume_bytes:
        files = {
            "resume_file": (
                st.session_state.resume_filename or "resume.pdf",
                st.session_state.resume_bytes,
            )
        }
    else:
        data["candidate_profile"] = st.session_state.candidate_profile_text

    resp = requests.post(f"{api_base()}/jobs", data=data, files=files, timeout=30)
    resp.raise_for_status()
    return resp.json()["job_id"]


def api_get_job(job_id: str) -> dict:
    resp = requests.get(f"{api_base()}/jobs/{job_id}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_edit_job(job_id: str, edit_request: str) -> dict:
    resp = requests.post(
        f"{api_base()}/jobs/{job_id}/edit", json={"edit_request": edit_request}, timeout=90
    )
    resp.raise_for_status()
    return resp.json()


def api_chat(job_id: str, message: str, history: list) -> str:
    payload = {
        "message": message,
        "history": [{"role": m["role"], "content": m["content"]} for m in history[-8:]],
    }
    resp = requests.post(f"{api_base()}/jobs/{job_id}/chat", json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["reply"]


def api_health() -> bool:
    try:
        resp = requests.get(f"{api_base()}/health", timeout=3)
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False


# ---------------------------------------------------------------------------
# Progress inference -- the API returns a state snapshot, not a live node-
# by-node stream, so we infer a human-readable progress label from which
# fields are populated on each poll.
# ---------------------------------------------------------------------------
def infer_progress_label(state: dict) -> str:
    status = state.get("status")
    if status == "queued":
        return "Queued..."
    if status == "failed":
        return f"Failed: {state.get('error')}"
    if status == "completed":
        return "Done!"
    if state.get("resume_output"):
        return "Finalizing Skills section..."
    if state.get("attempt_count"):
        score = state.get("ats_score")
        if score is not None:
            return f"Scored attempt {state['attempt_count']} ({score}/100) -- refining..."
        return f"Writing tailored resume (attempt {state['attempt_count']})..."
    if state.get("candidate_name"):
        return "Resume parsed -- checking GitHub, then writing resume..."
    if state.get("company_name"):
        return "Job details extracted -- reading your resume..."
    return "Fetching job posting..."


def run_pipeline_via_api(status_placeholder) -> dict:
    try:
        job_id = api_create_job()
    except requests.exceptions.RequestException as e:
        return {"error": f"Could not reach backend at {api_base()}: {e}"}

    st.session_state.job_id = job_id
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    last_state = {"status": "queued"}

    while time.time() < deadline:
        try:
            last_state = api_get_job(job_id)
        except requests.exceptions.RequestException as e:
            return {"error": f"Lost connection to backend while polling: {e}"}

        status_placeholder.markdown(f"- {infer_progress_label(last_state)}")

        if last_state.get("status") in ("completed", "failed"):
            return last_state
        time.sleep(POLL_INTERVAL_SECONDS)

    return {"error": "Timed out waiting for the backend to finish (5 min)."}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def format_ats_summary(state: dict) -> str:
    lines = [
        f"**ATS Score: {state.get('ats_score')}/100**  "
        f"(attempts used: {state.get('attempt_count')}, "
        f"keyword coverage: {state.get('ats_keyword_coverage')}%)"
    ]
    if state.get("final_note"):
        lines.append(f"_{state['final_note']}_")
    matched = state.get("matched_keywords") or []
    missing = state.get("missing_keywords") or []
    if matched:
        lines.append("**\u2705 Matched:** " + ", ".join(matched))
    if missing:
        lines.append("**\u274C Missing:** " + ", ".join(missing))
    return "\n\n".join(lines)


def format_github_summary(state: dict) -> str:
    if not state.get("github_url"):
        return ""
    projects = state.get("github_projects") or []
    if not projects:
        return f"**GitHub:** {state['github_url']}  \n_No matching public repos found._"
    lines = [f"**GitHub:** {state['github_url']}", "**Job-relevant repos used as evidence:**"]
    for p in projects:
        desc = f" -- {p['description']}" if p.get("description") else ""
        lang = f" ({p['language']})" if p.get("language") else ""
        lines.append(f"- [{p['name']}]({p['url']}){lang}{desc}")
    return "\n".join(lines)


def format_full_result(state: dict) -> str:
    if state.get("error"):
        return f"\u274C Something went wrong: {state['error']}"
    parts = [format_ats_summary(state)]
    gh = format_github_summary(state)
    if gh:
        parts.append(gh)
    parts.append(state.get("resume_output", ""))
    return "\n\n---\n\n".join(parts)


def interpret_command(text: str, has_result: bool) -> str:
    t = text.lower()
    if any(k in t for k in ["regenerate", "start over", "redo the resume", "run again", "generate again"]):
        return "regenerate"
    if "threshold" in t and any(ch.isdigit() for ch in t):
        return "set_threshold"
    if has_result and any(
        k in t for k in ["ats score", "show score", "my score", "missing keyword", "matched keyword", "show analysis", "show details"]
    ):
        return "show_ats"
    if has_result and any(k in t for k in ["github", "repo", "repos", "projects used", "which projects"]):
        return "show_github"
    if has_result and any(
        k in t
        for k in [
            "shorten", "make it shorter", "make it longer", "add ", "remove ",
            "rewrite", "change the", "emphasize", "focus more on", "reword",
            "edit the resume", "update the resume", "tweak", "revise",
        ]
    ):
        return "edit_resume"
    return "chat"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("Settings")

st.session_state.api_base_url = st.sidebar.text_input("Backend API URL", value=st.session_state.api_base_url)
backend_ok = api_health()
st.sidebar.markdown("Backend: " + ("\U0001F7E2 connected" if backend_ok else "\U0001F534 unreachable"))
if not backend_ok:
    st.sidebar.caption(f"Couldn't reach {api_base()}/health -- check the backend is running and the URL is correct.")

st.session_state.ats_threshold = st.sidebar.slider("ATS score threshold", 50, 100, st.session_state.ats_threshold, 5)
st.session_state.max_attempts = st.sidebar.slider("Max generate/score attempts", 1, 5, st.session_state.max_attempts)

st.sidebar.divider()
st.sidebar.markdown("**Captured so far:**")
st.sidebar.write(f"Job URL: {st.session_state.job_url or '_not yet_'}")
st.sidebar.write(f"Resume: {st.session_state.resume_filename or ('typed background' if st.session_state.candidate_profile_text else '_not yet_')}")

if st.sidebar.button("Reset conversation"):
    reset_conversation()
    st.rerun()

sidebar_file = st.sidebar.file_uploader(
    "Attach resume here if your Streamlit version doesn't support in-chat uploads",
    type=["pdf", "docx", "txt", "md"],
)
if sidebar_file is not None:
    sig = f"{sidebar_file.name}-{sidebar_file.size}"
    if sig != st.session_state.sidebar_file_signature:
        st.session_state.resume_bytes = sidebar_file.getvalue()
        st.session_state.resume_filename = sidebar_file.name
        st.session_state.candidate_profile_text = None
        st.session_state.sidebar_file_signature = sig
        st.session_state.messages.append(
            {"role": "assistant", "content": f"Got it -- received **{sidebar_file.name}** from the sidebar."}
        )


# ---------------------------------------------------------------------------
# Main chat UI
# ---------------------------------------------------------------------------
st.title("\U0001F4AC AI Resume Tailor")
st.caption("Chat naturally: paste a job URL, attach your resume, describe your background, or ask about your results.")

if not st.session_state.messages:
    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": (
                "Hi! Paste a job posting URL and attach (or describe) your resume, "
                "and I'll generate a tailored, ATS-scored resume for you. "
                "You can send them in the same message or separately."
            ),
        }
    )

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

try:
    prompt = st.chat_input(
        "Paste a job URL, attach your resume, or ask a question...",
        accept_file=True,
        file_type=["pdf", "docx", "txt", "md"],
    )
    user_text = (getattr(prompt, "text", "") or "") if prompt is not None else None
    user_files = (getattr(prompt, "files", []) or []) if prompt is not None else []
except TypeError:
    prompt = st.chat_input("Paste a job URL, or ask a question... (use the sidebar to attach your resume)")
    user_text = prompt
    user_files = []

if prompt is not None:
    display_text = user_text or ("(attached a file)" if user_files else "")
    st.session_state.messages.append({"role": "user", "content": display_text})
    with st.chat_message("user"):
        st.markdown(display_text)

    found_url = URL_RE.search(user_text or "")
    if found_url:
        st.session_state.job_url = found_url.group(0)

    if user_files:
        f = user_files[0]
        st.session_state.resume_bytes = f.getvalue()
        st.session_state.resume_filename = f.name
        st.session_state.candidate_profile_text = None

    command = interpret_command(user_text or "", has_result=bool(st.session_state.final_state))

    with st.chat_message("assistant"):
        reply = None

        if command == "regenerate":
            if not ready_to_generate():
                reply = "I still need both a job URL and your resume (or a background description) before I can generate anything."
            else:
                status = st.empty()
                final_state = run_pipeline_via_api(status)
                st.session_state.final_state = final_state
                reply = format_full_result(final_state)

        elif command == "set_threshold":
            digits = re.search(r"\d+", user_text)
            if digits:
                st.session_state.ats_threshold = max(50, min(100, int(digits.group(0))))
                reply = f"Threshold set to {st.session_state.ats_threshold}. Say 'regenerate' to apply it."
            else:
                reply = "What threshold would you like (50-100)?"

        elif command == "show_ats":
            reply = format_ats_summary(st.session_state.final_state)

        elif command == "show_github":
            gh = format_github_summary(st.session_state.final_state)
            reply = gh or "No GitHub URL was found in your resume, so I couldn't check for relevant projects."

        elif command == "edit_resume":
            if not st.session_state.job_id:
                reply = "I don't have a generated resume to edit yet -- generate one first."
            else:
                try:
                    updated = api_edit_job(st.session_state.job_id, user_text)
                    st.session_state.final_state = updated
                    reply = "Here's the updated resume:\n\n" + format_full_result(updated)
                except requests.exceptions.RequestException as e:
                    reply = f"\u274C Couldn't reach the backend to apply that edit: {e}"

        else:
            if not st.session_state.final_state:
                if found_url and not profile_ready():
                    reply = "Got the job URL! Now attach your old resume, or just describe your background."
                elif user_files and not st.session_state.job_url:
                    reply = f"Got **{st.session_state.resume_filename}**! Now paste the job posting URL."
                elif ready_to_generate():
                    status = st.empty()
                    final_state = run_pipeline_via_api(status)
                    st.session_state.final_state = final_state
                    reply = format_full_result(final_state)
                elif st.session_state.job_url and not profile_ready() and user_text and not found_url:
                    st.session_state.candidate_profile_text = user_text
                    if ready_to_generate():
                        status = st.empty()
                        final_state = run_pipeline_via_api(status)
                        st.session_state.final_state = final_state
                        reply = format_full_result(final_state)
                else:
                    try:
                        reply = api_chat(current_chat_thread_id(), user_text or "", st.session_state.messages)
                    except requests.exceptions.RequestException as e:
                        reply = f"\u274C Couldn't reach the backend: {e}"
            else:
                try:
                    reply = api_chat(current_chat_thread_id(), user_text or "", st.session_state.messages)
                except requests.exceptions.RequestException as e:
                    reply = f"\u274C Couldn't reach the backend: {e}"

        st.markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})

    if st.session_state.final_state and not st.session_state.final_state.get("error"):
        st.download_button(
            "Download latest resume (.md)",
            data=st.session_state.final_state.get("resume_output", "") or "",
            file_name=f"{st.session_state.final_state.get('company_name', 'tailored')}_resume.md",
            mime="text/markdown",
        )

# ---------------------------------------------------------------------------
# Details panel -- structured breakdown alongside the chat
# ---------------------------------------------------------------------------
fs = st.session_state.final_state
if fs and not fs.get("error"):
    with st.expander("\U0001F4CB Details (job info, candidate profile, ATS metrics)"):
        tab_job, tab_candidate, tab_ats = st.tabs(["\U0001F3E2 Job Info", "\U0001F9D1 Candidate Profile", "\U0001F4CA ATS Scoring"])

        with tab_job:
            st.markdown(f"**Company:** {fs.get('company_name', '-')}")
            st.markdown(f"**Role:** {fs.get('job_role', '-')}")
            st.markdown(f"**Description:** {fs.get('job_description', '-')}")
            st.markdown("**Key Responsibilities:**")
            for r in fs.get("key_responsibilities") or []:
                st.markdown(f"- {r}")
            st.markdown("**Requirements:**")
            for r in fs.get("requirements") or []:
                st.markdown(f"- {r}")

        with tab_candidate:
            if fs.get("candidate_name"):
                st.markdown(f"**Name:** {fs.get('candidate_name')}")
                if fs.get("github_url"):
                    st.markdown(f"**GitHub:** {fs['github_url']}")
                    for p in fs.get("github_projects") or []:
                        desc = f" -- {p['description']}" if p.get("description") else ""
                        st.markdown(f"- [{p['name']}]({p['url']}) ({p.get('language', '-')}){desc}")
            else:
                st.info("No resume was uploaded for this run -- candidate profile was typed manually.")

        with tab_ats:
            c1, c2, c3 = st.columns(3)
            c1.metric("Final ATS Score", fs.get("ats_score", "-"))
            c2.metric("Keyword Coverage %", fs.get("ats_keyword_coverage", "-"))
            c3.metric("Attempts Used", fs.get("attempt_count", "-"))
            if fs.get("final_note"):
                st.warning(fs["final_note"])
            colm, colx = st.columns(2)
            with colm:
                st.markdown("**\u2705 Matched keywords**")
                for k in fs.get("matched_keywords") or []:
                    st.markdown(f"- {k}")
            with colx:
                st.markdown("**\u274C Missing keywords**")
                for k in fs.get("missing_keywords") or []:
                    st.markdown(f"- {k}")
