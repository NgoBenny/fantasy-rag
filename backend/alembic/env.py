from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from fantasy_rag.config import Settings

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

# Migrations are hand-written; there is no ORM metadata to autogenerate from.
url = context.config.get_main_option("sqlalchemy.url") or Settings().database_url

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url).connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
