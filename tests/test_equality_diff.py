"""scripts/equality-diff.py: the check a port runs before replacing a job
(docs/porting.md)."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "equality-diff.py"
spec = importlib.util.spec_from_file_location("equality_diff", SCRIPT)
equality_diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(equality_diff)

LINES = """# BodyStats
body_composition,scale=QN\\ Scale,source=scale,user=private-user weight_kg=70.5,bmi=22.0 1000
body_composition,source=scale,user=private-user weight_kg=71.0 2000
body_thresholds,user=private-user bmi_t1=18.5 9000
"""


def stored(rows=None, thresholds_time=5000, weight_type="float"):
    rows = rows if rows is not None else {"QN Scale": [[1000, 22, 70.5]], "": [[2000, None, 71.0]]}
    series = [{"name": "body_composition", "tags": {"scale": scale, "source": "scale", "user": "private-user"},
               "columns": ["time", "bmi", "weight_kg"], "values": values} for scale, values in rows.items()]
    series.append({"name": "body_thresholds", "tags": {"user": "private-user"}, "columns": ["time", "bmi_t1"],
                   "values": [[thresholds_time - 100, 17.0], [thresholds_time, 18.5]]})
    data = {"results": [{"statement_id": 0, "series": series}]}
    types = {"results": [{"series": [
        {"name": "body_composition", "columns": ["fieldKey", "fieldType"],
         "values": [["bmi", "float"], ["weight_kg", weight_type]]},
        {"name": "body_thresholds", "columns": ["fieldKey", "fieldType"], "values": [["bmi_t1", "float"]]}]}]}
    return data, types


def run(tmp_path, capsys, data, types, *args):
    (tmp_path / "lines.lp").write_text(LINES)
    (tmp_path / "influx.json").write_text(json.dumps(data))
    (tmp_path / "types.json").write_text(json.dumps(types))
    code = equality_diff.main([str(tmp_path / "lines.lp"), str(tmp_path / "influx.json"),
                               "--types", str(tmp_path / "types.json"), "--latest", "body_thresholds", *args])
    return code, capsys.readouterr().out


def test_equal(tmp_path, capsys):
    code, out = run(tmp_path, capsys, *stored())
    assert code == 0 and out.endswith("EQUAL\n")
    assert "body_composition: 2 point(s) in the dry run, 2 in InfluxDB; 2 equal, 0 differing" in out
    assert "every field written has the type InfluxDB holds" in out


def test_a_different_value_names_the_field_never_the_value(tmp_path, capsys):
    code, out = run(tmp_path, capsys, *stored({"QN Scale": [[1000, 22, 70.6]], "": [[2000, None, 71.0]]}))
    assert code == 1 and "1 differing" in out and "differing fields: {'weight_kg': 1}" in out
    assert "70.6" not in out and "QN" not in out and "private-user" not in out


def test_missing_and_extra_points_and_tags(tmp_path, capsys):
    # The second weigh-in stored with a tag the dry run doesn't write: another series.
    data, types = stored({"QN Scale": [[1000, 22, 70.5]], "Other": [[2000, None, 71.0]]})
    code, out = run(tmp_path, capsys, data, types)
    assert code == 1 and "1 only in the dry run, 1 only in InfluxDB" in out


def test_a_type_change_is_a_difference(tmp_path, capsys):
    code, out = run(tmp_path, capsys, *stored(weight_type="integer"))
    assert code == 1 and "weight_kg (float, InfluxDB holds int)" in out


def test_since_and_latest(tmp_path, capsys):
    # Only the newest boundaries point counts, whatever its time.
    code, out = run(tmp_path, capsys, *stored({"QN Scale": [[1000, 22, 99.0]], "": [[2000, None, 71.0]]}),
                    "--since", "1500")
    assert code == 0 and "body_composition: 1 point(s) in the dry run, 1 in InfluxDB" in out


@pytest.mark.parametrize("line", ['m,t=a\\ b f="x y",g=1i 10', 'm f=t 10', 'm f=1e+06 10'])
def test_line_protocol_parsing(tmp_path, line):
    (tmp_path / "l.lp").write_text(line + "\n")
    (key, fields), = equality_diff.parse_lines(str(tmp_path / "l.lp")).items()
    assert key[2] == 10 and all(kind in (str, int, bool, float) for _v, kind in fields.values())
