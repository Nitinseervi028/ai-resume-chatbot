"""
FastAPI backend for the Job URL -> Resume Generator pipeline.

Exposes the LangGraph pipeline (job_resume_langgraph.py) over HTTP so any
frontend -- web, mobile, another service -- can drive it, instead of being
tied to the Streamlit chat app.

Design notes:
  - Jobs run in a background thread pool (pipeline calls are synchronous/
    blocking -- LLM calls, scraping, etc.) so the API responds immediately
    with a job_id instead of holding the HTTP connection open for the
    whole run.
  - Status/results are read directly from the SqliteSaver checkpoint store
    by thread_id (== job_id) -- no separate result-tracking database is
    needed, and results survive a server restart.
  - Each request/job opens its OWN sqlite3 connection (rather than sharing
    one connection across separate requests/jobs), which keeps concurrent
    requests from stepping on each other. Each connection is opened with
    check_same_thread=False, because LangGraph's Pregel executor uses its
    own internal worker threads to run graph nodes even within a single
    synchronous .invoke() call -- so the connection needs to tolerate
    being touched by more than one OS thread within that one operation.
    For heavier production concurrency, swap SqliteSaver for
    langgraph-checkpoint-postgres instead -- same checkpointer interface,
    built for real concurrent multi-connection access.

Run:
    uvicorn fastapi_backend:app --reload --port 8000

Then open http://localhost:8000/docs for interactive API docs (built into
FastAPI automatically).

Install:
    pip install fastapi uvicorn python-multipart --break-system-packages

Requires job_resume_langgraph.py in the SAME directory.
"""

import os
import sqlite3
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List

from dotenv import load_dotenv
load_dotenv()  # picks up a local .env file automatically (no-op if absent --
                # e.g. in Docker, where env vars come from env_file/environment instead)

from fastapi import FastAPI, Form, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from job_resume_langgraph import (
    ResumeState,
    build_graph,
    get_llm,
    score_resume,
    polish_skills,
)

DB_PATH = os.getenv("CHECKPOINT_DB_PATH", "resume_pipeline_checkpoints.db")

app = FastAPI(
    title="AI Resume Tailor API",
    description="Job URL -> tailored, ATS-scored resume, backed by a LangGraph pipeline.",
    version="1.0",
)

# CORS wide open for local development. Restrict allow_origins to your
# actual frontend's domain(s) before deploying this anywhere public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=4)

# Lightweight in-memory status flags, keyed by job_id. This is just for a
# quick "queued/running" signal before the first checkpoint exists -- the
# actual result data always comes from the checkpoint store, so a server
# restart mid-job only loses the "running" flag, never the underlying
# progress (GET /jobs falls back to inferring status from the checkpoint
# itself if a job_id isn't found here).
JOB_FLAGS: dict = {}
JOB_FLAGS_LOCK = threading.Lock()


def _graph_with_fresh_connection():
    """
    Builds a compiled graph backed by a NEW sqlite3 connection, scoped to
    one logical operation (one job run, or one request).

    check_same_thread=False is required here: LangGraph's Pregel executor
    uses its own internal worker threads to run graph nodes even within a
    single synchronous .invoke() call, so the checkpointer's connection
    gets touched by a different OS thread than the one that opened it.
    SQLite's default (check_same_thread=True) forbids that and raises
    "SQLite objects created in a thread can only be used in that same
    thread." We still avoid sharing one connection ACROSS separate
    requests/jobs (each call here gets its own), which is what actually
    keeps concurrent access safe -- this flag just permits LangGraph's
    internal threading within a single operation's connection.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return build_graph(checkpointer=checkpointer), conn


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class JobCreateResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str  # "queued" | "running" | "completed" | "failed"
    error: Optional[str] = None
    company_name: Optional[str] = None
    job_role: Optional[str] = None
    job_description: Optional[str] = None
    key_responsibilities: Optional[List[str]] = None
    requirements: Optional[List[str]] = None
    candidate_name: Optional[str] = None
    github_url: Optional[str] = None
    github_projects: Optional[List[dict]] = None
    ats_score: Optional[int] = None
    ats_keyword_coverage: Optional[int] = None
    attempt_count: Optional[int] = None
    matched_keywords: Optional[List[str]] = None
    missing_keywords: Optional[List[str]] = None
    final_note: Optional[str] = None
    resume_output: Optional[str] = None  # only populated once status == "completed"


class EditRequest(BaseModel):
    edit_request: str


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []


class ChatResponse(BaseModel):
    reply: str


# ---------------------------------------------------------------------------
# Background job execution
# ---------------------------------------------------------------------------
def _run_job(job_id: str, initial_state: ResumeState, tmp_file_to_cleanup: Optional[str]):
    with JOB_FLAGS_LOCK:
        JOB_FLAGS[job_id] = {"status": "running"}

    graph_app, conn = _graph_with_fresh_connection()
    try:
        config = {"configurable": {"thread_id": job_id}}
        final_state = graph_app.invoke(initial_state, config=config)
        with JOB_FLAGS_LOCK:
            if final_state.get("error"):
                JOB_FLAGS[job_id] = {"status": "failed", "error": final_state["error"]}
            else:
                JOB_FLAGS[job_id] = {"status": "completed"}
    except Exception as e:
        with JOB_FLAGS_LOCK:
            JOB_FLAGS[job_id] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()
        if tmp_file_to_cleanup and os.path.exists(tmp_file_to_cleanup):
            os.remove(tmp_file_to_cleanup)


def _read_job_state(job_id: str) -> JobStatusResponse:
    graph_app, conn = _graph_with_fresh_connection()
    try:
        config = {"configurable": {"thread_id": job_id}}
        snapshot = graph_app.get_state(config)
    finally:
        conn.close()

    values = dict(snapshot.values) if snapshot and snapshot.values else {}

    with JOB_FLAGS_LOCK:
        flag = JOB_FLAGS.get(job_id)

    if flag is None and not values:
        raise HTTPException(status_code=404, detail="Job not found")

    if flag is not None:
        status = flag["status"]
        error = flag.get("error")
    else:
        # No in-memory flag (e.g. server restarted) -- infer from the
        # checkpoint itself: if there's no more pending work (snapshot.next
        # is empty) and we have output or an error, it's done.
        pending = bool(snapshot.next) if snapshot else True
        if values.get("error"):
            status, error = "failed", values["error"]
        elif values.get("resume_output") and not pending:
            status, error = "completed", None
        else:
            status, error = "running", None

    return JobStatusResponse(
        job_id=job_id,
        status=status,
        error=error,
        company_name=values.get("company_name"),
        job_role=values.get("job_role"),
        job_description=values.get("job_description"),
        key_responsibilities=values.get("key_responsibilities"),
        requirements=values.get("requirements"),
        candidate_name=values.get("candidate_name"),
        github_url=values.get("github_url"),
        github_projects=values.get("github_projects"),
        ats_score=values.get("ats_score"),
        ats_keyword_coverage=values.get("ats_keyword_coverage"),
        attempt_count=values.get("attempt_count"),
        matched_keywords=values.get("matched_keywords"),
        missing_keywords=values.get("missing_keywords"),
        final_note=values.get("final_note"),
        resume_output=values.get("resume_output") if status == "completed" else None,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {
        "service": "AI Resume Tailor API",
        "docs": "/docs",
        "health": "/health",
    }


@app.post("/jobs", response_model=JobCreateResponse)
async def create_job(
    job_url: str = Form(...),
    candidate_profile: Optional[str] = Form(None),
    ats_threshold: int = Form(80),
    max_attempts: int = Form(3),
    resume_file: Optional[UploadFile] = File(None),
):
    if not resume_file and not (candidate_profile or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Provide either resume_file (upload) or candidate_profile (text background).",
        )

    initial_state: ResumeState = {
        "job_url": job_url,
        "ats_threshold": ats_threshold,
        "max_attempts": max_attempts,
    }

    tmp_path = None
    if resume_file is not None:
        suffix = os.path.splitext(resume_file.filename or "")[1] or ".txt"
        content = await resume_file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        initial_state["resume_file_path"] = tmp_path
    else:
        initial_state["candidate_profile"] = candidate_profile

    job_id = str(uuid.uuid4())
    with JOB_FLAGS_LOCK:
        JOB_FLAGS[job_id] = {"status": "queued"}

    executor.submit(_run_job, job_id, initial_state, tmp_path)
    return JobCreateResponse(job_id=job_id, status="queued")


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str):
    return _read_job_state(job_id)


@app.post("/jobs/{job_id}/edit", response_model=JobStatusResponse)
def edit_job(job_id: str, body: EditRequest):
    graph_app, conn = _graph_with_fresh_connection()
    try:
        config = {"configurable": {"thread_id": job_id}}
        snapshot = graph_app.get_state(config)
        if not snapshot or not snapshot.values.get("resume_output"):
            raise HTTPException(status_code=404, detail="No completed resume found for this job_id yet.")

        state = dict(snapshot.values)
        system_prompt = (
            "You are editing an existing tailored resume based on the user's "
            "request. Make ONLY the requested change. Do not fabricate any new "
            "experience, skills, or qualifications -- only reword, reorganize, "
            "shorten, lengthen, or re-emphasize what's already truthfully there. "
            "Return the COMPLETE updated resume in clean Markdown, all sections, "
            "no commentary before or after it."
        )
        human = (
            f"JOB CONTEXT:\nCompany: {state.get('company_name')}\nRole: {state.get('job_role')}\n"
            f"Requirements: {', '.join(state.get('requirements', []))}\n\n"
            f"CURRENT RESUME:\n{state.get('resume_output', '')}\n\n"
            f"USER'S EDIT REQUEST:\n{body.edit_request}\n\n"
            "Return the full revised resume now."
        )
        try:
            response = get_llm().invoke([SystemMessage(content=system_prompt), HumanMessage(content=human)])
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Edit failed: {e}")

        state["resume_output"] = response.content.strip()
        try:
            state = score_resume(state)
            state = polish_skills(state)
        except Exception:
            pass  # rescoring is best-effort; keep the edited resume regardless

        # Persist the edit as a new checkpoint on this thread.
        graph_app.update_state(config, state)
    finally:
        conn.close()

    return _read_job_state(job_id)


@app.post("/jobs/{job_id}/chat", response_model=ChatResponse)
def chat_about_job(job_id: str, body: ChatRequest):
    """
    Freeform Q&A about a job/resume, using whatever state exists for this
    job_id as context (works even before generation completes, or before
    any job exists at all for this id -- it just answers with no context
    in that case, rather than erroring).
    """
    graph_app, conn = _graph_with_fresh_connection()
    try:
        config = {"configurable": {"thread_id": job_id}}
        snapshot = graph_app.get_state(config)
    finally:
        conn.close()

    state = dict(snapshot.values) if snapshot and snapshot.values else {}

    context = ""
    if state.get("resume_output"):
        context = (
            f"\n\nCONTEXT -- the user's tailored resume for {state.get('job_role')} at "
            f"{state.get('company_name')} currently scores {state.get('ats_score')}/100. "
            f"Missing keywords: {', '.join(state.get('missing_keywords', []))}. "
            f"Current resume:\n{state.get('resume_output', '')}"
        )

    system_prompt = (
        "You are a helpful resume/career assistant embedded in a chat UI. "
        "Answer the user's question concisely and honestly. If they ask "
        "about their ATS score or missing keywords, explain based on the "
        "context given -- don't invent numbers not present in it. If they "
        "seem to want an actual resume edit, tell them to phrase it as a "
        "direct request (e.g. 'shorten the summary')." + context
    )

    messages = [SystemMessage(content=system_prompt)]
    for m in body.history[-8:]:
        cls = HumanMessage if m.role == "user" else SystemMessage
        messages.append(cls(content=m.content))
    messages.append(HumanMessage(content=body.message))

    try:
        response = get_llm().invoke(messages)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chat failed: {e}")

    return ChatResponse(reply=response.content.strip())


@app.get("/jobs/{job_id}/download")
def download_resume(job_id: str):
    status_resp = _read_job_state(job_id)
    if not status_resp.resume_output:
        raise HTTPException(status_code=404, detail="Resume not ready yet (or job hasn't completed).")

    filename = f"{(status_resp.company_name or 'tailored').replace(' ', '_')}_resume.md"
    return Response(
        content=status_resp.resume_output,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )