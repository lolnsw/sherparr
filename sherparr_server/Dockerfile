FROM python:3.13-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project files
COPY pyproject.toml ./
COPY shared/ ./shared/
COPY sherparr_server/ ./sherparr_server/

# Install dependencies
RUN uv sync --frozen

# Expose port
EXPOSE 8000

# Run server
CMD ["uv", "run", "uvicorn", "sherparr_server.main:app", "--host", "0.0.0.0", "--port", "8000"]