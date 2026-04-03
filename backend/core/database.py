"""
core/database.py
────────────────
SQLAlchemy session dependency shared across all routers.
"""
import os
import sys

# Allow imports from the database/ sibling directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))

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
