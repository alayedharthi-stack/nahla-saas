# Confined internal E2E runner image (off-Railway network envelope).
FROM python:3.11-slim

ARG NAHLA_PINNED_REVISION
LABEL nahla.pinned_revision="${NAHLA_PINNED_REVISION}"
ENV GIT_COMMIT_SHA=${NAHLA_PINNED_REVISION}
RUN test -n "${NAHLA_PINNED_REVISION}" \
    && printf '%s\n' "${NAHLA_PINNED_REVISION}" > /nahla-revision

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        gcc \
        iproute2 \
        iptables \
        libcap2-bin \
        libpq-dev \
        nftables \
        openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY database ./database
COPY scripts ./scripts
COPY ops/internal_e2e_runner ./ops/internal_e2e_runner

ENV PYTHONPATH=/app
ENV NAHLA_INTERNAL_E2E_EVIDENCE_DIR=/evidence

RUN sed -i 's/\r$//' /app/ops/internal_e2e_runner/scripts/*.sh \
    && chmod +x /app/ops/internal_e2e_runner/scripts/*.sh

ENTRYPOINT ["/app/ops/internal_e2e_runner/scripts/entrypoint.sh"]
CMD ["preflight"]
