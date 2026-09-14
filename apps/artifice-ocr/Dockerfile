# ---- builder ----
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app

# Layer 1 — dependency resolution (cached unless lock, a pyproject.toml, or packages/ changes)
COPY pyproject.toml uv.lock ./
COPY apps/artifice-ocr/pyproject.toml        apps/artifice-ocr/
COPY apps/artifice-draft/pyproject.toml      apps/artifice-draft/
COPY apps/artifice-graph/pyproject.toml      apps/artifice-graph/
COPY apps/artifice-transcribe/pyproject.toml apps/artifice-transcribe/
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
