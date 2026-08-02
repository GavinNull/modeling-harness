# A production build must pass immutable image and CLI-version inputs, for example:
# docker build --build-arg NODE_BASE_IMAGE=node@sha256:<verified-digest> \
#   --build-arg CODEX_CLI_VERSION=<reviewed-version> -f containers/codex-agent/Dockerfile .
ARG NODE_BASE_IMAGE=node:22-bookworm-slim
FROM ${NODE_BASE_IMAGE}

ARG CODEX_CLI_VERSION
RUN apt-get update \
    && apt-get install --no-install-recommends --yes python3 \
    && rm -rf /var/lib/apt/lists/* \
    && test -n "${CODEX_CLI_VERSION}" \
    && npm install --global --ignore-scripts "@openai/codex@${CODEX_CLI_VERSION}" \
    && useradd --system --uid 65532 --create-home agent

COPY containers/codex-agent/codex_worker.py /opt/modeling-harness/codex_worker.py
COPY workspaces/prompts/system_prompts/ /opt/modeling-harness/prompts/
RUN chmod 0555 /opt/modeling-harness/codex_worker.py \
    && chmod -R a-w /opt/modeling-harness/prompts

USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/modeling-harness/codex_worker.py"]
