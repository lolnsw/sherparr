FROM python:3.13-slim

# Install rsync and other system dependencies
RUN apt-get update && apt-get install -y \
    rsync \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project files
COPY pyproject.toml ./
COPY shared/ ./shared/
COPY sherparr_client/ ./sherparr_client/

# Install dependencies
RUN uv sync

# Create mount points
RUN mkdir -p /mnt/remotes /mnt/users /mnt/user/logs

# Run client
CMD ["uv", "run", "python", "-m", "sherparr_client.main"]