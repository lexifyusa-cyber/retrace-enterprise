# FastAPI SQLite to PostgreSQL Migration

Scope: `main-app/backend` FastAPI application database only. OpenCode internal SQLite and pod-agent SQLite are intentionally excluded. ChromaDB remains filesystem-backed.

## Local PostgreSQL

Preferred path when Docker Compose v2 is available:

```bash
docker compose up -d postgres
cd main-app/backend
source .venv/bin/activate
export DATABASE_URL=postgresql+asyncpg://retrace:retrace_dev_password@localhost:5432/retrace
alembic upgrade head
```

If `docker compose` is routed through the old Python `docker-compose` v1 wrapper and fails with `ModuleNotFoundError: distutils`, use Podman directly or install Compose v2. Podman fallback:

```bash
podman volume create retrace_postgres_data
podman run -d --name retrace-postgres \
  -e POSTGRES_DB=retrace \
  -e POSTGRES_USER=retrace \
  -e POSTGRES_PASSWORD=retrace_dev_password \
  -p 5432:5432 \
  -v retrace_postgres_data:/var/lib/postgresql/data \
  postgres:16-alpine
```

## Copy SQLite Data

Use a copy of the SQLite file first. Do not point the script at a live DB that is accepting writes.

```bash
cd main-app/backend
cp retrace.db /tmp/retrace-migration-test.db
export DATABASE_URL=postgresql+asyncpg://retrace:retrace_dev_password@localhost:5432/retrace
python scripts/migrate_sqlite_to_postgres.py --sqlite /tmp/retrace-migration-test.db --truncate
python scripts/validate_sqlite_postgres.py --sqlite /tmp/retrace-migration-test.db
```

## App Smoke Test

Start the backend with the PostgreSQL URL and verify:

- login/auth and refresh sessions
- team listing for each role
- settings save/load
- conversations and messages
- MCP extensions
- TeamKnowParse KB reads
- Chroma lookup with `TEAMKNOWPARSE_CHROMA_DIR` set appropriately

## Fly Production v1

Production app DB: Fly Postgres in `iad`.
Production ChromaDB path: `/data/chroma_teamknowparse`.

```bash
fly postgres create --region iad
fly postgres attach <postgres-app-name> -a retrace-dev
fly secrets set -a retrace-dev TEAMKNOWPARSE_CHROMA_DIR=/data/chroma_teamknowparse
```

Run schema migrations against the production `DATABASE_URL` before cutover:

```bash
cd main-app/backend
DATABASE_URL='<fly-postgres-url>' alembic upgrade head
```

Cutover sequence:

1. Stop writes / enter brief maintenance window.
2. Backup SQLite DB and `/data/chroma_teamknowparse`.
3. Run Alembic against Fly Postgres.
4. Run SQLite-to-Postgres data copy.
5. Run validation.
6. Start app with PostgreSQL `DATABASE_URL`.
7. Smoke test before reopening traffic.

Rollback before reopening traffic: point `DATABASE_URL` back to the SQLite backup and redeploy. After PostgreSQL accepts writes, rollback requires manual reconciliation.
