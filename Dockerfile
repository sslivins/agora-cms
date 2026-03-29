FROM python:3.11-slim

WORKDIR /app

# ffmpeg for video/image transcoding
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cms/ cms/

EXPOSE 8080

CMD ["uvicorn", "cms.main:app", "--host", "0.0.0.0", "--port", "8080"]
