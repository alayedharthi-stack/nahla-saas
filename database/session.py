import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost:5432/nahla_saas")

# echo=True only in development — never in production (floods logs, leaks query data)
_echo_sql = os.getenv("ENVIRONMENT", "development") != "production"

# pool_pre_ping: drop dead TCP connections before checkout.
# connect_timeout (libpq): never block the worker on TCP retry budget (~120s).
# pool_timeout: fail checkout instead of waiting indefinitely when pool is exhausted.
_conn_timeout = int(os.getenv("NAHLA_DB_CONNECT_TIMEOUT", "10"))
_pool_timeout = int(os.getenv("NAHLA_DB_POOL_TIMEOUT", "20"))
_pool_recycle = int(os.getenv("NAHLA_DB_POOL_RECYCLE", "280"))
_stmt_timeout_ms = os.getenv("NAHLA_DB_STATEMENT_TIMEOUT_MS", "15000").strip()

_connect_args = {"connect_timeout": _conn_timeout}
if _stmt_timeout_ms.isdigit() and int(_stmt_timeout_ms) > 0:
    _connect_args["options"] = f"-c statement_timeout={int(_stmt_timeout_ms)}"

engine = create_engine(
    DATABASE_URL,
    echo=_echo_sql,
    future=True,
    pool_pre_ping=True,
    pool_recycle=_pool_recycle,
    pool_timeout=_pool_timeout,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
