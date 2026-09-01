FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 reporthub && mkdir -p /data && chown reporthub:reporthub /data
USER reporthub
ENV REPORT_HUB_DATA_DIR=/data
EXPOSE 58787
CMD ["report-hub"]
