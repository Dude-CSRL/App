import json
import shutil
from pathlib import Path

import yaml

from ha_watcher import report

FIX = Path(__file__).parent / "fixtures" / "report"
HUE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
KASA = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


def copy_logs(tmp_path):
    for f in ("capture.jsonl", "capture.jsonl.1"):
        shutil.copy(FIX / f, tmp_path / f)
    return str(tmp_path / "capture.jsonl")


def test_reads_rotated_dedupes_and_tolerates_malformed(tmp_path):
    rep = report.summarize(copy_logs(tmp_path), profiles_dir=str(tmp_path / "none"))
    assert rep["log_files"] == ["capture.jsonl.1", "capture.jsonl"]
    assert rep["device_count"] == 2 and rep["matched_count"] == 0
    assert len(rep["malformed_lines"]) == 3
    hue = next(d for d in rep["devices"] if d["device_id"] == HUE)
    assert hue["occurrences"] == 2
    assert hue["first_seen"].startswith("2026-10-10") and hue["last_seen"].startswith("2026-10-12")
    assert hue["manufacturer"] == "Signify Netherlands B.V." and hue["integration_domain"] == "hue"
    assert hue["suggested_area"] == "Living Room" and hue["connections"]
    assert all(d["profile_match"] is None for d in rep["devices"])


def test_profile_matching(tmp_path):
    rep = report.summarize(copy_logs(tmp_path), profiles_dir=str(FIX / "profiles"))
    by = {d["device_id"]: d for d in rep["devices"]}
    assert by[KASA]["profile_match"] == "tplink_kp115.yaml"
    assert by[HUE]["matched"] is False


def test_cli_writes_md_json_and_stubs(tmp_path, capsys):
    log = copy_logs(tmp_path)
    out, stubs = tmp_path / "out", tmp_path / "stubs"
    rc = report.main(["--log-file", log, "--out-dir", str(out),
                      "--profiles-dir", str(FIX / "profiles"), "--emit-stubs", str(stubs)])
    assert rc == 0
    data = json.loads((out / "device_report.json").read_text())
    assert data["device_count"] == 2
    md = (out / "device_report.md").read_text()
    assert HUE in md and "tplink_kp115.yaml" in md and "Malformed lines" in md
    files = list(stubs.iterdir())
    assert [f.name for f in files] == ["signify_netherlands_b_v__hue_white_and_color_ambiance_a19.yaml"]
    stub = yaml.safe_load(files[0].read_text())
    assert stub["status"] == "draft" and stub["model"] == "Hue white and color ambiance A19"
    # rerun doesn't overwrite
    files[0].write_text("edited")
    report.main(["--log-file", log, "--out-dir", str(out), "--emit-stubs", str(stubs)])
    assert files[0].read_text() == "edited"


def test_missing_log_nonzero(tmp_path):
    assert report.main(["--log-file", str(tmp_path / "nope.jsonl"), "--out-dir", str(tmp_path)]) == 1
