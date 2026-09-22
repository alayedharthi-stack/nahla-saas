FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir SQLAlchemy==2.0.30 alembic==1.13.1 psycopg2-binary==2.9.9
COPY database ./database
COPY ops/production_migrations/apply_0112.py ./ops/production_migrations/apply_0112.py
CMD ["python", "-u", "ops/production_migrations/apply_0112.py"]
