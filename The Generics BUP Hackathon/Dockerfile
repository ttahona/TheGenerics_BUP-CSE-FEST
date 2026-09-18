FROM python:3.12-slim

WORKDIR /app

# Install curl for the docker-compose healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source, frontend web dashboard, and samples
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY samples/ /app/samples/

ENV PYTHONPATH=/app/backend \
    PORT=8000 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Run FastAPI service with a single worker (LP state is not shared, single process is fastest)
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
