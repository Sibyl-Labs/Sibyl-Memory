FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/home/sibyl/.local/bin:${PATH}"

WORKDIR /app

COPY sibyl-memory-client/ /app/sibyl-memory-client/
COPY sibyl-memory-hermes/ /app/sibyl-memory-hermes/
COPY sibyl-memory-mcp/ /app/sibyl-memory-mcp/

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --editable /app/sibyl-memory-client \
                              --editable /app/sibyl-memory-hermes \
                              --editable /app/sibyl-memory-mcp

RUN useradd -m sibyl \
    && mkdir -p /home/sibyl/.sibyl-memory \
    && chown -R sibyl:sibyl /home/sibyl

ENV SIBYL_MEMORY_DB=/home/sibyl/.sibyl-memory/memory.db \
    SIBYL_CREDENTIALS=/home/sibyl/.sibyl-memory/credentials.json

VOLUME ["/home/sibyl/.sibyl-memory"]

USER sibyl

ENTRYPOINT ["python", "-m", "sibyl_memory_mcp"]