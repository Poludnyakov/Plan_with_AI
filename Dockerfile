# Use official lightweight Python image
FROM python:3.12-slim

# Set environment variables to prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

# Set working directory in container
WORKDIR /app

# Install system dependencies (build-essential and libpq-dev for PostgreSQL interaction, curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements.txt to leverage Docker cache layers
COPY requirements.txt /app/

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# MAX API uses the Russian Trusted Root CA required by the official MAX docs.
COPY certs/russian_trusted_root_ca.crt /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
RUN update-ca-certificates

# Copy the rest of the project files
COPY . /app/

# Expose port 8000 for FastAPI
EXPOSE 8000

# Default command (will be overridden for the bot worker in docker-compose.yml)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
