# SPDX-FileCopyrightText: 2026 Maurice Casey
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Standalone-repo Dockerfile for artifice-ocr. Copied into place by
# scripts/export-to-public-repos.sh -- edit HERE, not in the public repo,
# since a re-export overwrites apps/artifice-ocr/Dockerfile with this file.
# The version rsync'd straight from apps/artifice-ocr/Dockerfile in the
# monorepo assumes the full four-app workspace (draft/graph/transcribe
# pyproject.toml COPY lines) and does not build in the single-app repo.

# ---- builder ----
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app

# Layer 1 — dependency resolution (cached unless lock, a pyproject.toml, or packages/ changes)
COPY pyproject.toml uv.lock ./
COPY apps/artifice-ocr/pyproject.toml apps/artifice-ocr/
COPY packages/ packages/
RUN uv sync --locked --no-install-project --package artifice-ocr --extra web

# Layer 2 — full source + project install
COPY . .
RUN uv sync --locked --package artifice-ocr --extra web

# ---- runtime ----
FROM python:3.12-slim AS runtime
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv

RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /app
COPY --from=builder /app /app

EXPOSE 8000
USER appuser
CMD ["uv", "run", "--no-sync", "uvicorn", "artifice_ocr.web.server:app", \
     "--host", "0.0.0.0", "--port", "8000"]
