# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

FROM python:3.12-alpine3.24@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a

ARG SOURCE_REVISION=unknown

LABEL org.opencontainers.image.title="Media Manager" \
      org.opencontainers.image.description="Bounded media conversion API" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY --from=uv /uv /uvx /bin/

RUN apk add --no-cache ca-certificates ffmpeg

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && adduser -D -u 10001 -h /home/media -s /sbin/nologin media \
    && mkdir --mode=0700 /work \
    && chown 10001:10001 /work

USER 10001:10001

EXPOSE 8080

CMD ["/app/.venv/bin/uvicorn", "media_manager.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
