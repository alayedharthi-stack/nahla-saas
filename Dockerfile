FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir SQLAlchemy==2.0.30 alembic==1.13.1 psycopg2-binary==2.9.9
COPY database ./database
COPY backend/core/__init__.py ./backend/core/__init__.py
COPY backend/core/commerce_runtime/__init__.py ./backend/core/commerce_runtime/__init__.py
COPY backend/core/commerce_runtime/models.py ./backend/core/commerce_runtime/models.py
COPY backend/core/commerce_runtime/navigation_models.py ./backend/core/commerce_runtime/navigation_models.py
COPY backend/core/commerce_runtime/navigation.py ./backend/core/commerce_runtime/navigation.py
COPY ops/production_migrations/apply_0113.py ./ops/production_migrations/apply_0113.py
CMD ["python", "-u", "ops/production_migrations/apply_0113.py"]
