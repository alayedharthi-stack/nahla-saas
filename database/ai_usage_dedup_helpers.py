"""Install only the AI response identity index; never reconcile old records."""
import sqlalchemy as sa

INDEX_NAME = "uq_ai_usage_provider_request"


def install_index(bind):
    existing = next((i for i in sa.inspect(bind).get_indexes("ai_usage_events")
                     if i["name"] == INDEX_NAME), None)
    if existing is not None:
        if (not existing["unique"] or existing["column_names"] != ["provider", "request_id"]
                or existing.get("dialect_options", {}).get("postgresql_where")):
            raise RuntimeError("AI usage index definition requires review")
        return "already_installed"
    duplicates = bind.execute(sa.text("""
        SELECT COUNT(*) FROM (
            SELECT provider, request_id FROM ai_usage_events
            WHERE request_id IS NOT NULL
            GROUP BY provider, request_id HAVING COUNT(*) > 1
        ) AS duplicate_requests
    """)).scalar()
    if duplicates:
        raise RuntimeError("AI usage contains duplicate provider responses; review reconciliation before 0114.")
    table = sa.Table("ai_usage_events", sa.MetaData(),
                     sa.Column("provider", sa.String()), sa.Column("request_id", sa.String()))
    sa.Index(INDEX_NAME, table.c.provider, table.c.request_id, unique=True).create(bind)
    return "installed"
