FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Hugging Face Spaces runs the container as uid 1000, not root. Everything the
# app touches at runtime must be owned by that user — including the tiktoken
# cache warmed below, which would otherwise be silently re-downloaded on the
# first request, or fail outright against a read-only home.
RUN useradd -m -u 1000 app

WORKDIR /srv

ENV HOME=/srv \
    TIKTOKEN_CACHE_DIR=/srv/.tiktoken \
    PYTHONPATH=/srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY paritok_adaptive/ ./paritok_adaptive/
COPY harness/ ./harness/
COPY run_pinned_proxy.py README.md LICENSE ./

# Warm the encodings at build time; a cold container would otherwise pay a
# multi-second download on the first request and look broken.
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" \
 && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')" \
 && chown -R app:app /srv

USER app

# Spaces routes to app_port from the README frontmatter (8080). PORT covers
# Render, Railway and Fly, which inject it instead.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
