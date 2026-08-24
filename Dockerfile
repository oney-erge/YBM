# YBM - headless profile.
#
# What runs here: Telegram/WhatsApp intake, the operator loop, filesystem tools
# scoped to mounted volumes, the code interpreter, MCP, and the admin console.
#
# What does NOT run here, by nature rather than by omission: desktop control,
# screenshots, the VS Code bridge, and browser automation that needs a real
# display. There is no session to attach to inside a container. YBM_HEADLESS=1
# makes `ybm doctor` report those as unavailable instead of failing at call
# time - see bootstrap.is_headless_runtime.
#
# Built on uv's image so the container and the host installer agree on how
# Python is provided: scripts/install.ps1 also lets uv supply the interpreter
# rather than requiring one.

# --- frontend: build the admin console ------------------------------------
# Without this the image serves the "No admin console build was found yet - run
# ybm ui-build" placeholder, and that instruction cannot be followed inside a
# container: there is no frontend/ directory and no node. The whole UI was
# missing until this stage existed.
FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94 AS frontend

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# vite.config.ts writes to ../backend/src/agent_control/static/admin, so give
# it that path to write into.
RUN mkdir -p /build/backend/src/agent_control/static \
    && npm run build

# --- WhatsApp: install the optional Node sidecar ---------------------------
# The bridge is disabled by default, but a container advertised as supporting
# it must contain both Node and its production dependencies when an operator
# enables the channel later. Keeping npm in this build stage leaves only the
# Node runtime and resolved module tree in the final image.
FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94 AS whatsapp

WORKDIR /build/whatsapp-bridge
COPY whatsapp-bridge/package.json whatsapp-bridge/package-lock.json ./
RUN npm ci --omit=dev
COPY whatsapp-bridge/src/ ./src/

# --- builder: resolve and install dependencies -----------------------------
FROM ghcr.io/astral-sh/uv:0.9.7-python3.12-bookworm-slim@sha256:4f52717de41541452f5318571b05da783d2ddf346a94b4d5c6512a4b51e986bd AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app/backend

# Dependency layer first: it changes far less often than the source, so an edit
# to agent_control does not re-resolve the whole lockfile.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project \
        --extra voice --extra tray

COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra voice --extra tray

# --- runtime ---------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime

ARG YBM_VERSION=dev
LABEL org.opencontainers.image.title="YBM" \
      org.opencontainers.image.description="Local agent-control system, headless container profile" \
      org.opencontainers.image.source="https://github.com/oney-erge/YBM" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="$YBM_VERSION"

# curl is used by the HEALTHCHECK below; git lets the coding-agent tools work
# against a mounted repository. Both are small and deliberate - nothing else is
# installed, because every extra package is attack surface for a process that
# runs model-chosen tool calls.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git libstdc++6 tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root. The code interpreter's own Docker backend already runs sandboxed
# work as a non-root user; the agent process itself should not be root either.
RUN useradd --create-home --uid 10001 ybm

WORKDIR /app
COPY --from=builder --chown=ybm:ybm /app/backend/.venv /app/backend/.venv
COPY --chown=ybm:ybm backend/ /app/backend/
# The built console, served at /admin. Copied after backend/ so it is not
# clobbered by it.
COPY --from=frontend --chown=ybm:ybm \
     /build/backend/src/agent_control/static/admin /app/backend/src/agent_control/static/admin
# The bridge process resolves `whatsapp-bridge/` relative to /app and invokes
# `node` from PATH. Node's official bookworm build is compatible with this
# bookworm runtime, while npm itself is intentionally not copied.
COPY --from=whatsapp /usr/local/bin/node /usr/local/bin/node
COPY --from=whatsapp --chown=ybm:ybm /build/whatsapp-bridge /app/whatsapp-bridge
COPY --chown=ybm:ybm config/config.example.yaml /app/config/config.example.yaml
COPY --chown=ybm:ybm scripts/ /app/scripts/
COPY --chown=ybm:ybm docs/ /app/docs/
COPY --chown=ybm:ybm skills/ /app/skills/
COPY --chown=ybm:ybm AGENTS.md CHANGELOG.md LICENSE README.md SECURITY.md /app/

# AGENT_ is the settings env prefix and __ the nesting delimiter (config.py's
# SettingsConfigDict), so AGENT_SERVER__HOST maps to server.host. YBM_HEADLESS
# is read directly by bootstrap.is_headless_runtime, not through settings.
#
# 0.0.0.0 binds every interface *inside the container only*; compose publishes
# it to 127.0.0.1 on the host. Binding loopback here would make it unreachable.
# The admin API refuses to serve on a non-loopback host without a token, so set
# AGENT_ADMIN_TOKEN in .env - compose passes it through.
ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    YBM_HEADLESS=1 \
    AGENT_SERVER__HOST=0.0.0.0 \
    AGENT_SERVER__PORT=8765

# Written at runtime, and the mount points compose attaches volumes to.
RUN mkdir -p /app/.agent_control /app/config /app/workspace \
    && chown -R ybm:ybm /app/.agent_control /app/config /app/workspace

USER ybm
EXPOSE 8765

# tini reaps the subprocesses YBM spawns (coding agents, MCP stdio servers),
# which would otherwise accumulate as zombies under PID 1.
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/health || exit 1

# --foreground because start_all spawns detached children and returns; a PID 1
# that exits stops the container. --no-localdeploy because the model server is
# a separate concern here (a compose service, or Ollama on the host).
CMD ["ybm", "start", "--foreground", "--no-localdeploy"]
