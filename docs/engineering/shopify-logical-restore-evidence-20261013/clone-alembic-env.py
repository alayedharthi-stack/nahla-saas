"""Clone-only Alembic environment; revisions remain unchanged from a8fb."""
from alembic import context
config=context.config
connection=config.attributes['connection']
context.configure(connection=connection,target_metadata=None,on_version_apply=config.attributes['on_version_apply'])
with context.begin_transaction():
    context.run_migrations()
