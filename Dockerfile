# Build wheels in one stage, install them in a clean one, so compilers and caches
# never end up in the image that runs.
FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim
ARG APP_VERSION=dev
ARG GIT_SHA=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=$APP_VERSION \
    GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.source="https://github.com/Jainam004747/lease-ledger-k8s" \
      org.opencontainers.image.description="lease-ledger HTTP API" \
      org.opencontainers.image.revision=$GIT_SHA

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /srv
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/* && rm -rf /wheels
COPY migrations ./migrations
COPY app ./app

# A numeric user so Kubernetes can verify runAsNonRoot without reading /etc/passwd
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "20"]
