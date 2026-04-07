FROM python:3.11-slim

WORKDIR /app

# BtbN static FFmpeg 8.1 build (GPL, linux64)
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-04-06-13-14/ffmpeg-n8.1-7-ga3475e2554-linux64-gpl-8.1.tar.xz

# libheif-examples for HEIC grid assembly, curl + xz-utils to fetch FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libheif-examples \
    curl \
    xz-utils \
    && curl -fsSL "$FFMPEG_URL" | tar -xJ --strip-components=1 -C /usr/local \
    && apt-get purge -y curl xz-utils && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-test.txt

COPY cms/ cms/
COPY tests/ tests/
COPY pytest.ini .

EXPOSE 8080

CMD ["uvicorn", "cms.main:app", "--host", "0.0.0.0", "--port", "8080"]
