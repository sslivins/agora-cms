FROM python:3.11-slim

WORKDIR /app

# BtbN static FFmpeg 8.1 build (GPL) — auto-select arch
ARG TARGETARCH
ARG FFMPEG_VERSION=n8.1-7-ga3475e2554
ARG FFMPEG_RELEASE=autobuild-2026-04-06-13-14

# libheif-examples for HEIC grid assembly, curl + xz-utils to fetch FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libheif-examples \
    curl \
    xz-utils \
    && ARCH=$(case "$TARGETARCH" in arm64) echo linuxarm64;; *) echo linux64;; esac) \
    && curl -fsSL "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_RELEASE}/ffmpeg-${FFMPEG_VERSION}-${ARCH}-gpl-8.1.tar.xz" \
       | tar -xJ --strip-components=1 -C /usr/local \
    && apt-get purge -y curl xz-utils && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-test.txt

COPY cms/ cms/
COPY tests/ tests/
COPY pytest.ini .

EXPOSE 8080

CMD ["uvicorn", "cms.main:app", "--host", "0.0.0.0", "--port", "8080"]
