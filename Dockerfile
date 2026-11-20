FROM python:3.11-slim

# Install ffmpeg for video thumbnail generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Environment settings for 24/7 cloud operation
ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV POSTKARO_VAULT=file
ENV POSTKARO_HOME=/app/data
ENV RENDER=true
ENV ALLOWED_HOSTS=*

RUN mkdir -p /app/data

EXPOSE 10000

# Start Uvicorn web server
CMD ["sh", "-c", "uvicorn postkaro.server:app --host 0.0.0.0 --port ${PORT:-10000}"]
