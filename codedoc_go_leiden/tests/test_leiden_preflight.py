"""Preflight rows of codedoc_go_leiden: a missing Leiden backend stops every
analyze mode (exit 3) and shows as an error in doctor, never a silent
fallback (leiden spec §3.3, L4); bad topic settings stop the graph modes."""
from __future__ import annotations

import sys

import pytest

from graph_generator import __main__ as cli
from graph_generator import config, pipeline, topics_phase
from graph_generator.topics import community

MODES = ("graph", "wiki", "all", "doctor")


@pytest.fixture
def quiet_checks(monkeypatch):
    """Only the leiden rows: the toolchain, Spanner and Gemini checks are stubbed."""
    ok = pipeline.Check("stub", "ok", "")
    monkeypatch.setattr(pipeline, "_check_builds", lambda: ok)
    monkeypatch.setattr(pipeline, "_check_toolchain", lambda: (ok, None))
    monkeypatch.setattr(pipeline, "_check_helper", lambda tc: ok)
    monkeypatch.setattr(pipeline, "_check_memory", lambda: ok)
    monkeypatch.setattr(pipeline, "_check_spanner", lambda soft: ok)
    monkeypatch.setattr(pipeline, "_check_gemini", lambda soft: ok)


def _rows(mode: str) -> dict[str, pipeline.Check]:
    checks, _toolchain = pipeline.preflight(mode, no_spanner=True)
    return {c.name: c for c in checks if c.name in ("leiden backend", "topic settings")}


@pytest.mark.parametrize("mode", MODES)
def test_the_backend_row_is_ok_when_installed(quiet_checks, mode):
    row = _rows(mode)["leiden backend"]
    assert row.status == "ok" and "graspologic-native 1.3.1" in row.detail and "networkx" in row.detail


@pytest.mark.parametrize("mode", MODES)
def test_a_missing_backend_is_an_error_in_every_mode(quiet_checks, monkeypatch, mode):
    monkeypatch.setitem(sys.modules, "graspologic_native", None)  # import raises ImportError
    row = _rows(mode)["leiden backend"]
    assert row.status == "error" and "graspologic_native" in row.detail
    assert "requirements.txt" in row.fix and "never falls back" in row.detail


def test_a_missing_networkx_is_an_error(quiet_checks, monkeypatch):
    monkeypatch.setitem(sys.modules, "networkx", None)
    row = _rows("graph")["leiden backend"]
    assert row.status == "error" and "networkx" in row.detail


def test_an_untested_backend_version_warns(quiet_checks, monkeypatch):
    monkeypatch.setattr(community, "backend_version", lambda: "1.4.0")
    row = _rows("graph")["leiden backend"]
    assert row.status == "warn" and "1.4.0" in row.detail and "1.3.1" in row.detail


def test_analyze_exits_3_when_the_backend_is_missing(quiet_checks, monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "graspologic_native", None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze", str(tmp_path), "--wiki", "--no-spanner"])
    assert exc.value.code == 3
    assert "leiden backend" in capsys.readouterr().err


def test_doctor_shows_a_missing_backend_as_an_error(quiet_checks, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "graspologic_native", None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor"])
    assert exc.value.code == 3
    assert "leiden backend  error" in capsys.readouterr().out


def test_the_settings_row_shows_the_effective_settings(quiet_checks):
    row = _rows("graph")["topic settings"]
    assert row.status == "ok"
    assert row.detail == ("band 5-25 topics, subtopics above 12 primary atoms, profile strict, "
                          "seeds 42,43,44,45,46")


PROFILE_FORM = "strict or a '+' combination of degree_norm, calls_only, no_implements, with_tests"
SEEDS_FORM = "distinct comma-separated integers from 0 to 2^64-1"


@pytest.mark.parametrize("key, value, message", [
    ("TOPIC_SEEDS", "42,x", f"TOPIC_SEEDS must be {SEEDS_FORM}, not '42,x'"),
    ("TOPIC_SEEDS", "42,42", f"TOPIC_SEEDS must be {SEEDS_FORM}, not '42,42'"),
    ("TOPIC_SEEDS", "", f"TOPIC_SEEDS must be {SEEDS_FORM}, not ''"),
    ("TOPIC_SEEDS", "-1,43", f"TOPIC_SEEDS must be {SEEDS_FORM}, not '-1,43'"),
    ("TOPIC_SEEDS", "42,18446744073709551616", f"TOPIC_SEEDS must be {SEEDS_FORM}, not '42,18446744073709551616'"),
    ("TOPIC_PROFILE", "fancy", f"TOPIC_PROFILE must be {PROFILE_FORM}, not 'fancy'"),
    ("TOPIC_PROFILE", "strict+fancy", f"TOPIC_PROFILE must be {PROFILE_FORM}, not 'strict+fancy'"),
    ("TOPIC_MIN", "abc", "TOPIC_MIN must be an integer, not 'abc'"),
    ("TOPIC_MAX", "2.5", "TOPIC_MAX must be an integer, not '2.5'"),
    ("SUBTOPIC_MIN_ATOMS", "", "SUBTOPIC_MIN_ATOMS must be an integer, not ''"),
    ("TOPIC_MIN", "30", "TOPIC_MIN and TOPIC_MAX must be integers with 1 <= TOPIC_MIN <= TOPIC_MAX, "
                        "not '30' and '25'"),
    ("TOPIC_MIN", "0", "TOPIC_MIN and TOPIC_MAX must be integers with 1 <= TOPIC_MIN <= TOPIC_MAX, "
                       "not '0' and '25'"),
    ("SUBTOPIC_MIN_ATOMS", "0", "SUBTOPIC_MIN_ATOMS must be an integer >= 1, not '0'"),
])
def test_bad_topic_settings_are_an_error_naming_key_value_and_form(quiet_checks, monkeypatch, key, value,
                                                                     message):
    monkeypatch.setattr(config, key, value)
    row = _rows("graph")["topic settings"]
    assert (row.status, row.detail) == ("error", message)
    assert "TOPIC_MIN" in row.fix


@pytest.mark.parametrize("mode", MODES)
def test_a_bad_setting_is_an_error_in_every_mode(quiet_checks, monkeypatch, mode):
    monkeypatch.setattr(config, "TOPIC_MIN", "abc")
    assert _rows(mode)["topic settings"].status == "error"


@pytest.mark.parametrize("argv", [["analyze", "{dir}", "--graph", "--no-spanner"],
                                  ["analyze", "{dir}", "--wiki", "--no-spanner"],
                                  ["analyze", "{dir}", "--all", "--no-spanner"],
                                  ["doctor"]])
def test_a_bad_setting_exits_3_without_a_traceback(quiet_checks, monkeypatch, tmp_path, out_dir, capsys, argv):
    monkeypatch.setattr(config, "TOPIC_MAX", "many")
    with pytest.raises(SystemExit) as exc:
        cli.main([a.replace("{dir}", str(tmp_path)) for a in argv])
    out, err = capsys.readouterr()
    assert exc.value.code == 3 and "topic settings" in err
    assert "TOPIC_MAX must be an integer, not 'many'" in out and "Traceback" not in out + err


def test_topic_params_map_the_settings(monkeypatch):
    monkeypatch.setattr(config, "TOPIC_SEEDS", " 7, 8 ")
    monkeypatch.setattr(config, "TOPIC_MIN", 3)
    monkeypatch.setattr(config, "TOPIC_MAX", 9)
    monkeypatch.setattr(config, "SUBTOPIC_MIN_ATOMS", 20)
    p = topics_phase.topic_params("iryo")
    assert (p.repo, p.id_prefix, p.seeds, p.band, p.subtopic_min_atoms) == \
        ("iryo", config.ID_PREFIX, (7, 8), (3, 9), 20)
    monkeypatch.setattr(config, "TOPIC_SEEDS", "0,18446744073709551615")  # graspologic-native's u64 range
    assert topics_phase.topic_params("iryo").seeds == (0, 2 ** 64 - 1)
