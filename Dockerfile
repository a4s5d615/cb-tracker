FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Create data directory (overridden by volume in production)
RUN mkdir -p /data
ENV DATA_DIR=/data

EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
