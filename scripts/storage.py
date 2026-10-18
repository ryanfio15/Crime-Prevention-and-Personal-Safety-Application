"""Storage reclaim toolkit: measure, gate, compact.

The four steps around applying 012_storage_reclaim.sql, as subcommands rather
than SQL to paste. Two reasons it is a script: `psql` is not on the PATH in the
usual dev environment, and the regression gate is a multi-statement comparison
that a shell here mangles.

    python scripts/storage.py sizes             # what is actually big
    python scripts/storage.py baseline          # capture, BEFORE safety.migrate
    python scripts/storage.py gate              # prove Philadelphia did not move
    python scripts/storage.py compact           # hand freed pages back to the OS
    python scripts/storage.py history-gate      # will the full history fit?

`sizes` and `gate` are read-only. `baseline` writes one table. `compact` takes an
ACCESS EXCLUSIVE lock per table -- see its own warning.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import date
from pathlib import Path

import psycopg

# Invoked as a file path (`python scripts/storage.py`), matching
# build_chicago_crosswalk.py, so the repo root is not on sys.path the way it is
# for `python -m safety.*`. Put it there before importing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety.db import connect, wait_for_db  # noqa: E402
from safety.etl import gold  # noqa: E402

log = logging.getLogger(__name__)

# What `compact` rewrites: the whole gold schema, plus the census blocks.
#
# Discovered rather than listed, and the whole schema rather than just the tables
# the migrations delete from. Every gold table is materialized derived data
# rebuilt by delete-then-insert on each refresh -- that is what makes a refresh of
# one layer leave the others alone -- so every one of them accumulates dead tuples
# as a matter of course, migrations or not. A hand-maintained list got this wrong
# in exactly the expensive direction: it omitted gold.cell_monthly and
# gold.cell_offense_mix, 509 MB between them on a two-city deployment, neither
# touched by any migration and both rewritten on every gold run.
#
# Measured: gold.cell_activity compacted from 342 MB to 156 MB with an identical
# row count. More than half of it was dead space.
#
# silver.incident is deliberately excluded. Its partitions are upserted rather
# than rebuilt, so they bloat far more slowly, and they are large enough that
# locking one is a different order of decision. `sizes` shows them; compact them
# by hand if their dead-row counts justify it.
_RECLAIM_SQL = """
SELECT c.oid::regclass::text              AS name,
       pg_total_relation_size(c.oid)      AS bytes,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND (n.nspname = 'gold'
       OR c.oid = 'reference.census_block'::regclass)
  AND pg_total_relation_size(c.oid) > 0
ORDER BY pg_total_relation_size(c.oid) ASC
"""

_BASELINE = "public.phl_baseline"


def cmd_sizes(conn: psycopg.Connection) -> int:
    """Table sizes and dead-tuple counts, largest first."""
    with conn.cursor() as cur:
        total = cur.execute(
            "SELECT pg_size_pretty(pg_database_size(current_database())) AS s"
        ).fetchone()["s"]
        rows = cur.execute(
            """
            SELECT schemaname || '.' || relname               AS name,
                   pg_size_pretty(pg_total_relation_size(relid)) AS size,
                   pg_total_relation_size(relid)              AS bytes,
                   n_live_tup, n_dead_tup
            FROM pg_stat_user_tables
            WHERE pg_total_relation_size(relid) > 1024 * 512
            ORDER BY pg_total_relation_size(relid) DESC
            """
        ).fetchall()

    print(f"database total: {total}\n")
    print(f"{'table':<38} {'size':>10} {'live rows':>12} {'dead rows':>12}")
    print("-" * 76)
    for row in rows:
        print(
            f"{row['name']:<38} {row['size']:>10} "
            f"{row['n_live_tup']:>12,} {row['n_dead_tup']:>12,}"
        )

    bloated = [r for r in rows if r["n_dead_tup"] > max(r["n_live_tup"], 10_000)]
    if bloated:
        print(
            "\nMore dead rows than live in: "
            + ", ".join(r["name"] for r in bloated)
            + "\nEvery gold refresh is delete-then-insert, so this is expected "
            "between vacuums.\nIf the space is needed by something else, "
            "`python scripts/storage.py compact`."
        )
    return 0


def cmd_baseline(conn: psycopg.Connection) -> int:
    """Snapshot Philadelphia's safety percentiles, for the gate to compare against.

    Run BEFORE `python -m safety.migrate`. After it there is nothing left to
    compare: the migration prunes the superseded scheme's rows and the
    resolution-10 ones, which is most of the table.
    """
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {_BASELINE}")
        cur.execute(
            f"""
            CREATE TABLE {_BASELINE} AS
            SELECT h3_index, h3_res, time_window, track, scheme_version,
                   safety_percentile
            FROM gold.cell_safety WHERE source_id = 'phl'
            """
        )
        n = cur.rowcount
        by_res = cur.execute(
            f"SELECT h3_res, scheme_version, count(*) AS k FROM {_BASELINE} "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
    conn.commit()

    print(f"captured {n:,} rows into {_BASELINE}")
    for row in by_res:
        print(f"  res {row['h3_res']}  {row['scheme_version']:<20} {row['k']:>8,}")
    if not n:
        print(
            "\nNothing captured -- Philadelphia has no safety rows yet, so there is "
            "no regression to check for.\nThat is fine on a fresh database; skip the "
            "gate and go straight to `safety.migrate`."
        )
    return 0


def cmd_gate(conn: psycopg.Connection) -> int:
    """Compare the rebuilt ranking against the baseline, per PHASE2.md.

    Meaningful only after a `gold --city phl` has rebuilt the layer. Run straight
    after `safety.migrate` it passes trivially, because nothing has been
    recomputed yet.

    The row count is *expected* to fall, and by a lot: the superseded scheme and
    every resolution-10 row are gone on purpose. What must not move is the
    percentile of a row present in both.
    """
    with conn.cursor() as cur:
        if not cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS ok", (_BASELINE,)
        ).fetchone()["ok"]:
            print(
                f"no {_BASELINE} -- capture it before migrating:\n"
                "  python scripts/storage.py baseline",
                file=sys.stderr,
            )
            return 1

        summary = cur.execute(
            f"""
            SELECT count(*)                                            AS cells,
                   corr(b.safety_percentile, s.safety_percentile)      AS correlation,
                   max(abs(b.safety_percentile - s.safety_percentile)) AS max_shift
            FROM {_BASELINE} b
            JOIN gold.cell_safety s
              USING (h3_index, h3_res, time_window, track, scheme_version)
            """
        ).fetchone()
        dropped = cur.execute(
            f"""
            SELECT b.h3_res, b.scheme_version, count(*) AS k
            FROM {_BASELINE} b
            LEFT JOIN gold.cell_safety s
              USING (h3_index, h3_res, time_window, track, scheme_version)
            WHERE s.h3_index IS NULL
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()

    cells = summary["cells"]
    if not cells:
        print(
            "No overlapping rows. Either `gold --city phl` has not run since the "
            "migration, or the\nserving scheme changed -- check "
            "reference.source_registry.severity_scheme_version.",
            file=sys.stderr,
        )
        return 1

    corr = summary["correlation"]
    shift = summary["max_shift"]
    print(f"rows compared   {cells:,}")
    print(f"correlation     {corr!r}")
    print(f"max shift       {shift!r}")

    if dropped:
        print("\nintentionally gone (superseded scheme / resolution 10):")
        for row in dropped:
            print(f"  res {row['h3_res']}  {row['scheme_version']:<20} {row['k']:>8,}")

    # correlation is NULL when every compared row is identical AND there is no
    # variance to correlate -- not the case here, but guard rather than crash.
    ok = shift == 0.0 and (corr is None or abs(corr - 1.0) < 1e-12)
    print()
    if ok:
        print("PASS -- every row present in both is unchanged.")
        return 0
    print(
        "FAIL -- Philadelphia moved. PHASE2.md names the two Stage-0 changes that "
        "could reach it\n(the crosswalk fallback tier and the new NIBRS weight "
        "rows); neither should apply to phl.",
        file=sys.stderr,
    )
    return 1


def cmd_compact(conn: psycopg.Connection) -> int:
    """VACUUM FULL the gold schema and the census blocks. See _RECLAIM_SQL.

    Plain VACUUM marks freed pages reusable by the same table, which is where
    most of them want to go -- these are rebuilt by delete-then-insert. This is
    for the other case: the space is wanted by a *different* table, i.e. a new
    city's partitions, which is the whole point on a volume that is nearly full.

    Two costs, both real. It takes an ACCESS EXCLUSIVE lock, so the API's reads
    of that table block for the duration; run it in a quiet window. And it writes
    a whole new copy before dropping the old one, so it needs free disk equal to
    the *post-delete* size of the largest table here -- which is exactly the
    resource being rationed. The check below is why the sizes are printed first.

    **Smallest first**, which is the opposite of the obvious order and matters on
    a nearly-full volume. Each table rewritten frees its own bloat immediately, so
    working upwards means the largest table -- the one whose copy needs the most
    headroom -- is attempted when the most space has already been recovered.
    Largest-first attempts the riskiest rewrite at the moment free space is at its
    minimum, which is how a compaction run fails halfway and leaves the volume
    worse than it started.
    """
    with conn.cursor() as cur:
        rows = cur.execute(_RECLAIM_SQL).fetchall()
        free = cur.execute(
            """
            SELECT pg_size_pretty(sum(pg_total_relation_size(relid))) AS used,
                   sum(pg_total_relation_size(relid)) AS used_bytes
            FROM pg_stat_user_tables
            """
        ).fetchone()

    largest = max((r["bytes"] for r in rows), default=0)
    print("about to rewrite, smallest first:")
    for row in rows:
        print(f"  {row['name']:<34} {row['size']:>10}")
    print(
        f"\nlargest is {largest / 1_048_576:.0f} MB, so that much free volume is "
        "needed for its copy;\nit runs last, after the others have given their "
        f"bloat back. Currently {free['used']} in tables.\nEach table is locked "
        "in turn -- API reads of it will block."
    )

    # End the read transaction the queries above opened before touching
    # autocommit. psycopg refuses the change while a transaction is in progress
    # -- "can't change 'autocommit' now: connection in transaction status
    # INTRANS" -- and it raises before the first VACUUM, so the command reports
    # what it is about to do and then does none of it. The two other callers of
    # this pattern (migrate._vacuum, census.release_block_geometry) commit first
    # for the same reason; this one only read, so rollback is the honest verb.
    conn.rollback()

    prior = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for row in rows:
                print(f"  vacuuming {row['name']} ...", flush=True)
                cur.execute(f"VACUUM (FULL, ANALYZE) {row['name']}")
    finally:
        conn.autocommit = prior

    with conn.cursor() as cur:
        total = cur.execute(
            "SELECT pg_size_pretty(pg_database_size(current_database())) AS s"
        ).fetchone()["s"]
    print(f"\ndone. database total now {total}")
    return 0


_CITY_SILVER_SQL = """
SELECT
    r.source_id,
    r.history_start_date,
    r.history_enabled,
    COALESCE((
        SELECT sum(pg_total_relation_size(t.relid))
        FROM pg_partition_tree(to_regclass('silver.incident_' || r.source_id)) t
    ), 0)::bigint AS silver_bytes,
    COALESCE((
        SELECT sum(c.reltuples) FILTER (WHERE c.reltuples > 0)
        FROM pg_partition_tree(to_regclass('silver.incident_' || r.source_id)) t
        JOIN pg_class c ON c.oid = t.relid
        WHERE t.isleaf
    ), 0)::bigint AS silver_rows,
    (SELECT min(occurred_local_date) FROM silver.incident i WHERE i.source_id = r.source_id)
        AS oldest,
    (SELECT max(occurred_local_date) FROM silver.incident i WHERE i.source_id = r.source_id)
        AS newest,
    (SELECT sum(bronze_bytes)::double precision / NULLIF(sum(records_fetched), 0)
       FROM etl.pull_run p
      WHERE p.source_id = r.source_id AND p.status = 'succeeded'
        AND p.mode IN ('backfill', 'incremental', 'history')) AS bronze_per_record,
    (SELECT count(*) FROM gold.city_window w WHERE w.source_id = r.source_id) AS windows_now
FROM reference.source_registry r
WHERE r.enabled
ORDER BY r.source_id
"""

# The gold tables whose size grows with the number of windows. The hourly
# layer and the monthly series do not: one window, and a two-year span.
_WINDOWED_GOLD = ("gold.cell_activity", "gold.cell_safety", "gold.cell_offense_mix")

GIB = 1024**3


def cmd_history_gate(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    """Project the disk the full history needs, from what is stored now.

    Per city: silver bytes per row and rows per day measured from what the
    city already holds, extended back to registry.history_start_date; bronze
    from the bytes per record of its completed pulls; gold from its share of
    the windowed gold tables, scaled by how many windows it will have. Then
    times `--instances` (prod and dev share the disk and each holds its own
    copy), against the free space on `--path`. Read-only.

    A projection, not a measurement -- run it again after loading one year per
    city on dev, when the bytes per row are the real ones for older data.
    """
    with conn.cursor() as cur:
        cities = cur.execute(_CITY_SILVER_SQL).fetchall()
        gold_bytes = {
            name: cur.execute(
                "SELECT pg_total_relation_size(%s::regclass) AS b", (name,)
            ).fetchone()["b"]
            for name in _WINDOWED_GOLD
        }
        gold_rows = {
            r["source_id"]: r["n"]
            for r in cur.execute(
                "SELECT source_id, count(*) AS n FROM gold.cell_activity GROUP BY 1"
            ).fetchall()
        }
        db_bytes = cur.execute(
            "SELECT pg_database_size(current_database()) AS b"
        ).fetchone()["b"]
    conn.rollback()

    windowed_total = sum(gold_bytes.values())
    all_gold_rows = sum(gold_rows.values()) or 1

    print(
        f"{'city':<5} {'from':>10} {'loaded from':>11} {'add rows':>11} "
        f"{'silver':>8} {'bronze':>8} {'gold':>8} {'windows':>9}"
    )
    print("-" * 78)
    total = 0.0
    for c in cities:
        start = c["history_start_date"]
        if start is None or c["oldest"] is None or not c["silver_rows"]:
            continue
        days_held = max((c["newest"] - c["oldest"]).days, 1)
        per_day = c["silver_rows"] / days_held
        missing_days = max((c["oldest"] - start).days, 0)
        add_rows = per_day * missing_days
        silver = add_rows * (c["silver_bytes"] / c["silver_rows"])
        bronze = add_rows * (c["bronze_per_record"] or 0)

        windows_now = c["windows_now"] or 4
        windows_then = len(gold.resolve_windows(c["newest"], start))
        share = gold_rows.get(c["source_id"], 0) / all_gold_rows
        gold_add = windowed_total * share * max(windows_then / windows_now - 1, 0)

        city_total = silver + bronze + gold_add
        total += city_total
        print(
            f"{c['source_id']:<5} {start!s:>10} {c['oldest']!s:>11} {add_rows:>11,.0f} "
            f"{silver / GIB:>7.1f}G {bronze / GIB:>7.1f}G {gold_add / GIB:>7.1f}G "
            f"{windows_now:>4}->{windows_then:<4}"
            + ("" if c["history_enabled"] else "  (history not enabled)")
        )

    free = shutil.disk_usage(args.path).free
    need = total * args.instances
    after = free - need
    print("-" * 78)
    print(f"one instance needs      {total / GIB:8.1f} GB more")
    print(f"x {args.instances} instance(s)         {need / GIB:8.1f} GB")
    print(f"free on {args.path:<15} {free / GIB:8.1f} GB")
    print(f"free afterwards         {after / GIB:8.1f} GB (gate: {args.min_free_gb:g} GB)")
    print(
        f"this database now       {db_bytes / GIB:8.1f} GB; dumps grow with it, and "
        "safety-backup wants twice the last dump plus 15 GB free"
    )
    ok = after >= args.min_free_gb * GIB
    print("PASS" if ok else "FAIL: load less history, or set SAFETY_MAX_WINDOW_YEARS (counts only for long windows)")
    return 0 if ok else 1


_COMMANDS = {
    "sizes": cmd_sizes,
    "baseline": cmd_baseline,
    "gate": cmd_gate,
    "compact": cmd_compact,
    "history-gate": cmd_history_gate,
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(_COMMANDS))
    parser.add_argument(
        "--instances", type=int, default=2, help="history-gate: instances sharing the disk (2)"
    )
    parser.add_argument(
        "--path", default="/", help="history-gate: filesystem holding the database (/)"
    )
    parser.add_argument(
        "--min-free-gb", type=float, default=20.0, help="history-gate: free space to keep (20)"
    )
    args = parser.parse_args(argv)

    wait_for_db()
    with connect() as conn:
        if args.command == "history-gate":
            return cmd_history_gate(conn, args)
        return _COMMANDS[args.command](conn)


if __name__ == "__main__":
    sys.exit(main())
