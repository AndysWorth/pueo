# Use the official Python 3.14 slim image for a compact, production-ready footprint
FROM python:3.14-slim

# Set system environment paths
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Map Pueo's platform-dir env vars to Docker-appropriate mount points.
# These override the platformdirs defaults so all runtime state lands in
# named volumes rather than inside the /app image layer.
ENV PUEO_CONFIG_DIR=/config \
    PUEO_DATA_DIR=/data \
    PUEO_STATE_DIR=/state \
    PUEO_CACHE_DIR=/cache \
    PUEO_LOG_DIR=/logs

WORKDIR /app

# Install native dependencies required for network tools (e.g., pinging/network triage)
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Create volume mount points so they exist in the base image even before
# a volume is attached.  main.py also calls dirs.create_all() at startup,
# but the image-layer directories prevent permission errors on first run.
RUN mkdir -p /config /data /state /cache /logs

# Declare the mutable directories as volumes so Docker tracks them separately
# from the image layer and they survive container recreation.
VOLUME ["/config", "/data", "/state", "/cache", "/logs"]

# config.yaml lives in the /config volume; PUEO_CONFIG_DIR tells paths.py
# where to look for it (falls back to config.yaml in the same dir as main.py
# if the volume is empty on first run).
CMD ["python", "main.py"]
