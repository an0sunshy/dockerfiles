FROM python:3.12-slim

LABEL maintainer="an0sunshy <an0sunshy@gmail.com>"
LABEL org.opencontainers.image.source=https://github.com/an0sunshy/dockerfiles
LABEL org.opencontainers.image.description="Claude Code usage/cost exporter — parses ~/.claude/projects transcripts into Prometheus metrics"

# stdlib-only; no requirements to install
COPY claude-usage-exporter.py /app/claude-usage-exporter.py

# Args (e.g. --serve --listen=0.0.0.0 --port=9119 --projects-dir=/projects) are
# supplied by the caller / compose command.
ENTRYPOINT ["python", "/app/claude-usage-exporter.py"]
