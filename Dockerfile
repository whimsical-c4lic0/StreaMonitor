# syntax=docker/dockerfile:1

FROM python:3.15-alpine3.23

ENV PYCURL_SSL_LIBRARY=openssl \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/app

RUN apk add --no-cache \
        ca-certificates \
        crc32c \
        ffmpeg \
        libcurl \
        libstdc++ \
        tzdata \
    && addgroup -S -g 1000 streamonitor \
    && adduser -S -D -H -u 1000 -G streamonitor streamonitor

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./

# pycurl and google-crc32c have no musllinux wheels; build them against
# Alpine's libcurl/crc32c, then remove the compiler packages from this layer.
RUN apk add --no-cache --virtual .build-deps \
        build-base \
        crc32c-dev \
        curl-dev \
        openssl-dev \
    && uv sync --frozen --no-dev \
    && apk del .build-deps

COPY --chown=streamonitor:streamonitor *.py ./
COPY --chown=streamonitor:streamonitor streamonitor ./streamonitor
COPY --chown=streamonitor:streamonitor docker/healthcheck.py /usr/local/bin/healthcheck.py

# The recorded media and configuration live here. Volumes should be mounted
# with the matching UID/GID so the unprivileged process can keep writing.
RUN mkdir -p /app/downloads \
    && printf '[]\n' > /app/config.json \
    && chown streamonitor:streamonitor /app /app/downloads /app/config.json

ENV PATH="/app/.venv/bin:$PATH"

USER streamonitor

EXPOSE 5000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "/usr/local/bin/healthcheck.py"]

CMD [ "python3", "Downloader.py"]
