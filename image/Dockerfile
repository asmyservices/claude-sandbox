FROM node:22-bookworm-slim

ARG APT_PACKAGES=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq less openssh-client procps \
        python3 python3-pip python3-venv \
        $APT_PACKAGES \
    && rm -rf /var/lib/apt/lists/*

ARG PYTHON_PACKAGES=""
RUN if [ -n "$PYTHON_PACKAGES" ]; then \
        pip3 install --no-cache-dir --break-system-packages $PYTHON_PACKAGES; \
    fi

# Bumped by `./sandbox upgrade` so this layer re-runs and pulls the newest CLI.
ARG CACHEBUST=0
ARG CLAUDE_CODE_VERSION=latest
RUN echo "cachebust=$CACHEBUST" \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

COPY hostctl /usr/local/bin/hostctl
RUN chmod 0755 /usr/local/bin/hostctl \
    && mkdir -p /home/agent /workspace \
    && chmod 0777 /home/agent /workspace

ENV HOME=/home/agent
WORKDIR /workspace
CMD ["sh", "-c", "exec claude remote-control --name \"$SANDBOX_NAME\""]
