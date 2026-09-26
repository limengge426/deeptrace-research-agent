# Stage 1: build the web console.
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npx tsc --noEmit && npx vite build --outDir /web-dist

# Stage 2: the API and workers, serving the console at /.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY --from=web /web-dist ./src/deeptrace_agent/web_dist
COPY examples ./examples
RUN pip install --no-cache-dir ".[server]" \
    && useradd --create-home --uid 1000 app \
    && mkdir /data && chown app /data

USER app
ENV DEEPTRACE_DB=/data/deeptrace.db \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# API with an embedded worker and the web console. docker-compose.yml runs the API and workers separately.
CMD ["deeptrace", "serve", "--host", "0.0.0.0", "--port", "8000"]
