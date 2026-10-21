FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt \
    && useradd --system --uid 10001 --create-home combine \
    && mkdir -p /data && chown combine:combine /data
COPY backend backend
COPY frontend frontend
COPY scripts scripts
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8765
USER combine
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/readyz',timeout=3)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--no-proxy-headers"]
