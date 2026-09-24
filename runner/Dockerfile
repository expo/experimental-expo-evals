FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/harbor
COPY pyproject.toml uv.lock e2b_environment.py ./
RUN uv sync --frozen --no-dev --no-editable
COPY runner/service.py /opt/runner/service.py
RUN useradd --create-home --uid 1000 runner && mkdir /work && chown runner:runner /work
USER runner
WORKDIR /work
ENV PYTHONUNBUFFERED=1 HARBOR_TELEMETRY=0
EXPOSE 8080
CMD ["/opt/harbor/.venv/bin/python", "/opt/runner/service.py"]
