FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml ./
RUN pip install --user --no-cache-dir .

# ---- runtime ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/app/.local/bin:$PATH

RUN groupadd -r app && useradd -r -g app -u 10001 -m -d /home/app app
COPY --from=builder --chown=app:app /root/.local /home/app/.local

WORKDIR /app
COPY --chown=app:app app ./app

USER app
EXPOSE 8080

# `--limit-concurrency 100`: hard cap on simultaneous in-flight requests.
#   Excess requests get 503 immediately rather than queueing on the asyncio
#   default executor (which the WeChat SDK uses via asyncio.to_thread). This
#   gives us self-protection under bursts; without it, a degraded WeChat would
#   pile up requests until OOM.
# `--timeout-graceful-shutdown 15`: wait up to 15s for in-flight payment
#   notifies to complete on SIGTERM. Cutting them mid-flight = lost ack to
#   alipay/wechat = unnecessary retries.
# Note: single worker. Multi-worker would split per-process state (rate limit
#   bucket, httpx pool, WeChat platform-cert cache) and is not supported.
#   Scale by running multiple containers behind the same proxy.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips=*", \
     "--limit-concurrency", "100", \
     "--timeout-graceful-shutdown", "15"]
