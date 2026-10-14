"""`validate` derives its tables and orphan checks from the schema spec (spec §2.4, §6)."""

import re
import types

from graph_generator import __main__ as m
from graph_generator import setup_spanner_graph as s
from tests.conftest import FakeSpannerDb


class _RecordingDb(FakeSpannerDb):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.sql: list[str] = []

    def sql_result(self, sql, params):
        self.sql.append(sql)
        return super().sql_result(sql, params)


def test_validate_queries_only_spec_tables(monkeypatch, capsys):
    db = _RecordingDb(sql_rows={
        "GROUP BY source_repo": [["web", "shared", 4]],
        "GROUP BY repo": [["web", 10], ["api", 7]],
        "COUNT(*)": [[0]],
    })
    monkeypatch.setattr(m, "_get_spanner_db", lambda: db)
    m.cmd_validate(types.SimpleNamespace())
    out = capsys.readouterr().out

    known = set(s.NODE_TABLES) | set(s.EDGE_TABLES)
    referenced = {t for q in db.sql for t in re.findall(r"\bFROM (\w+)", q)}
    assert referenced == known
    for label, _query in s.orphan_checks():
        assert f"{label}" in out
    assert out.count(" OK") == len(s.EDGE_TABLES)
    assert "web=10" in out and "api=7" in out
    assert "web → shared :  CrossRepoCalls=4, CrossRepoImports=4, CrossRepoRef=4" in out
    assert len(s.NODE_TABLES) == 6 and len(s.EDGE_TABLES) == 14
    for table in ("Packages", "PackageImports", "ClassEmbeds", "ClassImplements",
                  "PackageContainsFile", "CrossRepoImports"):
        assert table in out
    assert "Modules" not in out and "FileImports" not in out
    assert "Project:" in out


def test_validate_reports_orphans_and_empty_coupling(monkeypatch, capsys):
    db = FakeSpannerDb(sql_rows={"NOT EXISTS": [[2]], "GROUP BY": [], "COUNT(*)": [[1]]})
    monkeypatch.setattr(m, "_get_spanner_db", lambda: db)
    m.cmd_validate(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert out.count("WARNING: 2 orphans") == len(s.EDGE_TABLES)
    assert "(none — run `crossref` after ingesting 2+ repos)" in out
