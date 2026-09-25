FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv
COPY --from=node:24-bookworm-slim /usr/local/ /usr/local/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates openssh-client && rm -rf /var/lib/apt/lists/*
RUN npm install --global eas-cli@24.7.0
WORKDIR /opt/harbor
COPY pyproject.toml uv.lock e2b_environment.py eas_environment.py ./
COPY eas_provider ./eas_provider
RUN uv sync --frozen --no-dev --no-editable
COPY runner/service.py /opt/runner/service.py
RUN useradd --create-home --uid 1000 runner && mkdir /work && chown runner:runner /work
USER runner
WORKDIR /work
ENV PYTHONUNBUFFERED=1 HARBOR_TELEMETRY=0
EXPOSE 8080
CMD ["/opt/harbor/.venv/bin/python", "/opt/runner/service.py"]
