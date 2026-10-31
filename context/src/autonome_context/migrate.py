"""Applies migrations/NNNN_*.sql in order. Applied files are immutable; fix forward."""

from importlib import resources

import psycopg


def migrate(database_url: str) -> list[str]:
    files = sorted(
        f for f in resources.files("autonome_context.migrations").iterdir() if f.name.endswith(".sql")
    )
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "create table if not exists schema_migration ("
            " name text primary key, applied_at timestamptz not null default now())"
        )
        done = {row[0] for row in conn.execute("select name from schema_migration")}
        for f in files:
            if f.name in done:
                continue
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute("insert into schema_migration (name) values (%s)", (f.name,))
            applied.append(f.name)
    return applied
