# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.8.17 AS uv

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    XDG_CACHE_HOME=/tmp/patchscope-cache

RUN groupadd --gid 10001 patchscope \
    && useradd --uid 10001 --gid patchscope --create-home --shell /usr/sbin/nologin patchscope \
    && mkdir -p /app /data /tmp/patchscope-cache \
    && chown -R patchscope:patchscope /app /data /tmp/patchscope-cache

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app

COPY --chown=patchscope:patchscope pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY --chown=patchscope:patchscope src ./src
COPY --chown=patchscope:patchscope examples ./examples
COPY --chown=patchscope:patchscope .streamlit ./.streamlit
RUN uv sync --frozen --no-dev --no-editable

USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH" \
    PATCHSCOPE_DATA_DIR=/data

EXPOSE 8787 8501
CMD ["patchscope", "serve", "--host", "0.0.0.0", "--port", "8787"]
