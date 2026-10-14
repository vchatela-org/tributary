"""scripts/whoop-counts.py: the counts proof of the move from Whoops
(docs/connectors/whoop.md). Counts only, on both sides."""
import importlib.util
import io
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "whoop-counts.py"
spec = importlib.util.spec_from_file_location("whoop_counts", SCRIPT)
whoop_counts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(whoop_counts)

DAY = 86400
EPOCH_2026_10_01 = 1790812800


def test_the_sql_counts_rows_and_reads_no_value():
    sql = whoop_counts.sql()
    selects = re.findall(r"SELECT (.+?) FROM", sql)
    assert len(selects) == 5
    for columns in selects:
        # A table's name, a day, a count: nothing of a record.
        assert columns.endswith("count(*)"), columns
    assert "whoop_export_journal" not in whoop_counts.sql(("sleep",))
    assert sql.rstrip().endswith("ORDER BY 1, 2;")


def test_influx_is_asked_for_counts_per_day_of_whoop_s_points():
    statement = whoop_counts.influx_statement("recovery", "o'neil", "2025-03-01")
    assert statement == ('SELECT count("recovery_score") FROM "sleep_session" WHERE "source" = \'whoop\' AND '
                         "time >= '2025-03-01T00:00:00Z' AND \"user\" = 'o\\'neil' GROUP BY time(1d) fill(none)")


def test_compare_day_by_day():
    postgres = whoop_counts.read_postgres(io.StringIO(
        "sleep,2026-10-01,1\nsleep,2026-10-02,2\nsleep,2026-10-03,1\ncycle,2026-10-01,1\nnot a row\n"))

    def fetch(url, database, statement):
        assert url == "http://influx.example:8086" and "count(" in statement
        if '"sleep_session"' in statement and "score" in statement and "recovery" not in statement:
            values = [[EPOCH_2026_10_01, 1], [EPOCH_2026_10_01 + DAY, 1], [EPOCH_2026_10_01 + 3 * DAY, 1]]
            return {"results": [{"series": [{"name": "sleep_session", "values": values}]}]}
        if '"daily_activity"' in statement:
            return {"results": [{"series": [{"values": [[EPOCH_2026_10_01, 1]]}]}]}
        return {"results": [{"error": "database not found: SleepStats"}]}

    influx = whoop_counts.influx_counts("http://influx.example:8086", None, "2015-01-01", fetch=fetch)
    out = io.StringIO()
    assert whoop_counts.compare(postgres, influx, "2015-01-01", show_days=True, out=out) is False
    text = out.getvalue()
    assert ("sleep     PostgreSQL 4 on 3 day(s), InfluxDB 3 on 3 day(s): 1 equal, 1 differing, 1 only in InfluxDB, "
            "1 only in PostgreSQL") in text
    assert "2026-10-02: PostgreSQL 2, InfluxDB 1" in text and "2026-10-03: PostgreSQL 1, InfluxDB 0" in text
    assert "cycle     PostgreSQL 1 on 1 day(s), InfluxDB 1 on 1 day(s): 1 equal" in text
    assert text.rstrip().endswith("DIFFERENT")

    out = io.StringIO()
    assert whoop_counts.compare({"cycle": {"2026-10-01": 1}}, influx, "2015-01-01", tables=("cycle",), out=out)
    assert out.getvalue().rstrip().endswith("EQUAL")
