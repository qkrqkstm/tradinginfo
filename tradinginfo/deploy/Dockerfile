FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY scripts/requirements.txt scripts/
RUN pip install -r scripts/requirements.txt

COPY scripts/ scripts/
COPY config.json .
RUN mkdir -p state docs/data && \
    useradd -u 10001 -m app && chown -R app:app /app
USER app

# --max-runtime 0 = 무한 루프 (컨테이너 수명 = 프로세스 수명)
CMD ["python", "scripts/catalyst_monitor.py", "--loop", "--interval", "30", "--max-runtime", "0"]
