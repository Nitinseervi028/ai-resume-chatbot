r"""
MCP server for the AI Resume Tailor pipeline.

Exposes fastapi_backend.py as MCP tools -- generate_tailored_resume,
check_job_status, edit_resume, chat_about_resume -- so any MCP-compatible
client (Claude Desktop, Claude Code, etc.) can drive the whole pipeline
directly from a conversation.

Like streamlit_chat_app.py, this is a PURE API CLIENT: it never imports
job_resume_langgraph.py or calls any LLM directly, and needs no API keys
of its own. The FastAPI backend (fastapi_backend.py) must already be
running and reachable at API_BASE_URL -- start it first:

    uvicorn fastapi_backend:app --port 8000
    # or: docker compose up backend

Run standalone (for testing with `mcp dev`, or any stdio MCP client):
    python mcp_server.py

Configure in Claude Desktop -- edit claude_desktop_config.json
  (Windows: %APPDATA%\Claude\claude_desktop_config.json
   macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json)
and add:

    {
      "mcpServers": {
        "resume-tailor": {
          "command": "C:\\path\\to\\venv\\Scripts\\python.exe",
          "args": ["C:\\path\\to\\ai-resume-tailor\\mcp_server.py"],
          "env": { "API_BASE_URL": "http://localhost:8000" }
        }
      }
    }

Then restart Claude Desktop -- it will show a hammer/tools icon with these
four tools available.

Install:
    pip install mcp requests --break-system-packages
"""

import os
import time
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

API_BASE_DEFAULT = os.getenv("API_BASE_URL", "http://localhost:8000")
POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 300  # 5 min safety cap, matches streamlit_chat_app.py

mcp = FastMCP("resume-tailor")


def _api_base() -> str:
    return API_BASE_DEFAULT.rstrip("/")


def _format_result(job_id: str, state: dict) -> str:
    lines = [
        f"Job ID: {job_id}",
        f"Company: {state.get('company_name')}",
        f"Role: {state.get('job_role')}",
        f"ATS Score: {state.get('ats_score')}/100 "
        f"(attempts: {state.get('attempt_count')}, "
        f"keyword coverage: {state.get('ats_keyword_coverage')}%)",
    ]
    if state.get("final_note"):
        lines.append(f"Note: {state['final_note']}")
    if state.get("matched_keywords"):
        lines.append(f"Matched keywords: {', '.join(state['matched_keywords'])}")
    if state.get("missing_keywords"):
        lines.append(f"Missing keywords: {', '.join(state['missing_keywords'])}")
    if state.get("github_url"):
        lines.append(f"GitHub: {state['github_url']}")
        for p in state.get("github_projects") or []:
            lines.append(f"  - {p.get('name')}: {p.get('url')}")
    lines.append("\n--- Resume (Markdown) ---\n")
    lines.append(state.get("resume_output", ""))
    return "\n".join(lines)


@mcp.tool()
def generate_tailored_resume(
    job_url: str,
    resume_file_path: Optional[str] = None,
    candidate_profile: Optional[str] = None,
    ats_threshold: int = 80,
    max_attempts: int = 3,
) -> str:
    """
    Generate a tailored, ATS-scored resume for a specific job posting.

    Provide EITHER resume_file_path OR candidate_profile, not both:
      - resume_file_path: an absolute local filesystem path to the
        candidate's existing resume (.pdf, .docx, .txt, or .md). Since
        this MCP server runs locally, it can read files directly off disk.
      - candidate_profile: free-text background, if no resume file is
        available (name, experience, skills, education, etc.).

    This call BLOCKS until the resume is ready (typically 30-90 seconds,
    longer if the ATS scoring loop needs multiple attempts) and returns
    the full result: the resume text, ATS score, matched/missing
    keywords, and any GitHub projects used as evidence.

    Args:
        job_url: URL of the job posting to tailor the resume for.
        resume_file_path: Local path to the candidate's old resume file.
        candidate_profile: Free-text candidate background (alternative to resume_file_path).
        ats_threshold: Minimum ATS score (50-100) to accept the resume. Default 80.
        max_attempts: Max generate/score retry cycles before giving up. Default 3.
    """
    if not resume_file_path and not (candidate_profile or "").strip():
        return "Error: provide either resume_file_path or candidate_profile."

    data = {
        "job_url": job_url,
        "ats_threshold": str(ats_threshold),
        "max_attempts": str(max_attempts),
    }
    files = None
    file_handle = None
    if resume_file_path:
        if not os.path.exists(resume_file_path):
            return f"Error: resume_file_path does not exist on this machine: {resume_file_path}"
        file_handle = open(resume_file_path, "rb")
        files = {"resume_file": (os.path.basename(resume_file_path), file_handle)}
    else:
        data["candidate_profile"] = candidate_profile

    try:
        resp = requests.post(f"{_api_base()}/jobs", data=data, files=files, timeout=30)
        resp.raise_for_status()
        job_id = resp.json()["job_id"]
    except requests.exceptions.RequestException as e:
        return f"Error: could not reach backend at {_api_base()}: {e}"
    finally:
        if file_handle:
            file_handle.close()

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            state = requests.get(f"{_api_base()}/jobs/{job_id}", timeout=30).json()
        except requests.exceptions.RequestException as e:
            return f"Error: lost connection to backend while polling: {e}"

        if state.get("status") == "completed":
            return _format_result(job_id, state)
        if state.get("status") == "failed":
            return f"Generation failed: {state.get('error')}"
        time.sleep(POLL_INTERVAL_SECONDS)

    return (
        f"Timed out after {POLL_TIMEOUT_SECONDS}s waiting for job {job_id}. "
        "It may still be running -- use check_job_status to check on it later."
    )


@mcp.tool()
def check_job_status(job_id: str) -> str:
    """
    Check the status of a previously started resume generation job.
    Useful if generate_tailored_resume timed out, or to re-check a past
    job_id without regenerating anything.
    """
    try:
        state = requests.get(f"{_api_base()}/jobs/{job_id}", timeout=30).json()
    except requests.exceptions.RequestException as e:
        return f"Error: could not reach backend at {_api_base()}: {e}"

    if state.get("status") == "completed":
        return _format_result(job_id, state)
    if state.get("status") == "failed":
        return f"Failed: {state.get('error')}"
    return f"Status: {state.get('status')}"


@mcp.tool()
def edit_resume(job_id: str, edit_request: str) -> str:
    """
    Request an edit to a previously generated resume -- e.g. "make the
    summary shorter", "emphasize AWS experience more", "shorten the
    skills section". The edit is applied truthfully (no fabricated
    content) and the resume is automatically re-scored afterward.

    Args:
        job_id: The job_id returned by generate_tailored_resume.
        edit_request: What to change, in plain language.
    """
    try:
        resp = requests.post(
            f"{_api_base()}/jobs/{job_id}/edit",
            json={"edit_request": edit_request},
            timeout=90,
        )
        resp.raise_for_status()
        state = resp.json()
    except requests.exceptions.RequestException as e:
        return f"Error: could not reach backend at {_api_base()}: {e}"

    return _format_result(job_id, state)


@mcp.tool()
def chat_about_resume(job_id: str, message: str) -> str:
    """
    Ask a question about a generated resume or job without editing it --
    e.g. "why is my ATS score low?" or "what keywords am I missing?".

    Args:
        job_id: The job_id returned by generate_tailored_resume.
        message: The question to ask.
    """
    try:
        resp = requests.post(
            f"{_api_base()}/jobs/{job_id}/chat",
            json={"message": message, "history": []},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["reply"]
    except requests.exceptions.RequestException as e:
        return f"Error: could not reach backend at {_api_base()}: {e}"


if __name__ == "__main__":
    mcp.run()