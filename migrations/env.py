from logging.config import fileConfig

from alembic import context
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401
from app.config import get_settings
from app.database import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().sqlalchemy_database_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def compare_type(context_, inspected_column, metadata_column, inspected_type, metadata_type):
    # SQLite stores pgvector values through its generic numeric affinity and cannot
    # reflect VECTOR(n), so suppress only that known development/test mismatch.
    if context_.dialect.name == "sqlite" and isinstance(metadata_type, VECTOR):
        return False
    return None


def include_object(object_, name, type_, reflected, compare_to):
    # HNSW indexes are managed explicitly because they are PostgreSQL/pgvector-only.
    if type_ == "index" and name in {
        "ix_robot_memories_embedding_hnsw",
        "ix_knowledge_chunks_embedding_hnsw",
    }:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=compare_type,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=compare_type,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
