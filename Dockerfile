# The harness and its web UI. compose.yaml runs it next to Ollama with a decision model.
FROM python:3.12-slim

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[web,mcp,anthropic]" && rm -rf /src

RUN useradd --create-home --uid 1000 harness && mkdir /data && chown harness /data
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
USER harness
WORKDIR /data
ENV HARNESS_IN_CONTAINER=1 PYTHONUNBUFFERED=1
EXPOSE 8765
VOLUME ["/data"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
