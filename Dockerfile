FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSPACE=/data/workspace.json

WORKDIR /app

COPY feedback_triage ./feedback_triage
COPY data ./data

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"

CMD ["python", "-m", "feedback_triage", "--workspace", "/data/workspace.json", "serve", "--host", "0.0.0.0", "--port", "8080"]
