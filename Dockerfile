# syntax=docker/dockerfile:1

FROM python:3.15-alpine3.23

ENV PYCURL_SSL_LIBRARY=openssl \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

RUN apk add --no-cache \
        ca-certificates \
        crc32c \
        ffmpeg \
        libcurl \
        libstdc++ \
        tzdata

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

COPY *.py ./
COPY streamonitor ./streamonitor

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 5000
CMD [ "python3", "Downloader.py"]
