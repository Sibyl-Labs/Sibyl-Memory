# syntax=docker/dockerfile:1
# First-party Docker image for the Sibyl Memory MCP server (stdio transport).
# Non-root, pinned slim base, no secrets baked in. Storage lives on a mounted
# volume at /home/app/.sibyl-memory. See sibyl-memory-mcp/README.md "Docker".
FROM python:3.12-slim-bookworm  # TODO: pin by digest before publish (no docker daemon in build env)

# No build tools needed: sibyl-memory-mcp is pure Python and sqlite3 + FTS5
# ship in the stdlib. Keep the layer count and surface minimal.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root runtime user with a real home for the default ~/.sibyl-memory path.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app app

# Install the MCP server from the local monorepo source (first-party build,
# not pulled from PyPI at image-build time). Copy only the two packages the
# server needs, then pip install the mcp package which pulls its declared
# deps (mcp, sibyl-memory-client, sibyl-memory-hermes) from PyPI.
WORKDIR /app
COPY sibyl-memory-mcp/ /app/sibyl-memory-mcp/
RUN pip install /app/sibyl-memory-mcp

# Memory dir on the mounted volume, owned by the non-root user, mode 0700.
RUN mkdir -p /home/app/.sibyl-memory \
 && chown -R app:app /home/app/.sibyl-memory \
 && chmod 700 /home/app/.sibyl-memory
VOLUME ["/home/app/.sibyl-memory"]

USER app
ENV HOME=/home/app

# stdio MCP server. Run attached with STDIN open (docker run -i), no TTY.
ENTRYPOINT ["sibyl-memory-mcp"]
