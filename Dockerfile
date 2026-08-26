# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

EXPOSE 8000

# Defaults run offline (mock backend + in-memory cache). Override at runtime:
#   -e LLM_BACKEND=groq -e LLM_API_KEY=... -e REDIS_URL=redis://redis:6379/0
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
