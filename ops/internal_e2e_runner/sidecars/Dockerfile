FROM python:3.11-slim

ARG NAHLA_PINNED_REVISION
LABEL nahla.pinned_revision="${NAHLA_PINNED_REVISION}"
RUN test -n "${NAHLA_PINNED_REVISION}" \
    && printf '%s\n' "${NAHLA_PINNED_REVISION}" > /nahla-revision

WORKDIR /sidecar
COPY ops/internal_e2e_runner/sidecars/*.py ./
USER 65532:65532
