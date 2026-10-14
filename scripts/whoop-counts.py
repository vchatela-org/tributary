#!/usr/bin/env python3
"""Counts proof for the move from Whoops to Tributary's Whoop connector
(docs/connectors/whoop.md, "Coming from Whoops"): does InfluxDB hold, day by
day, as many WHOOP records as Whoops' PostgreSQL does?

    whoop-counts.py sql [--without-journal]
        prints the PostgreSQL query to run on Whoops' database: records per UTC
        day of each table, as CSV (`psql -At -F, -f counts.sql`).

    whoop-counts.py compare POSTGRES_CSV --influx-url URL [--user USER]
                    [--since YYYY-MM-DD] [--show-days]
        reads that CSV, asks InfluxDB for the points per UTC day of the same
        records (source=whoop), and compares them.

Counts only, on both sides: no value is ever read, printed or kept, only how
many records a day holds (--show-days lists the differing days and their
counts). Exit status 1 when a day differs or is in PostgreSQL only.

What is compared, per UTC day:
  sleep     whoop_sleep (by its end)        sleep_session points with a `score`
  recovery  whoop_recovery (its sleep's end) sleep_session points with a
                                            `recovery_score`
  cycle     whoop_cycle (by its start)      daily_activity points with a `strain`
  workout   whoop_workout (by its start)    exercise points (source=whoop)
  journal   whoop_export_journal (by its    journal points
            cycle start)
A day in InfluxDB only is expected where Whoops never stored the record (an
unscored sleep, today's open cycle, history only an export holds); a day in
PostgreSQL only, or with fewer points, is a record to look at.

Standard library only, and not part of the image: run it on your own machine.
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import urlopen

TABLES = ("sleep", "recovery", "cycle", "workout", "journal")

SQL = {
    "sleep": "SELECT 'sleep', to_char((\"end\" AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD'), count(*) "
             "FROM whoop_sleep GROUP BY 2",
    "recovery": "SELECT 'recovery', to_char((s.\"end\" AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD'), count(*) "
                "FROM whoop_recovery r JOIN whoop_sleep s ON s.id = r.sleep_id GROUP BY 2",
    "cycle": "SELECT 'cycle', to_char((start AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD'), count(*) "
             "FROM whoop_cycle GROUP BY 2",
    "workout": "SELECT 'workout', to_char((start AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD'), count(*) "
               "FROM whoop_workout GROUP BY 2",
    "journal": "SELECT 'journal', to_char((cycle_start AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD'), count(*) "
               "FROM whoop_export_journal GROUP BY 2",
}

# (database, measurement, the field a record of this table always has there)
INFLUX = {
    "sleep": ("SleepStats", "sleep_session", "score"),
    "recovery": ("SleepStats", "sleep_session", "recovery_score"),
    "cycle": ("BodyStats", "daily_activity", "strain"),
    "workout": ("BodyStats", "exercise", "activity"),
    "journal": ("SleepStats", "journal", "count"),
}


def sql(tables=TABLES) -> str:
    return ("-- Whoops' records per UTC day: counts only, never a value.\n"
            + "\nUNION ALL\n".join(SQL[t] for t in tables) + "\nORDER BY 1, 2;\n")


def read_postgres(lines) -> dict:
    """{table: {day: count}} from psql's `table,day,count` lines."""
    counts = defaultdict(dict)
    for row in csv.reader(lines):
        if len(row) != 3 or row[0] not in TABLES:
            continue
        counts[row[0]][row[1]] = int(row[2])
    return dict(counts)


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def influx_statement(table: str, user, since: str) -> str:
    _database, measurement, field = INFLUX[table]
    where = f"\"source\" = 'whoop' AND time >= '{since}T00:00:00Z'"
    if user:
        where += f' AND "user" = {_quote(user)}'
    return f'SELECT count("{field}") FROM "{measurement}" WHERE {where} GROUP BY time(1d) fill(none)'


def _fetch(url: str, database: str, statement: str) -> dict:
    query = urlencode({"db": database, "q": statement, "epoch": "s"})
    with urlopen(f"{url.rstrip('/')}/query?{query}", timeout=60) as response:
        return json.load(response)


def influx_counts(url: str, user, since: str, tables=TABLES, fetch=_fetch) -> dict:
    """{table: {day: count}}, from InfluxDB's counts per UTC day."""
    counts = {}
    for table in tables:
        database = INFLUX[table][0]
        payload = fetch(url, database, influx_statement(table, user, since))
        days = {}
        for result in payload.get("results") or []:
            if result.get("error"):
                if "database not found" in result["error"]:
                    continue
                raise SystemExit(f"InfluxDB: {result['error']}")
            for series in result.get("series") or []:
                for at, count in series.get("values") or []:
                    if count:
                        day = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d")
                        days[day] = days.get(day, 0) + int(count)
        counts[table] = days
    return counts


def compare(postgres: dict, influx: dict, since: str, tables=TABLES, show_days=False, out=sys.stdout) -> bool:
    """Prints the comparison; True when no day differs or is in PostgreSQL only."""
    equal_everywhere = True
    for table in tables:
        pg = {day: n for day, n in (postgres.get(table) or {}).items() if day >= since}
        ix = influx.get(table) or {}
        same = [d for d in pg if ix.get(d) == pg[d]]
        differ = sorted(d for d in pg if d in ix and ix[d] != pg[d])
        pg_only = sorted(d for d in pg if d not in ix)
        ix_only = sorted(d for d in ix if d not in pg)
        print(f"{table:9} PostgreSQL {sum(pg.values())} on {len(pg)} day(s), InfluxDB {sum(ix.values())} on "
              f"{len(ix)} day(s): {len(same)} equal, {len(differ)} differing, {len(ix_only)} only in InfluxDB, "
              f"{len(pg_only)} only in PostgreSQL", file=out)
        if show_days:
            for day in differ:
                print(f"    {day}: PostgreSQL {pg[day]}, InfluxDB {ix[day]}", file=out)
            for day in pg_only:
                print(f"    {day}: PostgreSQL {pg[day]}, InfluxDB 0", file=out)
            for day in ix_only:
                print(f"    {day}: PostgreSQL 0, InfluxDB {ix[day]}", file=out)
        if differ or pg_only:
            equal_everywhere = False
    print("EQUAL" if equal_everywhere else "DIFFERENT", file=out)
    return equal_everywhere


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    print_sql = sub.add_parser("sql", help="print the PostgreSQL query (counts per day)")
    print_sql.add_argument("--without-journal", action="store_true",
                           help="Whoops without the export import has no whoop_export_journal")
    check = sub.add_parser("compare", help="compare psql's counts with InfluxDB's")
    check.add_argument("postgres_csv")
    check.add_argument("--influx-url", required=True)
    check.add_argument("--user", help="the `user` tag Tributary writes (TRIBUTARY_INFLUXDB_USER)")
    check.add_argument("--since", default="2015-01-01", help="first UTC day compared (YYYY-MM-DD)")
    check.add_argument("--without-journal", action="store_true")
    check.add_argument("--show-days", action="store_true", help="list the days that differ, with their counts")
    args = parser.parse_args(argv)
    tables = tuple(t for t in TABLES if not (args.without_journal and t == "journal"))
    if args.command == "sql":
        sys.stdout.write(sql(tables))
        return 0
    with open(args.postgres_csv, encoding="utf-8", newline="") as handle:
        postgres = read_postgres(handle)
    influx = influx_counts(args.influx_url, args.user, args.since, tables)
    return 0 if compare(postgres, influx, args.since, tables, args.show_days) else 1


if __name__ == "__main__":
    sys.exit(main())
