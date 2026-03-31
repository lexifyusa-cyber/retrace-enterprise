"""
Database connection and session management.
"""

from typing import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# Configure engine with appropriate settings
engine_kwargs = {
    "echo": settings.DEBUG,
    "future": True,
    "pool_pre_ping": True,  # Verify connections before using
}

# For SQLite with aiosqlite, add timeout and WAL mode to prevent locks
if "sqlite" in settings.DATABASE_URL:
    engine_kwargs["connect_args"] = {"timeout": 30.0}

engine = create_async_engine(settings.DATABASE_URL, **engine_kwargs)

# Enable WAL journal mode for SQLite — allows concurrent readers + one writer
if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


async def init_db() -> None:
    """Initialize the database and create all tables, then run lightweight migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _run_migrations()


async def _run_migrations() -> None:
    """Add columns that may be missing from older schemas (idempotent)."""
    migrations = [
        ("documentation", "status", "ALTER TABLE documentation ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'draft'"),
        ("sops", "status", "ALTER TABLE sops ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'draft'"),
        ("sops", "updated_at", "ALTER TABLE sops ADD COLUMN updated_at DATETIME"),
        ("sops", "schedule_type", "ALTER TABLE sops ADD COLUMN schedule_type VARCHAR(20) DEFAULT NULL"),
        ("sops", "schedule_config", "ALTER TABLE sops ADD COLUMN schedule_config JSON DEFAULT NULL"),
        ("sops", "next_run_at", "ALTER TABLE sops ADD COLUMN next_run_at DATETIME DEFAULT NULL"),
        ("sops", "last_run_at", "ALTER TABLE sops ADD COLUMN last_run_at DATETIME DEFAULT NULL"),
        ("sops", "is_active", "ALTER TABLE sops ADD COLUMN is_active BOOLEAN DEFAULT 0"),
        ("email_connections", "client_id", "ALTER TABLE email_connections ADD COLUMN client_id VARCHAR(512) DEFAULT NULL"),
        ("email_connections", "client_secret", "ALTER TABLE email_connections ADD COLUMN client_secret TEXT DEFAULT NULL"),
        # User model expansion for multi-tenancy auth
        ("users", "tenant_id", "ALTER TABLE users ADD COLUMN tenant_id VARCHAR(36) DEFAULT NULL"),
        ("users", "display_name", "ALTER TABLE users ADD COLUMN display_name VARCHAR(255) DEFAULT NULL"),
        ("users", "auth_provider", "ALTER TABLE users ADD COLUMN auth_provider VARCHAR(20) DEFAULT 'email'"),
        ("users", "is_active", "ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1"),
        ("users", "mfa_enabled", "ALTER TABLE users ADD COLUMN mfa_enabled BOOLEAN DEFAULT 0"),
        ("users", "mfa_secret_encrypted", "ALTER TABLE users ADD COLUMN mfa_secret_encrypted VARCHAR(255) DEFAULT NULL"),
        ("users", "failed_login_count", "ALTER TABLE users ADD COLUMN failed_login_count INTEGER DEFAULT 0"),
        ("users", "locked_until", "ALTER TABLE users ADD COLUMN locked_until DATETIME DEFAULT NULL"),
        ("users", "last_login_at", "ALTER TABLE users ADD COLUMN last_login_at DATETIME DEFAULT NULL"),
        ("users", "last_login_ip", "ALTER TABLE users ADD COLUMN last_login_ip VARCHAR(45) DEFAULT NULL"),
        ("users", "invited_by", "ALTER TABLE users ADD COLUMN invited_by VARCHAR(36) DEFAULT NULL"),
        ("users", "force_password_change", "ALTER TABLE users ADD COLUMN force_password_change BOOLEAN DEFAULT 0"),
        ("users", "updated_at", "ALTER TABLE users ADD COLUMN updated_at DATETIME DEFAULT NULL"),
        # Tenant scoping
        ("products", "tenant_id", "ALTER TABLE products ADD COLUMN tenant_id VARCHAR(36) DEFAULT NULL"),
        ("products", "created_by", "ALTER TABLE products ADD COLUMN created_by VARCHAR(36) DEFAULT NULL"),
        ("email_connections", "tenant_id", "ALTER TABLE email_connections ADD COLUMN tenant_id VARCHAR(36) DEFAULT NULL"),
        ("channel_connections", "tenant_id", "ALTER TABLE channel_connections ADD COLUMN tenant_id VARCHAR(36) DEFAULT NULL"),
        ("tenants", "max_products_per_user_admin", "ALTER TABLE tenants ADD COLUMN max_products_per_user_admin INTEGER DEFAULT 10"),
        ("users", "max_products", "ALTER TABLE users ADD COLUMN max_products INTEGER DEFAULT NULL"),
        ("tenants", "ldap_require_ssl", "ALTER TABLE tenants ADD COLUMN ldap_require_ssl BOOLEAN DEFAULT 0"),
        ("tenants", "smtp_config", "ALTER TABLE tenants ADD COLUMN smtp_config JSON DEFAULT NULL"),
        ("users", "phone", "ALTER TABLE users ADD COLUMN phone VARCHAR(30) DEFAULT NULL"),
        ("users", "department", "ALTER TABLE users ADD COLUMN department VARCHAR(128) DEFAULT NULL"),
        ("users", "timezone", "ALTER TABLE users ADD COLUMN timezone VARCHAR(64) DEFAULT NULL"),
        ("users", "manager_id", "ALTER TABLE users ADD COLUMN manager_id VARCHAR(36) DEFAULT NULL"),
        ("users", "employee_id", "ALTER TABLE users ADD COLUMN employee_id VARCHAR(64) DEFAULT NULL"),
        ("users", "status", "ALTER TABLE users ADD COLUMN status VARCHAR(20) DEFAULT 'active'"),
        ("users", "password_reset_token", "ALTER TABLE users ADD COLUMN password_reset_token VARCHAR(128) DEFAULT NULL"),
        ("users", "password_reset_expires", "ALTER TABLE users ADD COLUMN password_reset_expires DATETIME DEFAULT NULL"),
        ("users", "password_changed_at", "ALTER TABLE users ADD COLUMN password_changed_at DATETIME DEFAULT NULL"),
        ("tenants", "ip_allowlist", "ALTER TABLE tenants ADD COLUMN ip_allowlist JSON DEFAULT NULL"),
        ("tenants", "ip_denylist", "ALTER TABLE tenants ADD COLUMN ip_denylist JSON DEFAULT NULL"),
        ("users", "max_managed_users", "ALTER TABLE users ADD COLUMN max_managed_users INTEGER DEFAULT NULL"),
        ("tenants", "max_managed_users_per_user_admin", "ALTER TABLE tenants ADD COLUMN max_managed_users_per_user_admin INTEGER DEFAULT 50"),
        ("tenants", "auto_provision_enabled", "ALTER TABLE tenants ADD COLUMN auto_provision_enabled BOOLEAN DEFAULT 0"),
    ]
    async with engine.begin() as conn:
        for table, column, ddl in migrations:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            cols = {row[1] for row in result.fetchall()}
            if column not in cols:
                await conn.execute(text(ddl))

        # Make product_id nullable on tables that previously required it
        nullable_migrations = [
            ("conversations", "product_id"),
            ("agent_sessions", "product_id"),
            ("sops", "product_id"),
            ("documentation", "product_id"),
            ("users", "username"),
            ("users", "hashed_password"),
        ]
        for table, column in nullable_migrations:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            rows = result.fetchall()
            col_info = next((r for r in rows if r[1] == column), None)
            if col_info and col_info[3] == 1:  # notnull flag is 1 → needs migration
                cols_meta = [(r[1], r[2], r[4]) for r in rows]  # name, type, default
                col_defs = []
                for cname, ctype, cdefault in cols_meta:
                    nullable = "" if cname == column else (" NOT NULL" if next(r for r in rows if r[1] == cname)[3] else "")
                    default = f" DEFAULT {cdefault}" if cdefault is not None else ""
                    pk = " PRIMARY KEY" if next(r for r in rows if r[1] == cname)[5] else ""
                    col_defs.append(f"{cname} {ctype}{nullable}{default}{pk}")
                col_names = ", ".join(c[0] for c in cols_meta)
                tmp = f"_{table}_tmp"
                await conn.execute(text(f"CREATE TABLE {tmp} ({', '.join(col_defs)})"))
                await conn.execute(text(f"INSERT INTO {tmp} ({col_names}) SELECT {col_names} FROM {table}"))
                await conn.execute(text(f"DROP TABLE {table}"))
                await conn.execute(text(f"ALTER TABLE {tmp} RENAME TO {table}"))

        # chunk_records: make text and embedding nullable (read from disk / ChromaDB only)
        try:
            result = await conn.execute(text("PRAGMA table_info(chunk_records)"))
            rows = result.fetchall()
        except Exception:
            rows = []
        if rows:
            text_notnull = next((r[3] for r in rows if r[1] == "text"), 0)
            emb_notnull = next((r[3] for r in rows if r[1] == "embedding"), 0)
            if text_notnull or emb_notnull:
                col_defs = []
                for r in rows:
                    cname, ctype, notnull, default, pk = r[1], r[2], r[3], r[4], r[5]
                    if cname in ("text", "embedding"):
                        notnull = 0
                    not_null_sql = " NOT NULL" if notnull else ""
                    default_sql = f" DEFAULT {default}" if default is not None else ""
                    pk_sql = " PRIMARY KEY" if pk else ""
                    col_defs.append(f'"{cname}" {ctype}{default_sql}{not_null_sql}{pk_sql}')
                col_names = ", ".join(f'"{r[1]}"' for r in rows)
                await conn.execute(text("CREATE TABLE _chunk_records_new (" + ", ".join(col_defs) + ")"))
                await conn.execute(text(f"INSERT INTO _chunk_records_new ({col_names}) SELECT {col_names} FROM chunk_records"))
                await conn.execute(text("DROP TABLE chunk_records"))
                await conn.execute(text("ALTER TABLE _chunk_records_new RENAME TO chunk_records"))


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
