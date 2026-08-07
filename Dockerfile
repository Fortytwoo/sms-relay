FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 smsrelay \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent smsrelay

WORKDIR /app
COPY --chown=10001:10001 app.py /app/app.py
COPY --chown=10001:10001 web /app/web

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["python", "/app/app.py"]
