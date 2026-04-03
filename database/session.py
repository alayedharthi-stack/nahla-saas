import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost:5432/nahla_saas")

# echo=True only in development — never in production (floods logs, leaks query data)
_echo_sql = os.getenv("ENVIRONMENT", "development") != "production"

engine = create_engine(DATABASE_URL, echo=_echo_sql, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
