# Shared image for both services (fastapi_backend + streamlit_chat_app).
# docker-compose.yml builds this once and runs it twice with different
# commands -- see docker-compose.yml for the actual service definitions.

FROM python:3.11-slim

WORKDIR /app

# System deps: none needed beyond the base image. pypdf and python-docx are
# pure Python; we use html.parser (stdlib) instead of lxml for BeautifulSoup,
# so no extra system libraries are required -- keeps the image small.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY job_resume_langgraph.py streamlit_chat_app.py fastapi_backend.py ./

# Non-root user for security.
RUN useradd -m appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# Checkpoint DB lives on a mounted volume (see docker-compose.yml) so it
# persists across container restarts/rebuilds and is shared between the
# backend and chat services.
ENV CHECKPOINT_DB_PATH=/app/data/resume_pipeline_checkpoints.db

# Streamlit-specific env vars for headless container operation: skip the
# "send usage stats?" prompt, don't try to open a browser, and disable the
# module-introspecting file watcher (irrelevant in a container -- there's
# no live code editing happening, and it sidesteps the whole class of
# watcher-vs-optional-dependency crashes we hit locally with transformers).
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV STREAMLIT_SERVER_FILE_WATCHER_TYPE=none

EXPOSE 8000 8501

# No default CMD -- docker-compose.yml specifies the command per service
# (uvicorn for backend, streamlit run for chat). Running this image
# directly without a command override will just print usage help.
CMD ["python", "-c", "print('Specify a command: uvicorn fastapi_backend:app or streamlit run streamlit_chat_app.py -- see docker-compose.yml')"]
