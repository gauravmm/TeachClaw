FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# Install Node.js 20 for the mermaid CLI (mmdc)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* && \
    npm install -g @mermaid-js/mermaid-cli

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p benchclaw && touch benchclaw/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf benchclaw

# Copy the full source and install
COPY benchclaw/ benchclaw/
RUN uv pip install --system --no-cache .

# Create config directory
RUN mkdir -p /root/.benchclaw

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["benchclaw"]
CMD ["status"]
