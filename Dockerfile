FROM python:3.12-alpine

# Install build deps for any C extensions
RUN apk add --no-cache gcc musl-dev

# Install uv
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies first (layer cache)
COPY pyproject.toml .
RUN uv pip install --system fastapi uvicorn jinja2 "docker>=7.0.0" "httpx>=0.27.0"

# Copy application code
COPY . .

# Create data directory for persisted node list
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
