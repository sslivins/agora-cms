FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY broker.py ./

EXPOSE 7080

CMD ["python", "-m", "uvicorn", "broker:app", "--host", "0.0.0.0", "--port", "7080"]
