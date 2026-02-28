FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global user.email "team-claw@ai" \
    && git config --global user.name "Team Claw" \
    && git config --global safe.directory /workspace

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy base runtime
COPY models.py message_bus.py agent.py entrypoint.py ./
COPY tools/ ./tools/

# /agent is a volume mount — role-specific files (system_prompt.md, config.py)
# /workspace is a volume mount — shared code artifacts

CMD ["python", "entrypoint.py"]
