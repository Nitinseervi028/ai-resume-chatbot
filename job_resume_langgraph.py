"""
Job URL -> Resume Generator (LangGraph pipeline)
=================================================

Flow:
  1. fetch_job_posting        -> scrapes the raw text from the job URL
  2. extract_job_info         -> LLM call that pulls out structured fields:
                                  company_name, job_role, job_description,
                                  key_responsibilities, requirements
  2b. extract_candidate_profile -> (optional) if resume_file_path is set,
                                  parses an uploaded old resume (.pdf/.docx/
                                  .txt/.md) into structured candidate data
                                  instead of requiring manually-typed text
  3. generate_resume          -> LLM call that takes the structured job info +
                                  the candidate's background and writes a
                                  tailored resume
  4. score_resume             -> ATS-style scoring; loops back to
                                  generate_resume with feedback if below
                                  threshold, up to max_attempts
  5. polish_skills            -> ensures every confirmed matched keyword is
                                  explicitly listed in the Skills section

Checkpointing (persistent memory across runs/kernel restarts):
  The graph is compiled with a SqliteSaver checkpointer, keyed by a
  "thread_id" you choose. Every node's state gets saved to a local .db
  file as the graph runs. This gives you:
    - Full state history per run: app.get_state_history(config) lets you
      inspect exactly what each node returned, at each step -- great for
      debugging (this alone would've made several of our earlier bugs
      trivial to spot).
    - Resumability: if a run crashes or you restart the kernel, the last
      checkpoint for a given thread_id is still on disk.
    - Reuse: invoking the same thread_id again lets LangGraph pick up
      from where that thread left off rather than always starting fresh.
  See the __main__ block below for usage.

Install:
    pip install langgraph langgraph-checkpoint-sqlite langchain-openai requests beautifulsoup4 --break-system-packages
    pip install pypdf python-docx --break-system-packages   # for resume_file_path parsing

Set your API key (using OpenRouter as the OpenAI-compatible provider):
    export OPENROUTER_API_KEY="sk-or-..."

Run:
    python job_resume_langgraph.py
"""

import os
import re
import sqlite3
from typing import TypedDict, Optional, List

from dotenv import load_dotenv
load_dotenv()  # picks up a local .env file automatically if present

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver


# ---------------------------------------------------------------------------
# 1. State definition
# ---------------------------------------------------------------------------
class ResumeState(TypedDict, total=False):
    job_url: str
    raw_job_text: str
    company_name: str
    job_role: str
    job_description: str
    key_responsibilities: list
    requirements: list
    candidate_profile: str          # free-text background, either typed manually OR
                                     # auto-built from an uploaded old resume (see below)
    resume_output: str
    error: Optional[str]

    # --- Candidate resume upload/extraction fields ---
    resume_file_path: str            # path to an uploaded old resume (.pdf/.docx/.txt/.md)
    raw_candidate_resume_text: str   # raw extracted text from that file
    candidate_name: str
    candidate_skills: list
    candidate_experience: list
    candidate_education: list
    candidate_certifications: list
    github_url: str                  # candidate's GitHub profile URL, if found in the resume
    github_projects: list            # top job-relevant public repos fetched from GitHub
    github_note: str                 # explains why github_projects is empty, if it is

    # --- ATS scoring / self-improvement loop fields ---
    ats_score: int                  # 0-100, clamped/final score for this attempt
    ats_raw_llm_score: int          # 0-100, the model's raw self-reported score before clamping
    ats_keyword_coverage: int       # 0-100, deterministic matched/(matched+missing) percentage
    ats_feedback: str               # actionable feedback from the ATS-checker LLM
    matched_keywords: list
    missing_keywords: list
    attempt_count: int              # how many generate->score cycles have run
    ats_threshold: int              # score needed to pass (default 80, set below)
    max_attempts: int               # hard cap on loop iterations (default 3, set below)
    best_score: int                 # best score seen across attempts (for fallback)
    best_resume: str                # resume text tied to best_score
    final_note: str                 # explains outcome if threshold was never crossed


class JobInfo(BaseModel):
    """Structured job posting details."""
    company_name: str = Field(default="", description="Name of the hiring company")
    job_role: str = Field(default="", description="Job title / role name")
    job_description: str = Field(default="", description="2-4 sentence summary of the role")
    key_responsibilities: List[str] = Field(
        default_factory=list,
        description="What the person will do day-to-day. Infer from prose if no explicit bullet list exists.",
    )
    requirements: List[str] = Field(
        default_factory=list,
        description="Skills, tools, experience, and qualifications needed. Infer from prose if no explicit bullet list exists.",
    )


class CandidateProfile(BaseModel):
    """Structured candidate background, extracted from an uploaded old resume."""
    name: str = Field(default="", description="Candidate's full name")
    summary: str = Field(
        default="", description="1-3 sentence professional summary, based only on what's actually in the resume"
    )
    skills: List[str] = Field(
        default_factory=list,
        description=(
            "SHORT, ATOMIC skills/tools/technologies (1-4 words each) actually listed or "
            "clearly evidenced in the resume -- e.g. 'Python', 'FastAPI', 'AWS', 'PostgreSQL'. "
            "Do not invent skills that aren't stated or clearly demonstrated."
        ),
    )
    experience_bullets: List[str] = Field(
        default_factory=list,
        description=(
            "Each work-experience entry as one bullet string, e.g. 'Python Backend "
            "Developer, Acme Corp (2021-2024): Built REST APIs with FastAPI, deployed "
            "on AWS'. Preserve the actual companies, titles, dates, and accomplishments "
            "from the resume -- do not fabricate or embellish."
        ),
    )
    education: List[str] = Field(
        default_factory=list,
        description="Each education entry as one string, e.g. 'B.Tech Computer Science, XYZ University, 2021'",
    )
    certifications_projects: List[str] = Field(
        default_factory=list,
        description="Certifications and/or notable projects actually listed in the resume",
    )
    github_url: str = Field(
        default="",
        description=(
            "The candidate's GitHub profile URL if present anywhere in the resume "
            "(e.g. in a contact/header section) -- e.g. 'https://github.com/username'. "
            "Leave empty if no GitHub link appears in the resume."
        ),
    )


class ATSScore(BaseModel):
    """ATS-style evaluation of a resume against a specific job posting."""
    score: int = Field(
        ..., ge=0, le=100,
        description="Overall ATS match score 0-100, how well this resume matches the job's requirements/responsibilities/keywords",
    )
    matched_keywords: List[str] = Field(
        default_factory=list,
        description=(
            "SHORT, ATOMIC keywords (1-4 words each) that ARE present in the resume -- "
            "e.g. 'Python', 'REST APIs', 'AI/ML fundamentals', 'Team Collaboration', "
            "'PostgreSQL'. Each item must be a single skill/tool/technology/domain term "
            "someone could put on a resume bullet. NEVER copy a full requirement "
            "sentence verbatim (e.g. do NOT return 'Currently pursuing a degree in "
            "Computer Science' or 'Basic understanding of AI and machine learning "
            "concepts' -- extract just 'Computer Science (in progress)' or 'AI/ML "
            "fundamentals' instead). Do not include education-status or years-of-"
            "experience statements here; those aren't Skills-section items."
        ),
    )
    missing_keywords: List[str] = Field(
        default_factory=list,
        description=(
            "SHORT, ATOMIC keywords/skills (1-4 words each) that are MISSING or weakly "
            "represented in the resume. Same rules as matched_keywords: single "
            "resume-worthy terms only, never full sentences copied from the job posting."
        ),
    )
    feedback: str = Field(
        default="",
        description="2-5 concise, actionable bullet-style sentences on exactly what to change to raise the score",
    )


# ---------------------------------------------------------------------------
# 2. LLM clients -- built LAZILY, not at import time.
#
#    IMPORTANT: these used to be plain module-level `llm = ChatOpenAI(...)`
#    instances, constructed once when this module is first imported, with
#    api_key=os.getenv("OPENROUTER_API_KEY") baked in at that exact moment.
#    That breaks any caller (like a Streamlit app) that sets the API key
#    AFTER import -- e.g. from a sidebar text input -- because Python only
#    executes a module's top-level code on its FIRST import; the key never
#    gets picked up, and langchain_openai fails with a confusing internal
#    error ("Sync client is not available...") instead of a clear "no API
#    key" message.
#
#    Fix: build a fresh client inside each get_*_llm() call, reading the
#    env var at that moment. Construction is cheap (no network call), so
#    there's no real performance cost to not caching these.
#
#    NOTE on max_tokens: 100 is nowhere near enough for either extraction
#    (a JSON-ish tool call with several list fields) or resume generation
#    (a full multi-section Markdown resume). Both were bumped up so output
#    doesn't get silently truncated mid-response.
# ---------------------------------------------------------------------------
def _get_api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise ValueError(
            "OPENROUTER_API_KEY is not set. Set it as an environment variable "
            "before running the pipeline (or, in the Streamlit app, enter it "
            "in the sidebar BEFORE clicking Generate)."
        )
    return key


def get_llm() -> ChatOpenAI:
    """Plain chat model used for resume generation and Skills polishing."""
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.3,
        base_url="https://openrouter.ai/api/v1",
        api_key=_get_api_key(),
        max_tokens=2000,   # resume generation needs real room to write
    )


def get_extraction_llm():
    """
    LLM FORCED to call the JobInfo tool (tool_choice pins the exact tool
    name), so extraction can never fall back to a free-text answer like
    "**Company Name:** ..." instead of structured data.
    """
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        base_url="https://openrouter.ai/api/v1",
        api_key=_get_api_key(),
        max_tokens=1000,
    ).bind_tools([JobInfo], tool_choice="JobInfo")


def get_scoring_llm():
    """LLM FORCED to call the ATSScore tool, same forced-tool-call pattern."""
    return ChatOpenAI(
        model="gpt-4o",
        temperature=0,
        base_url="https://openrouter.ai/api/v1",
        api_key=_get_api_key(),
        max_tokens=800,
    ).bind_tools([ATSScore], tool_choice="ATSScore")


def get_candidate_extraction_llm():
    """LLM FORCED to call the CandidateProfile tool, for parsing old resumes."""
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        base_url="https://openrouter.ai/api/v1",
        api_key=_get_api_key(),
        max_tokens=1500,
    ).bind_tools([CandidateProfile], tool_choice="CandidateProfile")



# ---------------------------------------------------------------------------
# 3. Node: fetch_job_posting
#    Scrapes the job URL and pulls the visible text out of the page.
# ---------------------------------------------------------------------------
def fetch_job_posting(state: ResumeState) -> ResumeState:
    url = state["job_url"]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        # Catch EVERYTHING here (not just requests.RequestException) so a
        # bad response, redirect loop, or parsing issue never escapes this
        # node and crashes the graph with an opaque error.
        return {**state, "error": f"Failed to fetch job URL ({type(e).__name__}): {e}"}

    try:
        soup = BeautifulSoup(resp.text, "html.parser")

        # strip out non-content tags
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator="\n")
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        text = text[:15000]
    except Exception as e:
        return {**state, "error": f"Failed to parse job page HTML: {e}"}

    # Many ATS/job boards render the actual JD via JavaScript or return a
    # near-empty shell to non-browser clients. If we got back almost
    # nothing usable, fail loudly instead of silently feeding the LLM garbage.
    if len(text) < 200:
        return {
            **state,
            "error": (
                "Fetched page but extracted almost no text "
                f"({len(text)} chars). This site likely blocks simple "
                "requests or renders content via JavaScript — try the "
                "Playwright-based fetch instead (see fetch_job_posting_playwright)."
            ),
        }

    return {**state, "raw_job_text": text, "error": None}


# ---------------------------------------------------------------------------
# 3b. Optional fallback: fetch_job_posting_playwright
#     Naukri, LinkedIn, and many ATS sites render the JD via JavaScript and/or
#     block plain `requests` calls (403, empty shell page, bot-check).
#     If fetch_job_posting keeps failing / returning near-empty text for a
#     given site, swap the node below in for it.
#
#     Install:  pip install playwright --break-system-packages
#               playwright install chromium
# ---------------------------------------------------------------------------
def fetch_job_posting_playwright(state: ResumeState) -> ResumeState:
    from playwright.sync_api import sync_playwright  # imported lazily, optional dep

    url = state["job_url"]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            )
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # let JS-rendered content settle
            html = page.content()
            browser.close()
    except Exception as e:
        return {**state, "error": f"Playwright fetch failed ({type(e).__name__}): {e}"}

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()[:15000]

    if len(text) < 200:
        return {**state, "error": "Playwright fetch also returned near-empty content."}

    return {**state, "raw_job_text": text, "error": None}


# ---------------------------------------------------------------------------
# 3c. Recommended: fetch_job_posting_firecrawl
#     Uses the Firecrawl API instead of raw requests. Firecrawl handles
#     JavaScript rendering, anti-bot/Cloudflare-style protections, and
#     returns clean Markdown — exactly what naukri.com (and most modern
#     job boards) need, and what an LLM parses best.
#
#     Sign up + get an API key: https://www.firecrawl.dev
#     Set:      export FIRECRAWL_API_KEY="fc-..."
# ---------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"


def fetch_job_posting_firecrawl(state: ResumeState) -> ResumeState:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return {**state, "error": "FIRECRAWL_API_KEY environment variable is not set."}

    url = state["job_url"]
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,   # strips nav/footer/ads automatically
        "waitFor": 2000,           # ms to let JS-rendered content load
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(FIRECRAWL_API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {**state, "error": f"Firecrawl request failed ({type(e).__name__}): {e}"}

    if not data.get("success", False):
        return {**state, "error": f"Firecrawl returned an error: {data.get('error', data)}"}

    markdown = data.get("data", {}).get("markdown", "").strip()

    if len(markdown) < 200:
        return {
            **state,
            "error": f"Firecrawl returned almost no content ({len(markdown)} chars) for {url}.",
        }

    return {**state, "raw_job_text": markdown[:15000], "error": None}


# ---------------------------------------------------------------------------
# 3d. File reading + Node: extract_candidate_profile
#     Reads an uploaded OLD resume (.pdf/.docx/.txt/.md) and extracts it
#     into structured CandidateProfile data via the same reliable
#     forced-tool-call pattern used for job extraction and ATS scoring.
#     This replaces manually typing candidate_profile as free text -- the
#     LLM gets the candidate's REAL skills/experience/education, verified
#     against the actual resume content rather than a hand-written summary.
#
#     Install:  pip install pypdf python-docx --break-system-packages
# ---------------------------------------------------------------------------
def load_text_from_file(path: str) -> str:
    """Extract raw text from a .pdf, .docx, .txt, or .md file."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        from pypdf import PdfReader  # imported lazily, optional dep
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif ext == ".docx":
        import docx  # python-docx, imported lazily, optional dep
        doc = docx.Document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
    elif ext in (".txt", ".md"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    else:
        raise ValueError(f"Unsupported resume file type '{ext}'. Supported: .pdf, .docx, .txt, .md")

    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text


CANDIDATE_EXTRACT_SYSTEM_PROMPT = """You are a precise information-extraction
engine. You will be given raw text extracted from a candidate's OLD resume
file. Extract their name, a short summary, skills, work experience,
education, and certifications/projects into structured data.

CRITICAL rules:
- Extract only what is ACTUALLY in the resume text. Never invent, embellish,
  or infer skills/experience/dates that aren't clearly stated or evidenced.
- Skills must be SHORT, ATOMIC terms (1-4 words each) -- e.g. "Python",
  "FastAPI", "AWS", "PostgreSQL" -- not full sentences.
- Preserve real company names, job titles, dates, and specific
  accomplishments in experience_bullets exactly as they appear (condensed
  into one bullet per role is fine, but don't alter the facts).
- If a section genuinely isn't present in the resume (e.g. no
  certifications), leave that list empty rather than guessing.
"""


def extract_candidate_profile(state: ResumeState) -> ResumeState:
    if state.get("error"):
        return state

    path = state.get("resume_file_path")
    try:
        raw_text = load_text_from_file(path)
    except Exception as e:
        return {**state, "error": f"Failed to read candidate resume file ({type(e).__name__}): {e}"}

    if len(raw_text.strip()) < 50:
        return {
            **state,
            "error": (
                f"Extracted almost no text ({len(raw_text)} chars) from the resume "
                "file. If this is a scanned/image-based PDF, it needs OCR first -- "
                "plain text extraction won't work on it."
            ),
        }

    messages = [
        SystemMessage(content=CANDIDATE_EXTRACT_SYSTEM_PROMPT),
        HumanMessage(content=f"RAW RESUME TEXT:\n\n{raw_text}"),
    ]

    try:
        response = get_candidate_extraction_llm().invoke(messages)
    except Exception as e:
        return {**state, "error": f"Candidate profile extraction failed ({type(e).__name__}): {e}"}

    if not getattr(response, "tool_calls", None):
        return {
            **state,
            "error": f"Model did not return a tool call for candidate extraction. Raw response: {response!r}",
        }

    try:
        args = response.tool_calls[0]["args"]
        cp = CandidateProfile(**args)
    except Exception as e:
        return {**state, "error": f"Could not parse tool_call args into CandidateProfile: {e}"}

    # Deterministic fallback: if the LLM missed the GitHub link (easy to
    # overlook in a dense header/contact section), scan the raw text
    # directly with a regex. Same "don't fully trust the LLM" pattern used
    # elsewhere in this pipeline.
    github_url = cp.github_url.strip()
    if not github_url:
        match = re.search(r"https?://(?:www\.)?github\.com/[A-Za-z0-9\-_.]+", raw_text)
        if match:
            github_url = match.group(0).rstrip("/.,)")

    # Build a clean text block from the structured data -- this is what
    # generate_resume actually reads, so that node doesn't need to change
    # at all; it just receives a richer, verified candidate_profile.
    parts = []
    if cp.name:
        parts.append(f"Name: {cp.name}")
    if cp.summary:
        parts.append(f"Summary: {cp.summary}")
    if cp.skills:
        parts.append("Skills: " + ", ".join(cp.skills))
    if cp.experience_bullets:
        parts.append("Experience:\n" + "\n".join(f"- {b}" for b in cp.experience_bullets))
    if cp.education:
        parts.append("Education:\n" + "\n".join(f"- {e}" for e in cp.education))
    if cp.certifications_projects:
        parts.append("Certifications/Projects:\n" + "\n".join(f"- {c}" for c in cp.certifications_projects))

    return {
        **state,
        "raw_candidate_resume_text": raw_text[:8000],
        "candidate_name": cp.name,
        "candidate_skills": cp.skills,
        "candidate_experience": cp.experience_bullets,
        "candidate_education": cp.education,
        "candidate_certifications": cp.certifications_projects,
        "github_url": github_url,
        "candidate_profile": "\n\n".join(parts),
        "error": None,
    }


# ---------------------------------------------------------------------------
# 3e. Node: fetch_github_projects
#     If a GitHub URL was found in the candidate's resume, pulls their
#     public repos via GitHub's REST API (no MCP needed -- a standalone
#     script can't use Claude's product-side GitHub MCP connector, which
#     requires Anthropic-side auth plumbing; the public REST API achieves
#     the same outcome and works anywhere) and deterministically ranks
#     them against the job's requirements/responsibilities by keyword
#     overlap. The top matches get passed to generate_resume as additional
#     truthful evidence for a Projects section.
#
#     Optional: set GITHUB_TOKEN to raise the rate limit (60/hr
#     unauthenticated -> 5000/hr authenticated). Works fine without it for
#     occasional use.
# ---------------------------------------------------------------------------
GITHUB_API_BASE = "https://api.github.com"


def _extract_github_username(github_url: str) -> Optional[str]:
    match = re.search(r"github\.com/([A-Za-z0-9\-_.]+)", github_url)
    if not match:
        return None
    username = match.group(1).strip("/")
    # strip common non-username path segments people accidentally include
    username = username.split("/")[0]
    return username or None


def _score_repo_relevance(repo: dict, keywords: list) -> int:
    """Deterministic keyword-overlap score -- no LLM guessing needed here."""
    haystack = " ".join(
        filter(
            None,
            [
                repo.get("name", ""),
                repo.get("description", "") or "",
                repo.get("language", "") or "",
                " ".join(repo.get("topics", []) or []),
            ],
        )
    ).lower()
    return sum(1 for kw in keywords if kw.lower() in haystack)


def fetch_github_projects(state: ResumeState) -> ResumeState:
    if state.get("error"):
        return state

    github_url = (state.get("github_url") or "").strip()
    if not github_url:
        return {**state, "github_projects": [], "github_note": "No GitHub URL found in the resume."}

    username = _extract_github_username(github_url)
    if not username:
        return {**state, "github_projects": [], "github_note": f"Couldn't parse a username from '{github_url}'."}

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(
            f"{GITHUB_API_BASE}/users/{username}/repos",
            headers=headers,
            params={"sort": "updated", "per_page": 100, "type": "owner"},
            timeout=15,
        )
        resp.raise_for_status()
        repos = resp.json()
    except Exception as e:
        # GitHub lookup is a nice-to-have enrichment -- never fail the whole
        # pipeline over it. Just note why and move on.
        return {
            **state,
            "github_projects": [],
            "github_note": f"GitHub lookup failed for '{username}' ({type(e).__name__}): {e}",
        }

    if not isinstance(repos, list) or not repos:
        return {**state, "github_projects": [], "github_note": f"No public repos found for GitHub user '{username}'."}

    # Skip forks -- they're rarely a candidate's own work worth highlighting.
    own_repos = [r for r in repos if not r.get("fork")]

    keywords = list(state.get("requirements", [])) + list(state.get("key_responsibilities", []))
    keywords += state.get("candidate_skills", [])  # also credit repos matching the candidate's own stated skills
    keywords = [k for k in keywords if k and len(k) < 40]  # atomic-ish terms only, same filter spirit as elsewhere

    scored = []
    for r in own_repos:
        score = _score_repo_relevance(r, keywords)
        scored.append((score, r))
    scored.sort(key=lambda pair: (pair[0], pair[1].get("stargazers_count", 0)), reverse=True)

    top = scored[:5]
    projects = [
        {
            "name": r.get("name", ""),
            "description": r.get("description") or "",
            "language": r.get("language") or "",
            "url": r.get("html_url", ""),
            "stars": r.get("stargazers_count", 0),
            "relevance_score": score,
        }
        for score, r in top
        if score > 0 or r.get("stargazers_count", 0) > 0  # only surface repos with some signal, not the whole account
    ]

    note = (
        f"Found {len(projects)} job-relevant repo(s) out of {len(own_repos)} public, non-fork repos."
        if projects
        else f"Checked {len(own_repos)} public repos for '{username}' but none matched the job's keywords."
    )

    return {**state, "github_projects": projects, "github_note": note}


# ---------------------------------------------------------------------------
# 4. Node: extract_job_info
#    Forces the LLM to call the JobInfo tool -> guaranteed structured data.
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM_PROMPT = """You are a precise information-extraction engine.
You will be given raw scraped text/markdown from a job posting web page.
Extract the job's company name, role/title, a short description, key
responsibilities, and requirements.

IMPORTANT rules for key_responsibilities and requirements:
- Many job postings do NOT use explicit "Responsibilities:" / "Requirements:"
  headers — the information is often embedded in prose paragraphs instead.
- If there is no explicit bulleted list, READ the full description/prose
  carefully and BREAK IT DOWN into individual, concise bullet points
  yourself. Do not leave these empty just because there was no formal
  heading — only leave them empty if the underlying information genuinely
  does not appear anywhere in the text.
- "key_responsibilities" = what the person will actually DO day-to-day
  (tasks, ownership areas, duties).
- "requirements" = what the candidate needs to HAVE (skills, tools,
  years of experience, degrees, qualifications, soft skills mentioned).
- Do not invent facts that aren't grounded in the source text — only
  rephrase/segment what's actually there.
- Aim for 3-8 bullets per list when the source text supports it.
"""


def extract_job_info(state: ResumeState) -> ResumeState:
    if state.get("error"):
        return state

    messages = [
        SystemMessage(content=EXTRACT_SYSTEM_PROMPT),
        HumanMessage(content=f"RAW JOB POSTING TEXT:\n\n{state['raw_job_text']}"),
    ]

    try:
        response = get_extraction_llm().invoke(messages)
    except Exception as e:
        return {**state, "error": f"Structured extraction failed ({type(e).__name__}): {e}"}

    if not getattr(response, "tool_calls", None):
        # Should not happen with tool_choice forced, but guard anyway so we
        # get a clear diagnostic instead of a downstream AttributeError.
        return {
            **state,
            "error": f"Model did not return a tool call. Raw response: {response!r}",
        }

    try:
        args = response.tool_calls[0]["args"]
        job_info = JobInfo(**args)
    except Exception as e:
        return {**state, "error": f"Could not parse tool_call args into JobInfo: {e}"}

    return {
        **state,
        "company_name": job_info.company_name,
        "job_role": job_info.job_role,
        "job_description": job_info.job_description,
        "key_responsibilities": job_info.key_responsibilities,
        "requirements": job_info.requirements,
        "error": None,
    }


# ---------------------------------------------------------------------------
# 5. Node: generate_resume
#    Combines extracted job info + candidate profile to produce a tailored resume.
# ---------------------------------------------------------------------------
RESUME_SYSTEM_PROMPT = """You are an expert resume writer specializing in
tailoring resumes to a specific target job.

You will be given:
1. Structured job details (company, role, description, key responsibilities,
   requirements) extracted from a real job posting.
2. The candidate's actual background.
3. (Optionally) A list of the candidate's real, verified public GitHub
   repos that were matched to this job's keywords.

Your job is to write a resume that is CLEARLY tailored to this exact job —
not a generic resume. Concretely:
- Go through the job's key_responsibilities and requirements one by one.
  For each one the candidate has relevant experience for, make sure that
  connection is explicit and uses similar terminology/keywords to the
  job posting (this matters for ATS keyword matching).
- Order and emphasize skills/experience so the most relevant-to-this-job
  items appear first.
- If key_responsibilities/requirements were sparse or empty, tailor based
  on the job_description and job_role instead — infer likely relevant
  keywords from those.
- If GitHub projects were provided, weave the most relevant ones into a
  Projects section with the repo name, a one-line truthful description
  based on what's given, and the URL. Do not claim capabilities or scale
  the repo description doesn't actually support.
- NEVER fabricate experience, skills, tools, or qualifications the
  candidate did not actually provide. Tailoring means reframing/reordering
  truthful content, not inventing new content.
- Write in clean, ATS-friendly Markdown.

Structure:
# {Candidate Name or "Candidate"}
## Summary (3-4 lines, written specifically for this role/company — mention
   the role title and 1-2 of the most relevant qualifications)
## Skills (grouped; relevant-to-this-job skills listed first)
## Experience (bullet points reworded to mirror the job's language where
   truthfully applicable; lead each bullet with impact/action verbs)
## Education
## Projects (if relevant GitHub repos were provided, or other projects from
   the candidate's background)
## (Optional) Certifications
"""


def generate_resume(state: ResumeState) -> ResumeState:
    if state.get("error"):
        return state

    job_context = f"""
COMPANY: {state.get('company_name')}
ROLE: {state.get('job_role')}
DESCRIPTION: {state.get('job_description')}
KEY RESPONSIBILITIES:
{chr(10).join('- ' + r for r in state.get('key_responsibilities', []))}
REQUIREMENTS:
{chr(10).join('- ' + r for r in state.get('requirements', []))}
"""

    human_parts = [
        f"JOB DETAILS:\n{job_context}",
        f"CANDIDATE BACKGROUND:\n{state.get('candidate_profile', '')}",
    ]

    github_projects = state.get("github_projects", [])
    if github_projects:
        project_lines = []
        for p in github_projects:
            desc = f" -- {p['description']}" if p.get("description") else ""
            lang = f" ({p['language']})" if p.get("language") else ""
            project_lines.append(f"- {p['name']}{lang}: {p['url']}{desc}")
        human_parts.append(
            "CANDIDATE'S RELEVANT GITHUB PROJECTS (real, verified public repos -- "
            "you MAY reference these truthfully in a Projects section if they "
            "strengthen the resume's relevance to this job; do not embellish "
            "beyond what the name/description states):\n" + "\n".join(project_lines)
        )

    # On a retry (attempt_count > 0), feed back the previous ATS score and
    # feedback so the model actually improves instead of regenerating blind.
    attempt_count = state.get("attempt_count", 0)
    if attempt_count > 0:
        human_parts.append(
            "PREVIOUS ATTEMPT FEEDBACK:\n"
            f"Previous ATS score: {state.get('ats_score')}/100\n"
            f"Missing/weak keywords: {', '.join(state.get('missing_keywords', [])) or 'none'}\n"
            f"Reviewer feedback: {state.get('ats_feedback', '')}\n\n"
            "Revise the resume to directly address this feedback and work in "
            "the missing keywords wherever truthfully applicable. Do not "
            "fabricate anything the candidate doesn't actually have."
        )

    human_parts.append("Write the tailored resume now.")

    messages = [
        SystemMessage(content=RESUME_SYSTEM_PROMPT),
        HumanMessage(content="\n\n".join(human_parts)),
    ]
    try:
        response = get_llm().invoke(messages)
    except Exception as e:
        return {**state, "error": f"Resume generation failed ({type(e).__name__}): {e}"}

    return {**state, "resume_output": response.content.strip(), "error": None}


# ---------------------------------------------------------------------------
# 5b. Node: score_resume
#     Acts as an ATS (Applicant Tracking System) checker: grades the most
#     recent resume against the job's requirements/responsibilities and
#     returns a 0-100 score plus actionable feedback. Forces a tool call
#     the same way extraction does, so scoring is always structured.
# ---------------------------------------------------------------------------
SCORE_SYSTEM_PROMPT = """You are an ATS (Applicant Tracking System) resume
screener. Evaluate the given resume strictly against the given job's
requirements, responsibilities, and description -- the way an automated
ATS keyword/relevance scanner combined with a critical recruiter would.

Be honest and realistic, not generous. Ground the score in the keyword
lists you produce -- do not report a high score while also listing several
important missing keywords; the score and your own evidence must agree.

Rubric (use this to actually derive the number, don't just guess):
- Estimate coverage = (# important requirement/responsibility keywords
  clearly present in the resume) / (total # important keywords identified).
- 0-40: covers well under half of the important keywords/requirements.
- 41-60: covers roughly half; several important gaps remain.
- 61-75: covers most keywords but 2+ meaningfully important ones are
  missing or only weakly represented.
- 76-89: covers nearly all important keywords with at most 1 minor gap.
- 90-100: covers essentially all important keywords/requirements with
  strong, truthful terminology alignment and almost no gaps.
If you list several missing_keywords, the score MUST land in a band that
reflects that -- do not contradict your own missing_keywords list.

CRITICAL rule for matched_keywords / missing_keywords: each item must be a
SHORT, ATOMIC, resume-worthy term (1-4 words) -- a specific skill, tool,
technology, or domain. NEVER copy an entire requirement sentence verbatim.

--- WORKED EXAMPLE ---
Given this job requirement text:
  "Currently pursuing a degree in Computer Science, Engineering, or a
  related field. Basic understanding of AI and machine learning concepts.
  Familiarity with Python programming. Assist in the development and
  implementation of AI models. Ability to work in a team environment."

And this resume excerpt:
  "B.Tech in Computer Science (in progress). Proficient in Python.
  Completed coursework in AI/ML fundamentals. Collaborated on group
  coding projects."

The CORRECT atomic extraction looks like this:
  matched_keywords:  ["Computer Science degree", "Python",
                       "AI/ML fundamentals", "Team Collaboration"]
  missing_keywords:  ["AI Model Development"]

Notice: no full sentences, no responsibility descriptions, no "currently
pursuing..." phrasing -- everything is a short resume-style term. This is
the exact format required. Do not deviate from this pattern.
--- END EXAMPLE ---

Also do not include pure education-status or experience-duration phrases
(e.g. "currently pursuing a degree", "3 years of experience") as
keywords -- those belong in Education/Summary sections, not a Skills list,
so they shouldn't be treated as Skills-section keywords here.

List the specific important keywords/skills from the job that ARE and
ARE NOT well represented in the resume, and give concise, actionable
feedback on what to change.
"""


def score_resume(state: ResumeState) -> ResumeState:
    if state.get("error"):
        return state

    job_context = f"""
COMPANY: {state.get('company_name')}
ROLE: {state.get('job_role')}
DESCRIPTION: {state.get('job_description')}
KEY RESPONSIBILITIES:
{chr(10).join('- ' + r for r in state.get('key_responsibilities', []))}
REQUIREMENTS:
{chr(10).join('- ' + r for r in state.get('requirements', []))}
"""

    messages = [
        SystemMessage(content=SCORE_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"JOB DETAILS:\n{job_context}\n\n"
                f"RESUME TO EVALUATE:\n{state.get('resume_output', '')}\n\n"
                "Score this resume against the job now."
            )
        ),
    ]

    try:
        response = get_scoring_llm().invoke(messages)
    except Exception as e:
        return {**state, "error": f"ATS scoring failed ({type(e).__name__}): {e}"}

    if not getattr(response, "tool_calls", None):
        return {**state, "error": f"Scorer did not return a tool call. Raw response: {response!r}"}

    try:
        args = response.tool_calls[0]["args"]
        ats = ATSScore(**args)
    except Exception as e:
        return {**state, "error": f"Could not parse tool_call args into ATSScore: {e}"}

    # --- Sanity clamp -------------------------------------------------
    # LLM-reported scores (especially from smaller/cheaper models) are
    # often poorly calibrated -- the model can report an optimistic
    # headline number (e.g. 85) while its OWN missing_keywords list
    # clearly shows real gaps. Rather than trust the raw self-reported
    # score blindly, recompute a keyword-coverage percentage and cap the
    # final score so it can't contradict the model's own evidence.
    matched_n = len(ats.matched_keywords)
    missing_n = len(ats.missing_keywords)
    total_kw = matched_n + missing_n
    keyword_coverage = round((matched_n / total_kw) * 100) if total_kw > 0 else 100

    raw_llm_score = ats.score
    # allow a small buffer above raw coverage (holistic fit can be
    # slightly better than a strict keyword-count would suggest) but
    # never let the final score wildly outrun the actual keyword evidence
    final_score = min(raw_llm_score, keyword_coverage + 10)

    attempt_count = state.get("attempt_count", 0) + 1

    # Track the best-scoring attempt so far, in case we never cross the
    # threshold and need to fall back to "the best we managed" at the end.
    # IMPORTANT: track the FULL evidence bundle together (resume, score,
    # keywords, feedback) -- not just resume+score -- so that if we fall
    # back to this attempt later, everything shown to the user (keyword
    # lists, raw score, coverage %) actually describes the SAME attempt
    # as the resume text, instead of mixing fields from different attempts.
    best_score = state.get("best_score", -1)
    best_resume = state.get("best_resume", "")
    best_matched_keywords = state.get("best_matched_keywords", [])
    best_missing_keywords = state.get("best_missing_keywords", [])
    best_feedback = state.get("best_feedback", "")
    best_raw_llm_score = state.get("best_raw_llm_score", 0)
    best_keyword_coverage = state.get("best_keyword_coverage", 0)
    if final_score > best_score:
        best_score = final_score
        best_resume = state.get("resume_output", "")
        best_matched_keywords = ats.matched_keywords
        best_missing_keywords = ats.missing_keywords
        best_feedback = ats.feedback
        best_raw_llm_score = raw_llm_score
        best_keyword_coverage = keyword_coverage

    return {
        **state,
        "ats_score": final_score,
        "ats_raw_llm_score": raw_llm_score,
        "ats_keyword_coverage": keyword_coverage,
        "ats_feedback": ats.feedback,
        "matched_keywords": ats.matched_keywords,
        "missing_keywords": ats.missing_keywords,
        "attempt_count": attempt_count,
        "best_score": best_score,
        "best_resume": best_resume,
        "best_matched_keywords": best_matched_keywords,
        "best_missing_keywords": best_missing_keywords,
        "best_feedback": best_feedback,
        "best_raw_llm_score": best_raw_llm_score,
        "best_keyword_coverage": best_keyword_coverage,
        "error": None,
    }


# ---------------------------------------------------------------------------
# 5c. Node: finalize_best
#     Runs only if the loop hit max_attempts without crossing ats_threshold.
#     Swaps in the best-scoring resume AND its matching evidence (keywords,
#     feedback, raw score) seen across all attempts, so everything shown to
#     the user describes the SAME attempt -- instead of mixing the best
#     resume with a different attempt's keyword analysis.
# ---------------------------------------------------------------------------
def finalize_best(state: ResumeState) -> ResumeState:
    threshold = state.get("ats_threshold", 80)
    return {
        **state,
        "resume_output": state.get("best_resume", state.get("resume_output", "")),
        "ats_score": state.get("best_score", state.get("ats_score", 0)),
        "ats_raw_llm_score": state.get("best_raw_llm_score", state.get("ats_raw_llm_score", 0)),
        "ats_keyword_coverage": state.get("best_keyword_coverage", state.get("ats_keyword_coverage", 0)),
        "matched_keywords": state.get("best_matched_keywords", state.get("matched_keywords", [])),
        "missing_keywords": state.get("best_missing_keywords", state.get("missing_keywords", [])),
        "ats_feedback": state.get("best_feedback", state.get("ats_feedback", "")),
        "final_note": (
            f"Did not reach the target ATS score of {threshold} after "
            f"{state.get('attempt_count', 0)} attempts. Returning the "
            f"best-scoring version found (score: {state.get('best_score')})."
        ),
    }


# ---------------------------------------------------------------------------
# 5d. Node: polish_skills
#     Runs on the final resume (whether it passed the threshold or we gave
#     up and fell back to the best attempt). The Skills section the model
#     writes during generate_resume is often a curated/representative
#     subset, not an exhaustive list. Since matched_keywords have ALREADY
#     been verified by the ATS scorer as truthfully present in the
#     candidate's background, this step rewrites ONLY the Skills section
#     so every single one of them is explicitly listed -- nothing new is
#     invented, we're just making sure nothing verified gets silently
#     dropped from the visible Skills section.
# ---------------------------------------------------------------------------
POLISH_SYSTEM_PROMPT = """You will be given a resume and a list of keywords
that an ATS reviewer has already CONFIRMED as truthfully, accurately
matched between this candidate and the target job.

Your ONLY task: rewrite the "## Skills" section so that EVERY keyword in
the confirmed list is explicitly represented -- as its own bullet, or
grouped sensibly with related keywords on one line (e.g. "Python, FastAPI,
Django" can share a bullet). Do not drop, summarize away, or omit any
confirmed keyword for the sake of brevity.

Rules:
- Do not add any skill/keyword that is NOT in the confirmed list AND not
  already truthfully reflected elsewhere in the resume.
- If any item in the confirmed list is actually a full sentence, a
  responsibility/duty description, or an education-status/experience-
  duration statement (e.g. "currently pursuing a degree...", "3 years of
  experience...") rather than a genuine skill/tool/technology term, DO NOT
  add it as a Skills bullet -- it belongs in Education/Summary, which you
  are not touching. Only add genuinely atomic, resume-worthy skill terms.
- Do not touch any other section (Summary, Experience, Education,
  Certifications/Projects) -- leave them exactly as given.
- Keep clean, ATS-friendly Markdown formatting consistent with the rest
  of the resume.
- Return the COMPLETE resume (all sections), not just the Skills section,
  with no extra commentary before or after it.
"""


def _looks_like_a_skill_keyword(kw: str) -> bool:
    """
    Defense-in-depth filter used before polish_skills. Even with a tightened
    prompt, a smaller model can still occasionally return a full requirement
    sentence instead of an atomic keyword (e.g. "Currently pursuing a degree
    in Computer Science" instead of "Computer Science degree"). Rather than
    trust the model's compliance alone, filter deterministically here:
      - reject anything too long to plausibly be a resume keyword
      - reject anything containing a sentence-ending period (mid-string)
      - reject obvious education-status / experience-duration phrasing,
        which belongs in Education/Summary, not a Skills bullet list
    This errs conservative: a false rejection just means one keyword isn't
    force-added to Skills (harmless); it never lets a full sentence through.
    """
    kw_clean = kw.strip()
    if not kw_clean:
        return False
    if len(kw_clean) > 40 or len(kw_clean.split()) > 5:
        return False
    if "." in kw_clean.rstrip("."):  # period in the middle = likely a sentence
        return False
    lowered = kw_clean.lower()
    banned_phrases = (
        "currently pursuing", "years of experience", "year of experience",
        "assist in", "collaborate with", "participate in", "responsible for",
        "ability to", "basic understanding of", "familiarity with",
    )
    if any(phrase in lowered for phrase in banned_phrases):
        return False
    return True


def polish_skills(state: ResumeState) -> ResumeState:
    if state.get("error"):
        return state

    raw_matched = state.get("matched_keywords", [])
    matched = [kw for kw in raw_matched if _looks_like_a_skill_keyword(kw)]
    resume = state.get("resume_output", "")
    if not matched or not resume:
        return state  # nothing valid to polish against

    messages = [
        SystemMessage(content=POLISH_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"CONFIRMED MATCHED KEYWORDS (every one must appear in Skills):\n"
                f"{', '.join(matched)}\n\n"
                f"CURRENT RESUME:\n{resume}\n\n"
                "Rewrite now, returning the full resume."
            )
        ),
    ]
    try:
        response = get_llm().invoke(messages)
    except Exception as e:
        # Polishing is a nice-to-have; if it fails, keep the original resume
        # rather than losing the whole result.
        return {**state, "final_note": (state.get("final_note", "") + f" [Skills polish step failed: {e}]").strip()}

    return {**state, "resume_output": response.content.strip()}


# ---------------------------------------------------------------------------
# 6. Conditional routing helpers
# ---------------------------------------------------------------------------
def check_error(state: ResumeState) -> str:
    return "error" if state.get("error") else "continue"


def check_ats_score(state: ResumeState) -> str:
    """
    Routes after score_resume:
      - "error"  -> scoring itself failed, bail out
      - "pass"   -> score crossed the threshold, we're done
      - "retry"  -> below threshold but attempts remain, loop back to generate_resume
      - "give_up"-> below threshold and out of attempts, finalize with best attempt
    """
    if state.get("error"):
        return "error"

    threshold = state.get("ats_threshold", 80)
    max_attempts = state.get("max_attempts", 3)

    if state.get("ats_score", 0) >= threshold:
        return "pass"
    if state.get("attempt_count", 0) >= max_attempts:
        return "give_up"
    return "retry"


def after_extract_job_info_router(state: ResumeState) -> str:
    """
    Routes after extract_job_info:
      - "error"        -> job extraction failed, bail out
      - "parse_resume" -> an old resume file was provided, parse it into
                           candidate_profile before generating
      - "have_profile" -> candidate_profile was already provided as text
                           (backward compatible), skip straight to generation
    """
    if state.get("error"):
        return "error"
    if state.get("resume_file_path"):
        return "parse_resume"
    return "have_profile"


# ---------------------------------------------------------------------------
# 7. Build the graph
# ---------------------------------------------------------------------------
def build_graph(fetch_node=fetch_job_posting_firecrawl, checkpointer=None):
    """
    fetch_node: which scraping strategy to use for step 1.
        - fetch_job_posting_firecrawl (default, RECOMMENDED) -> uses Firecrawl API,
          handles JS rendering + anti-bot sites like naukri.com
        - fetch_job_posting            -> plain requests + BeautifulSoup (fast, free,
          but fails on JS-heavy / bot-protected sites)
        - fetch_job_posting_playwright -> local headless browser fallback, no API key needed

    checkpointer: optional LangGraph checkpointer (e.g. a SqliteSaver instance).
        If provided, every node's state gets persisted, keyed by whatever
        thread_id you pass in the invoke/stream config -- enabling full
        state-history inspection and resumability. If None (default),
        the graph runs statelessly exactly as before.

    Candidate profile source (choose ONE per run, set in initial_state):
        - resume_file_path: "/path/to/old_resume.pdf"  -> parsed + extracted
          automatically via extract_candidate_profile (RECOMMENDED -- richer,
          verified data straight from the candidate's real resume)
        - candidate_profile: "free text background..."  -> used as-is if
          resume_file_path isn't set (backward compatible with manual input)

    Loop behavior (generate_resume <-> score_resume):
        generate_resume -> score_resume -> [pass: polish_skills -> END]
                                         -> [retry: back to generate_resume, with feedback]
                                         -> [give_up: finalize_best -> polish_skills -> END]
                                         -> [error: END]
        Controlled by state["ats_threshold"] (default 80) and
        state["max_attempts"] (default 3) -- set these in initial_state to override.
        polish_skills rewrites ONLY the Skills section so every keyword the
        ATS scorer confirmed as truthfully matched is explicitly listed.
    """
    graph = StateGraph(ResumeState)

    graph.add_node("fetch_job_posting", fetch_node)
    graph.add_node("extract_job_info", extract_job_info)
    graph.add_node("extract_candidate_profile", extract_candidate_profile)
    graph.add_node("fetch_github_projects", fetch_github_projects)
    graph.add_node("generate_resume", generate_resume)
    graph.add_node("score_resume", score_resume)
    graph.add_node("finalize_best", finalize_best)
    graph.add_node("polish_skills", polish_skills)

    graph.set_entry_point("fetch_job_posting")

    graph.add_conditional_edges(
        "fetch_job_posting", check_error, {"error": END, "continue": "extract_job_info"}
    )
    graph.add_conditional_edges(
        "extract_job_info",
        after_extract_job_info_router,
        {
            "error": END,
            "parse_resume": "extract_candidate_profile",
            "have_profile": "generate_resume",
        },
    )
    graph.add_conditional_edges(
        "extract_candidate_profile", check_error, {"error": END, "continue": "fetch_github_projects"}
    )
    # fetch_github_projects never sets state["error"] itself (GitHub lookup
    # failures are recorded in github_note instead) -- always continue.
    graph.add_edge("fetch_github_projects", "generate_resume")
    graph.add_edge("generate_resume", "score_resume")
    graph.add_conditional_edges(
        "score_resume",
        check_ats_score,
        {
            "error": END,
            "pass": "polish_skills",       # crossed threshold -> make sure Skills is exhaustive
            "retry": "generate_resume",    # loop back with feedback baked into state
            "give_up": "finalize_best",
        },
    )
    graph.add_edge("finalize_best", "polish_skills")  # best-effort result also gets polished
    graph.add_edge("polish_skills", END)

    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 8. Example usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Persistent checkpointer -- state for every node gets saved to this
    # local .db file, keyed by thread_id below. Survives kernel restarts.
    # CHECKPOINT_DB_PATH lets Docker deployments point this at a mounted
    # volume; defaults to a local file for plain script/notebook use.
    db_path = os.getenv("CHECKPOINT_DB_PATH", "resume_pipeline_checkpoints.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    app = build_graph(checkpointer=checkpointer)

    # thread_id groups all checkpoints for one "session" -- reuse the same
    # id to resume/inspect a run later, or vary it per candidate+job pair.
    config = {"configurable": {"thread_id": "nitin-constems-ai-intern"}}

    # --- Candidate profile: pick ONE of the two options below ---

    # OPTION A (RECOMMENDED): point at an old resume file and let
    # extract_candidate_profile parse it automatically. Supports
    # .pdf, .docx, .txt, .md
    resume_file_path = "old_resume.pdf"  # <-- replace with your actual file path

    # OPTION B (fallback / backward compatible): type the background
    # manually as free text. Only used if resume_file_path isn't set below.
    candidate_background = """
    Name: Nitin Sharma
    3 years experience as a Python backend developer.
    Built REST APIs with FastAPI and Django, worked with PostgreSQL and Redis,
    deployed services on AWS (EC2, Lambda, S3). Familiar with Docker, CI/CD (GitHub Actions),
    and basic Kubernetes. B.Tech in Computer Science.
    """

    initial_state: ResumeState = {
        "job_url": "https://in.indeed.com/cmp/Constems--ai?campaignid=mobvjcmp&from=mobviewjob&tk=1jtbk1k6bhmh0802&fromjk=28f8447db0b44918",
        "resume_file_path": resume_file_path,   # comment this line out to use candidate_profile instead
        "candidate_profile": candidate_background,
        "ats_threshold": 80,   # score needed to accept the resume
        "max_attempts": 3,     # give up and use best attempt after this many cycles
    }

    final_state = app.invoke(initial_state, config=config)

    # With a checkpointer, you can now inspect exactly what each node did:
    #   for snapshot in app.get_state_history(config):
    #       print(snapshot.metadata.get("writes"), snapshot.next)

    if final_state.get("error"):
        print("ERROR:", final_state["error"])
    else:
        if final_state.get("candidate_name"):
            print("=== Parsed Candidate Profile ===")
            print("Name:", final_state.get("candidate_name"))
            print("Skills:", final_state.get("candidate_skills"))
            print("Experience:", final_state.get("candidate_experience"))
            print("Education:", final_state.get("candidate_education"))
            print()

        print("=== Extracted Job Info ===")
        print("Company:", final_state.get("company_name"))
        print("Role:", final_state.get("job_role"))
        print("Description:", final_state.get("job_description"))
        print("Responsibilities:", final_state.get("key_responsibilities"))
        print("Requirements:", final_state.get("requirements"))

        print("\n=== ATS Scoring ===")
        print("Attempts used:", final_state.get("attempt_count"))
        print("Final ATS score (clamped):", final_state.get("ats_score"))
        print("Raw LLM self-reported score:", final_state.get("ats_raw_llm_score"))
        print("Keyword coverage %:", final_state.get("ats_keyword_coverage"))
        print("Matched keywords:", final_state.get("matched_keywords"))
        print("Missing keywords:", final_state.get("missing_keywords"))
        if final_state.get("final_note"):
            print("Note:", final_state["final_note"])


        print("\n=== Generated Resume ===\n")
        print(final_state.get("resume_output"))
