ARG PYTHON_BASE
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY docker/runtime-requirements.lock.txt ./runtime-requirements.lock.txt
RUN --mount=type=bind,from=runtime_wheels,target=/wheels \
    python -m pip install --no-index --find-links=/wheels --only-binary=:all: \
      --require-hashes --no-cache-dir -r runtime-requirements.lock.txt \
    && python -m pip check \
    && groupadd --gid 10001 webops-app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin webops-app
COPY app/ ./app/

ARG WEBOPS_VERSION
LABEL org.opencontainers.image.title="webops-app" \
      org.opencontainers.image.revision="${WEBOPS_VERSION}"

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2); assert r.status == 200 and json.load(r) == {'status':'ok'}"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
