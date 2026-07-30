FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY paritok_adaptive/ ./paritok_adaptive/
COPY harness/ ./harness/
COPY run_pinned_proxy.py README.md LICENSE ./

# The orchestrator spawns proxy subprocesses that import paritok_adaptive.
ENV PYTHONPATH=/srv

# Warm the tiktoken encoding at build time; the first request otherwise pays a
# multi-second download, and a cold container would look broken.
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')"

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
