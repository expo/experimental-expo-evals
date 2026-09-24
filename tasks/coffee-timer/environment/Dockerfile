FROM node:24-bookworm

RUN curl -LsSf https://astral.sh/uv/0.12.18/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
RUN uv python install 3.12

WORKDIR /app
COPY setup.sh /setup.sh
RUN bash /setup.sh
