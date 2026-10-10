# syntax=docker/dockerfile:1.7
# Shared image for Python workspace members. Build with:
#   docker build -f infra/docker/python-service.Dockerfile --build-arg PACKAGE=demo-checkout .
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.4

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
ARG PACKAGE
RUN test -n "${PACKAGE}" || (echo "PACKAGE build-arg is required" && exit 1)
COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY demo/microservices ./demo/microservices
COPY services ./services
COPY apps ./apps
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
