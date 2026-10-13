"""The Postgres migrations build the schema docs/ingestion/architecture.md defines (EMBER-61).

Needs psql, dbmate, and a Postgres server it may create and drop databases on:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
        python3 infra/k8s/apps/postgres/test_migrations.py

It builds the doc's SQL in database ember_test_doc and the migrations in ember_test_migrations.
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
DOC = REPO / "docs" / "ingestion" / "architecture.md"
MIGRATIONS = HERE / "migrations"
DBMATE = os.environ.get("DBMATE", "dbmate")

DOC_DB = "ember_test_doc"
MIGRATIONS_DB = "ember_test_migrations"

# Tables and the delivery key EMBER-61's acceptance criteria name
REQUIRED_TABLES = {"raw_events", "source_records", "episodes", "cursors", "installations", "rate_limits"}
DELIVERY_KEY = "UNIQUE (source, delivery_id)"

# What has to match between the two databases. dbmate's own table is left out.
CATALOG = """
select 'column', c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod),
       a.attnotnull, a.attidentity, pg_get_expr(d.adbin, d.adrelid)
  from pg_attribute a
  join pg_class c on c.oid = a.attrelid
  left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
 where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
   and c.relname <> 'schema_migrations' and a.attnum > 0 and not a.attisdropped
 order by 2, 3;
select 'constraint', conrelid::regclass, conname, pg_get_constraintdef(oid)
  from pg_constraint
 where connamespace = 'public'::regnamespace and conrelid::regclass::text <> 'schema_migrations'
 order by 2, 3;
select 'index', tablename, indexname, indexdef
  from pg_indexes
 where schemaname = 'public' and tablename <> 'schema_migrations'
 order by 2, 3;
"""


def database_url(name: str) -> str:
    parts = urlsplit(os.environ["PGTEST_URL"])
    return urlunsplit(parts._replace(path="/" + name))


def psql(db: str, sql: str) -> str:
    result = subprocess.run(
        ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", database_url(db), "-f", "-"],
        input=sql, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"psql on {db} failed:\n{result.stderr}")
    return result.stdout


def dbmate(*args: str) -> None:
    result = subprocess.run(
        [DBMATE, "--url", database_url(MIGRATIONS_DB), "--migrations-dir", str(MIGRATIONS),
         "--no-dump-schema", *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"dbmate {' '.join(args)} failed:\n{result.stdout}{result.stderr}")


def doc_sql() -> str:
    """The doc's Storage SQL, without the daily retention delete, which is not schema."""
    storage = DOC.read_text().split("\n## Storage\n", 1)[1]
    sql = re.search(r"```sql\n(.*?)```", storage, re.S).group(1)
    return re.sub(r"^delete from .*?;$", "", sql, flags=re.M | re.S)


def catalog(db: str) -> list[str]:
    return psql(db, CATALOG).splitlines()


def tables(db: str) -> set[str]:
    out = psql(db, "select tablename from pg_tables where schemaname = 'public';")
    return set(out.split()) - {"schema_migrations"}


class MigrationsTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        for db in (DOC_DB, MIGRATIONS_DB):
            psql("postgres", f"drop database if exists {db}; create database {db};")
        psql(DOC_DB, doc_sql())

    @classmethod
    def tearDownClass(cls):
        psql("postgres", f"drop database if exists {DOC_DB}; drop database if exists {MIGRATIONS_DB};")

    def test_every_migration_is_shipped_to_the_job(self):
        kustomization = (HERE / "kustomization.yaml").read_text()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            self.assertIn(f"- migrations/{path.name}\n", kustomization,
                          f"{path.name} is not in kustomization.yaml, so the migrate Job won't see it")

    def test_migrations_apply_roll_back_and_match_the_doc(self):
        dbmate("migrate")
        self.assertEqual(catalog(MIGRATIONS_DB), catalog(DOC_DB),
                         "the migrations and docs/ingestion/architecture.md build different schemas")

        for _ in MIGRATIONS.glob("*.sql"):
            dbmate("rollback")
        self.assertEqual(tables(MIGRATIONS_DB), set(), "rolling back every migration left tables behind")

        dbmate("migrate")
        self.assertEqual(catalog(MIGRATIONS_DB), catalog(DOC_DB),
                         "migrating again after a full rollback built a different schema")

    def test_doc_has_the_required_tables_and_delivery_key(self):
        self.assertTrue(REQUIRED_TABLES <= tables(DOC_DB), REQUIRED_TABLES - tables(DOC_DB))
        keys = psql(DOC_DB, "select pg_get_constraintdef(oid) from pg_constraint"
                            " where conrelid = 'raw_events'::regclass and contype = 'u';")
        self.assertIn(DELIVERY_KEY, keys.splitlines())


if __name__ == "__main__":
    unittest.main()
