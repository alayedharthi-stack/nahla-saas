"""
core/database.py
────────────────
SQLAlchemy session dependency shared across all routers.
"""
import os

# Allow imports from the database/ sibling directory

from session import SessionLocal  # noqa: E402


def get_db():
    """FastAPI dependency — yields a SQLAlchemy session, rolls back on error."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
