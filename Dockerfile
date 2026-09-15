# Playwright's own image carries Chromium plus every system library it needs —
# the dependency list is long and easy to get subtly wrong by hand.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt requirements-service.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-service.txt

COPY website_audit/ ./website_audit/
COPY service/ ./service/

# Chromium's own sandbox needs privileges a managed container platform does not
# grant. Cloud Run runs every container inside gVisor, which is the isolation
# boundary here — this flag does not remove that, it stops Chromium trying to
# nest a second sandbox inside it and failing to start.
ENV AUDIT_CHROMIUM_EXTRA_ARGS="--no-sandbox"

# Cloud Run sends traffic to $PORT and will not route to anything else.
ENV PORT=8080
EXPOSE 8080

# Chromium renders untrusted third-party pages, so it runs as a non-root user.
# The image provides pwuser and the browsers are already readable by it.
USER pwuser

CMD exec uvicorn service.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75
