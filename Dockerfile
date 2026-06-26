FROM python:3.11-slim AS base

LABEL org.opencontainers.image.title="sibyl-memory-mcp"
LABEL org.opencontainers.image.description="MCP server for Sibyl Memory Plugin — persistent memory for AI agents"
LABEL org.opencontainers.image.source="https://github.com/Sibyl-Labs/Sibyl-Memory"

# Install build deps (needed for sibyl-memory-client's C extensions if any)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install packages in order (client first, then CLI, then MCP)
COPY sibyl-memory-client/ ./sibyl-memory-client/
COPY sibyl-memory-cli/ ./sibyl-memory-cli/
COPY sibyl-memory-mcp/ ./sibyl-memory-mcp/
COPY sibyl-memory-hermes/ ./sibyl-memory-hermes/

RUN pip install --no-cache-dir \
    ./sibyl-memory-client \
    ./sibyl-memory-cli \
    ./sibyl-memory-hermes \
    ./sibyl-memory-mcp

# Clean up build deps
RUN apt-get purge -y gcc && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Persistent volume for SQLite database
VOLUME ["/data"]
ENV SIBYL_MEMORY_DB=/data/memory.db

# Run the MCP server on stdio
ENTRYPOINT ["python", "-m", "sibyl_memory_mcp"]
