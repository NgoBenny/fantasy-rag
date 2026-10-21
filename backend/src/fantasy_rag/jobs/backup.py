"""`fantasy-rag backup`: a verified dump of the local database, kept outside the repo.

The dump is taken from an exported snapshot, and the manifest (row counts per table and
checksums of key tables) is computed in that same snapshot, so a restore is checked
against exactly what was dumped, not against a database that kept ingesting. Roles are
saved too (pg_dump omits them; ours are NOLOGIN, so no passwords are involved).

`--verify-latest` restores the newest dump into a fresh, separate Postgres container
(roles first, then the dump) and compares it with its manifest: the drill for losing the
whole database container.
"""

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy.engine import make_url

from fantasy_rag.config import REPO_ROOT, Settings

IMAGE = "pgvector/pgvector:0.8.7-pg16"  # same as docker-compose.yml
KEEP_DAILY, KEEP_WEEKLY = 7, 4
# Whole-table checksums of the tables that must never drift (insert-only or identity).
CHECKSUMS = {
    "players": "id",
    "forward_runs": "id",
    "calibration_errors": "policy, rules_fp, player_id, season, week",
    "projections": "run_id, policy, scoring_key, player_id",
    "model_artifacts": "sha256",
}
# Insert-only tables whose triggers must survive a restore.
TRIGGERS = ["projections", "forward_runs", "forward_inputs", "model_artifacts",
            "calibration_errors"]  # fmt: skip


def backup_dir() -> Path:
    return Path(os.environ.get("BACKUP_DIR") or Path.home() / "fantasy-rag-backups")


def _dsn(url: str) -> dict[str, Any]:
    u = make_url(url)
    return {"host": u.host, "port": u.port, "dbname": u.database, "user": u.username,
            "password": u.password, "connect_timeout": 10}  # fmt: skip


def manifest(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """Row counts for every table and checksums of key tables, in the current snapshot."""
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        tables = [r[0] for r in cur.fetchall()]
        counts = {}
        for t in tables:
            cur.execute(f'SELECT count(*) FROM "{t}"')
            counts[t] = cur.fetchone()[0]  # type: ignore[index]
        sums = {}
        for t, order in CHECKSUMS.items():
            if t in tables:
                cur.execute(f"SELECT md5(string_agg(x::text, '|' ORDER BY {order})) FROM {t} x")
                sums[t] = cur.fetchone()[0]  # type: ignore[index]
    return {"tables": counts, "checksums": sums}


def _compose(
    *args: str, stdin: Any = None, stdout: Any = None
) -> subprocess.CompletedProcess[bytes]:
    """Run a Postgres tool inside the compose db container (its version matches the server)."""
    cmd = ["docker", "compose", "exec", "-T", "db", *args]
    return subprocess.run(cmd, cwd=REPO_ROOT, stdin=stdin, stdout=stdout,
                          stderr=subprocess.PIPE, check=True)  # fmt: skip


def _bundle(dump: Path) -> list[Path]:
    """A backup's files, in publish order: the manifest last marks it complete."""
    return [dump, dump.with_suffix(".roles.sql"), dump.with_suffix(".manifest.json")]


def _staged(p: Path) -> Path:
    return p.with_name(f".{p.name}.partial")


def complete_dumps(out_dir: Path) -> list[Path]:
    """Dumps with all three files, oldest first. Anything else is an unfinished attempt."""
    return [d for d in sorted(out_dir.glob("fantasy_*.dump")) if all(map(Path.exists, _bundle(d)))]


def run() -> Path:
    """Dump, verify, publish the complete bundle, rotate. Raises on any failure, leaving
    every earlier backup as it was and none of this attempt's files."""
    url = Settings().database_url
    db = _dsn(url)
    out_dir = backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    final = out_dir / f"fantasy_{stamp}.dump"
    files = _bundle(final)
    if any(p.exists() or _staged(p).exists() for p in files):
        raise RuntimeError(f"a backup named {final.name} already exists")
    tmp = _staged(final)
    published = False
    try:
        with psycopg.connect(**db) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]  # type: ignore[index]
            info = manifest(conn) | {"snapshot_taken_at": stamp}
            # pg_dump reads the same snapshot while this transaction stays open.
            with tmp.open("wb") as f:
                _compose("pg_dump", "-Fc", f"--snapshot={snapshot}", "-U", db["user"],
                         "-d", db["dbname"], stdout=f)  # fmt: skip
            conn.execute("COMMIT")
        if tmp.stat().st_size == 0:
            raise RuntimeError("empty dump")
        with tmp.open("rb") as f:  # a dump pg_restore can't list is no backup
            _compose("pg_restore", "--list", stdin=f, stdout=subprocess.DEVNULL)
        roles = _compose("pg_dumpall", "--roles-only", "--no-role-passwords", "-U", db["user"],
                         stdout=subprocess.PIPE).stdout  # fmt: skip
        _staged(files[1]).write_bytes(roles)
        _staged(files[2]).write_text(json.dumps(info, indent=1))
        for p in files:
            _staged(p).replace(p)
        published = True
    finally:
        for p in files:  # only this attempt's files
            _staged(p).unlink(missing_ok=True)
            if not published:
                p.unlink(missing_ok=True)
    rotate(out_dir)
    return final


def rotate(out_dir: Path) -> list[Path]:
    """Keep the newest KEEP_DAILY dumps plus the newest dump of each of KEEP_WEEKLY older
    ISO weeks. Returns what was removed (dump, roles and manifest together). Incomplete
    bundles are neither counted nor removed."""
    dumps = complete_dumps(out_dir)[::-1]
    keep = set(dumps[:KEEP_DAILY])
    weeks: set[str] = set()
    for d in dumps[KEEP_DAILY:]:
        week = datetime.strptime(d.stem.split("_")[1], "%Y%m%dT%H%M%SZ").strftime("%G-W%V")
        if week not in weeks and len(weeks) < KEEP_WEEKLY:
            weeks.add(week)
            keep.add(d)
    removed = [d for d in dumps if d not in keep]
    for d in removed:
        for f in _bundle(d):
            f.unlink(missing_ok=True)
    return removed


def verify_latest() -> dict[str, Any]:
    """Restore the newest complete dump into a fresh container and compare it with its
    manifest."""
    dumps = complete_dumps(backup_dir())
    if not dumps:
        raise RuntimeError(f"no complete backups in {backup_dir()}")
    dump = dumps[-1]
    expected = json.loads(dump.with_suffix(".manifest.json").read_text())
    db = _dsn(Settings().database_url)
    name = f"fantasy-restore-check-{os.getpid()}"
    start = time.monotonic()
    env = [f"POSTGRES_USER={db['user']}", f"POSTGRES_PASSWORD={db['password']}",
           f"POSTGRES_DB={db['dbname']}"]  # fmt: skip
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::5432",
                    *[x for e in env for x in ("-e", e)], IMAGE],
                   check=True, capture_output=True)  # fmt: skip
    try:
        for _ in range(60):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", db["user"]],
                                   capture_output=True)  # fmt: skip
            if ready.returncode == 0:
                break
            time.sleep(1)
        time.sleep(2)  # the image restarts Postgres once after init
        roles = dump.with_suffix(".roles.sql").read_bytes()
        # Roles first (the owner role already exists in a fresh container; that line errors
        # harmlessly), then the dump, whose objects reference them.
        subprocess.run(["docker", "exec", "-i", name, "psql", "-U", db["user"], "-d", db["dbname"],
                        "-q"], input=roles, capture_output=True, check=True)  # fmt: skip
        with dump.open("rb") as f:
            subprocess.run(["docker", "exec", "-i", name, "pg_restore", "-U", db["user"],
                            "-d", db["dbname"], "--exit-on-error"],
                           stdin=f, capture_output=True, check=True)  # fmt: skip
        port = subprocess.run(["docker", "port", name, "5432/tcp"], capture_output=True,
                              text=True, check=True).stdout.strip().rsplit(":", 1)[1]  # fmt: skip
        with psycopg.connect(**(db | {"host": "127.0.0.1", "port": int(port)})) as conn:
            got = manifest(conn)
            problems = [
                f"{t}: {got['tables'].get(t)} rows, dump had {n}"
                for t, n in expected["tables"].items()
                if got["tables"].get(t) != n
            ]
            problems += [f"{t}: checksum differs" for t, s in expected["checksums"].items()
                         if got["checksums"].get(t) != s]  # fmt: skip
            problems += _structure_problems(conn)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return {"dump": str(dump), "tables": len(expected["tables"]), "problems": problems,
            "seconds": round(time.monotonic() - start, 1)}  # fmt: skip


def _structure_problems(conn: psycopg.Connection[Any]) -> list[str]:
    """Roles, grants, insert-only triggers and extensions survived the restore."""
    checks = {
        "fantasy_reader can SELECT players":
            "SELECT has_table_privilege('fantasy_reader', 'players', 'SELECT')",
        "fantasy_reader cannot INSERT players":
            "SELECT NOT has_table_privilege('fantasy_reader', 'players', 'INSERT')",
        "fantasy_logger can INSERT query_log":
            "SELECT has_table_privilege('fantasy_logger', 'query_log', 'INSERT')",
        "fantasy_logger cannot SELECT query_log":
            "SELECT NOT has_table_privilege('fantasy_logger', 'query_log', 'SELECT')",
        "extensions vector and pg_trgm":
            "SELECT count(*) = 2 FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')",
    }  # fmt: skip
    for table in TRIGGERS:
        checks[f"{table} is insert-only"] = (
            "SELECT count(*) = 1 FROM pg_trigger WHERE NOT tgisinternal"
            f" AND tgrelid = '{table}'::regclass"
        )
    problems = []
    for label, sql in checks.items():
        if not conn.execute(sql).fetchone()[0]:  # type: ignore[index]
            problems.append(f"failed: {label}")
    return problems
