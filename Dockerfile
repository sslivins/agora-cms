FROM python:3.11-slim

WORKDIR /app

# FFmpeg 8.1 static build hosted in our own repo release
ARG TARGETARCH

# libheif-examples for HEIC grid assembly, curl + xz-utils to fetch FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libheif-examples \
    curl \
    xz-utils \
    && ARCH=$(case "$TARGETARCH" in arm64) echo linuxarm64;; *) echo linux64;; esac) \
    && curl -fsSL "https://github.com/sslivins/agora-cms/releases/download/ffmpeg-8.1/ffmpeg-${ARCH}.tar.xz" \
       | tar -xJ --strip-components=1 -C /usr/local \
    && apt-get purge -y curl xz-utils && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-shared.txt requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-test.txt

COPY shared/ shared/
COPY worker/ worker/
COPY cms/ cms/
COPY tests/ tests/
COPY pytest.ini .

EXPOSE 8080

CMD ["uvicorn", "cms.main:app", "--host", "0.0.0.0", "--port", "8080"]
