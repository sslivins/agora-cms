FROM python:3.11-slim

ENV PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1

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
RUN LANG=C.UTF-8 LC_ALL=C.UTF-8 pip install --no-cache-dir --progress-bar off \
    -r requirements.txt -r requirements-test.txt

COPY shared/ shared/
COPY worker/ worker/
COPY cms/ cms/
COPY tests/ tests/
COPY pytest.ini .
COPY alembic.ini .
COPY alembic/ alembic/

EXPOSE 8080

# Trust X-Forwarded-For / Forwarded headers from any upstream by default.
# Safe for local docker-compose (not externally reachable). Production
# deployments override this via the container env var FORWARDED_ALLOW_IPS
# (e.g. the Container Apps bicep sets it to the infrastructure subnet CIDR).
ENV FORWARDED_ALLOW_IPS=*

CMD ["uvicorn", "cms.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
