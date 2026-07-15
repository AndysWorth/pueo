# Use the official Python 3.14 slim image for a compact, production-ready footprint
FROM python:3.14-slim

# Set system environment paths
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

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

CMD ["python", "main.py", "--config", "config.yaml"]
