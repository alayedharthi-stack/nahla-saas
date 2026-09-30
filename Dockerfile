# Off-send evaluation image (ops branch only — never merged).
# PostgreSQL 16 in the container for a disposable database; the application
# code of this commit; the real model through ANTHROPIC_API_KEY. No WhatsApp,
# no production database. Idle unless EVAL_CONFIRM=RUN_OFFSEND_EVAL.
FROM postgres:16-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
WORKDIR /app
COPY requirements.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY . .
ENTRYPOINT []
CMD ["bash", "ops/commerce_runtime_eval/run.sh"]
