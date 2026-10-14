"""Preflight stops on a Spanner schema that differs from NODE_SPEC / EDGE_SPEC
(spec §2.7, R15): missing tables, missing columns or columns of another type
stop `analyze` (exit 3) with the fix; `doctor` warns."""
from __future__ import annotations

import pytest

from graph_generator import __main__ as m
from graph_generator import config, pipeline
from graph_generator import setup_spanner_graph as s
from tests.conftest import SAMPLE_TREE, FakeSpannerDb, write_tree

MIGRATE_FIX = ("run `python -m graph_generator setup spanner` (or "
               "`python -m graph_generator.setup_spanner_graph --migrate`)")


def drifted_db(drop_tables=(), drop_columns=(), retype=None) -> FakeSpannerDb:
    """A database with the spec's schema minus some tables and columns, and
    some columns of another type."""
    retype = retype or {}
    tables = [t for t in s.NODE_TABLES + s.EDGE_TABLES if t not in drop_tables]
    columns = [[t, c, retype.get((t, c), s._TYPE_SQL[typ])]
               for t in tables for c, typ in (s.NODE_SPEC.get(t) or s.EDGE_SPEC[t])[1]
               if (t, c) not in drop_columns]
    return FakeSpannerDb(sql_rows={"INFORMATION_SCHEMA.TABLES": [[t] for t in tables],
                                   "INFORMATION_SCHEMA.COLUMNS": columns})


def _check(monkeypatch, db, soft=False):
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    return pipeline._check_spanner(soft=soft)


def test_the_spec_schema_passes(monkeypatch):
    row = _check(monkeypatch, drifted_db())
    assert (row.status, row.fix) == ("ok", "")
    assert row.detail.endswith(": schema present")


def test_missing_columns_stop_with_the_migrate_fix(monkeypatch):
    row = _check(monkeypatch, drifted_db(drop_columns={("Files", "build_included"),
                                                       ("PossiblyCalls", "open_world")}))
    assert row.status == "error"
    assert row.detail.endswith(
        ": 2 columns missing (Files.build_included, PossiblyCalls.open_world)")
    assert row.fix == f"{MIGRATE_FIX}, or add --no-spanner"


def test_missing_tables_and_columns_are_reported_together(monkeypatch):
    row = _check(monkeypatch, drifted_db(
        drop_tables=set(s.EDGE_TABLES[:6]), drop_columns={("Methods", "entry_point")}))
    assert row.status == "error"
    assert ("6 tables missing (PackageImports, FileDependsOn, ClassEmbeds, ClassImplements, "
            "MethodCalls and 1 more); 1 columns missing (Methods.entry_point)") in row.detail
    assert row.fix.startswith(MIGRATE_FIX)


def test_a_column_of_another_type_stops_with_the_recreate_fix(monkeypatch):
    row = _check(monkeypatch, drifted_db(drop_columns={("Methods", "entry_point")},
                                         retype={("Files", "generated"): "STRING(MAX)"}))
    assert row.status == "error"
    assert "1 columns of another type (Files.generated is STRING(MAX), the spec says BOOL)" \
        in row.detail and "1 columns missing (Methods.entry_point)" in row.detail
    # --migrate cannot change a type: the fix is to recreate the database
    assert "--destroy" in row.fix and "setup spanner" in row.fix and "--no-spanner" in row.fix
    assert "--migrate" not in row.fix


def test_doctor_only_warns(monkeypatch):
    db = drifted_db(retype={("Files", "generated"): "STRING(MAX)"})
    assert _check(monkeypatch, db, soft=True).status == "warn"


def test_an_unreachable_database_still_names_the_credentials(monkeypatch):
    class _Down(FakeSpannerDb):
        def snapshot(self):
            raise RuntimeError("403 permission denied")

    row = _check(monkeypatch, _Down())
    assert row.status == "error" and "403 permission denied" in row.detail
    assert "credentials" in row.fix


def test_analyze_exits_3_on_a_drifted_schema_before_any_work(tmp_path, out_dir, monkeypatch,
                                                              capsys):
    import google.auth
    repo = write_tree(tmp_path / "web", SAMPLE_TREE)
    monkeypatch.setattr(config, "PEER_REPOS", None)
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), "adc-project"))
    monkeypatch.setattr(pipeline, "_get_spanner_db",
                        lambda: drifted_db(drop_columns={("Packages", "file_count")}))
    monkeypatch.setattr(pipeline, "load_graph_state", lambda *a: pytest.fail("work started"))
    with pytest.raises(SystemExit) as exc:
        m.main(["analyze", repo, "--wiki"])
    assert exc.value.code == 3
    captured = capsys.readouterr()
    assert "1 columns missing (Packages.file_count)" in captured.out
    assert f"fix: {MIGRATE_FIX}" in captured.out
    assert "1 check(s) failed: spanner" in captured.err


class _Denied(FakeSpannerDb):
    """Every call fails as with an ADC file whose quota project was deleted."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def snapshot(self):
        raise self.error


def _denied_error(with_reason: bool):
    from google.api_core.exceptions import BadRequest
    error = BadRequest("Project 'projects/old-quota' has been deleted.")
    if with_reason:
        error = BadRequest("Project 'projects/old-quota' has been deleted.",
                           details=["reason: USER_PROJECT_DENIED"])
    return error


@pytest.mark.parametrize("error", [
    _denied_error(with_reason=True),
    RuntimeError("400 USER_PROJECT_DENIED: Project 'projects/old-quota' has been deleted"),
    RuntimeError("The spanner.googleapis.com API requires a quota project, which is not set"),
])
def test_an_unusable_quota_project_names_its_fix(monkeypatch, error):
    monkeypatch.setattr(config, "GCP_PROJECT", "my-project")
    row = _check(monkeypatch, _Denied(error))
    assert row.status == "error"
    assert "quota project" in row.detail
    assert "gcloud auth application-default set-quota-project my-project" in row.fix
    assert "GOOGLE_CLOUD_QUOTA_PROJECT=my-project" in row.fix


def test_the_quota_fix_names_a_placeholder_without_a_project(monkeypatch):
    monkeypatch.setattr(config, "GCP_PROJECT", None)
    row = _check(monkeypatch, _Denied(RuntimeError("USER_PROJECT_DENIED")))
    assert "set-quota-project <project>" in row.fix


def test_other_permission_errors_keep_the_credentials_fix(monkeypatch):
    row = _check(monkeypatch, _Denied(_denied_error(with_reason=False)))
    assert "credentials" in row.fix and "set-quota-project" not in row.fix
