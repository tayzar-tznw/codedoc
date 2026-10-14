"""Schema spec (spec §6.1, §6.2), DDL, property graph, idempotent migration and
--verify (fake admin API)."""

import types

import pytest

from graph_generator import config
from graph_generator import setup_spanner_graph as s
from tests import go_schema_spec as go

NODE_COLUMNS = {
    "Files": ["file_id", "file_name", "extension", "directory", "path", "origin", "generated",
              "package", "package_name", "module", "build_constraint", "build_included",
              "summary", "repo"],
    "Packages": ["package_id", "import_path", "name", "directory", "module", "origin",
                 "file_count", "summary", "repo"],
    "Classes": ["class_id", "name", "qualname", "package", "fqcn", "file_id", "kind",
                "type_params", "embeds", "start_line", "end_line", "origin", "module", "summary",
                "repo"],
    "Methods": ["method_id", "name", "class_id", "file_id", "qualname", "fqmn", "signature",
                "member_kind", "receiver", "type_params", "has_body", "start_line", "end_line",
                "origin", "module", "entry_point", "summary", "repo"],
    "Topics": ["topic_id", "name", "summary", "repo"],
    "Directories": ["dir_id", "name", "summary", "repo"],
}
EDGE_COLUMNS = {
    "PackageImports": ["edge_id", "source_file", "target_package", "import_path", "alias", "kind"],
    "FileDependsOn": ["edge_id", "source_file", "target_file", "resolution"],
    "ClassEmbeds": ["edge_id", "child_class", "embedded_class", "pointer", "resolution"],
    "ClassImplements": ["edge_id", "class_id", "interface_id", "pointer_receiver", "resolution"],
    "MethodCalls": ["edge_id", "caller_method", "callee_method", "callee_name", "resolution",
                    "site_kind", "call_line"],
    "PossiblyCalls": ["edge_id", "caller_method", "callee_method", "callee_name", "reason",
                      "candidate_count", "open_world"],
    "FileDefinesClass": ["edge_id", "file_id", "class_id"],
    "ClassDefinesMethod": ["edge_id", "class_id", "method_id"],
    "DirContainsFile": ["edge_id", "dir_id", "file_id"],
    "PackageContainsFile": ["edge_id", "package_id", "file_id"],
    "FileBelongsToTopic": ["edge_id", "file_id", "topic_id"],
    "CrossRepoCalls": ["edge_id", "caller_method", "callee_method", "source_repo", "target_repo",
                       "symbol", "site_kind"],
    "CrossRepoRef": ["edge_id", "source_class", "target_class", "source_repo", "target_repo",
                     "symbol", "kind"],
    "CrossRepoImports": ["edge_id", "source_file", "target_package", "source_repo",
                         "target_repo", "import_path"],
}
ENDS = {
    "PackageImports": ("Files", "Packages"), "FileDependsOn": ("Files", "Files"),
    "ClassEmbeds": ("Classes", "Classes"), "ClassImplements": ("Classes", "Classes"),
    "MethodCalls": ("Methods", "Methods"), "PossiblyCalls": ("Methods", "Methods"),
    "FileDefinesClass": ("Files", "Classes"), "ClassDefinesMethod": ("Classes", "Methods"),
    "DirContainsFile": ("Directories", "Files"), "PackageContainsFile": ("Packages", "Files"),
    "FileBelongsToTopic": ("Files", "Topics"), "CrossRepoCalls": ("Methods", "Methods"),
    "CrossRepoRef": ("Classes", "Classes"), "CrossRepoImports": ("Files", "Packages"),
}
BOOL_COLUMNS = {("Files", "generated"), ("Files", "build_included"), ("Methods", "has_body"),
                ("ClassEmbeds", "pointer"), ("ClassImplements", "pointer_receiver"),
                ("PossiblyCalls", "open_world")}
INT_COLUMNS = {("Packages", "file_count"), ("Classes", "start_line"), ("Classes", "end_line"),
               ("Methods", "start_line"), ("Methods", "end_line"), ("MethodCalls", "call_line"),
               ("PossiblyCalls", "candidate_count")}


def _columns_of(typ: str) -> set[tuple[str, str]]:
    return {(t, c) for t, (_pk, cols, *_rest) in {**s.NODE_SPEC, **s.EDGE_SPEC}.items()
            for c, ct in cols if ct == typ}


@pytest.mark.parametrize("table", sorted(NODE_COLUMNS))
def test_node_tables_match_spec(table):
    assert s.write_columns(table) == NODE_COLUMNS[table]
    assert s.NODE_SPEC[table][0] == NODE_COLUMNS[table][0]
    assert s.NODE_SPEC[table][1][-1] == ("embedding", "vec")


@pytest.mark.parametrize("table", sorted(EDGE_COLUMNS))
def test_edge_tables_match_spec(table):
    pk, _cols, (scol, stab), (dcol, dtab) = s.EDGE_SPEC[table]
    assert s.write_columns(table) == EDGE_COLUMNS[table]
    assert pk == "edge_id"
    assert (stab, dtab) == ENDS[table]
    assert (scol, dcol) == tuple(EDGE_COLUMNS[table][1:3])


def test_six_node_and_fourteen_edge_tables_in_spec_order():
    assert s.NODE_TABLES == list(NODE_COLUMNS) and s.EDGE_TABLES == list(EDGE_COLUMNS)
    assert (len(s.NODE_TABLES), len(s.EDGE_TABLES)) == (6, 14)
    for gone in ("Modules", "FileImports", "ClassInherits", "FileBelongsToModule"):
        assert gone not in s.NODE_SPEC and gone not in s.EDGE_SPEC


def test_schema_equals_the_spec_fixture_types_included():
    assert s.NODE_SPEC == go.NODE_SPEC
    assert s.EDGE_SPEC == go.EDGE_SPEC


def test_flag_columns_are_bool_and_counts_are_int():
    assert _columns_of("bool") == BOOL_COLUMNS
    assert _columns_of("int") == INT_COLUMNS
    assert _columns_of("vec") == {(t, "embedding") for t in s.NODE_TABLES}
    assert {typ for _t, (_pk, cols, *_r) in {**s.NODE_SPEC, **s.EDGE_SPEC}.items()
            for _c, typ in cols} == {"str", "int", "bool", "vec"}


def test_type_shorthands():
    assert s._TYPE_SQL["bool"] == "BOOL"
    assert s._TYPE_SQL["str"] == "STRING(MAX)" and s._TYPE_SQL["int"] == "INT64"
    assert s._TYPE_SQL["vec"] == "ARRAY<FLOAT64>"
    assert s._TYPE_SQL["float"] == "FLOAT64"  # codedoc_go_leiden's topic metrics


def test_ddl_covers_every_table_with_its_primary_key():
    ddl = dict(zip(s.NODE_TABLES + s.EDGE_TABLES, s.node_table_ddl() + s.edge_table_ddl()))
    assert len(ddl) == 20
    for table, stmt in ddl.items():
        pk = (s.NODE_SPEC.get(table) or s.EDGE_SPEC[table])[0]
        assert stmt.startswith(f"CREATE TABLE {table} (")
        assert stmt.endswith(f") PRIMARY KEY ({pk})")
    assert "generated    BOOL," in ddl["Files"] and "build_included BOOL," in ddl["Files"]
    assert "has_body     BOOL," in ddl["Methods"]
    assert "open_world   BOOL\n" in ddl["PossiblyCalls"]
    assert "pointer      BOOL," in ddl["ClassEmbeds"]
    assert "pointer_receiver BOOL," in ddl["ClassImplements"]
    assert "file_count   INT64," in ddl["Packages"] and "call_line    INT64" in ddl["MethodCalls"]
    for table in s.NODE_TABLES:
        assert "embedding    ARRAY<FLOAT64>" in ddl[table]


def test_graph_ddl_lists_every_node_label_and_edge_with_its_keys(monkeypatch):
    monkeypatch.setattr(config, "GRAPH_NAME", "code_graph_go")
    ddl = s.graph_ddl()
    assert ddl.startswith("CREATE OR REPLACE PROPERTY GRAPH code_graph_go\n  NODE TABLES (\n")
    nodes = ddl.split("NODE TABLES (", 1)[1].split(")", 1)[0]
    assert [n.strip(" ,\n") for n in nodes.split(",")] == s.NODE_TABLES
    edges = ddl.split("EDGE TABLES (", 1)[1]
    for table, (src, dst) in ENDS.items():
        scol, dcol = EDGE_COLUMNS[table][1:3]
        spk, dpk = s.NODE_SPEC[src][0], s.NODE_SPEC[dst][0]
        assert (f"{table}\n      SOURCE KEY ({scol}) REFERENCES {src} ({spk})\n"
                f"      DESTINATION KEY ({dcol}) REFERENCES {dst} ({dpk})") in edges
    assert "Modules" not in ddl and "FileImports" not in ddl


def test_orphan_checks_and_cross_repo_tables_come_from_the_spec():
    checks = dict(s.orphan_checks())
    assert len(checks) == len(s.EDGE_TABLES) == 14
    assert ("SELECT COUNT(*) FROM PackageContainsFile e WHERE NOT EXISTS "
            "(SELECT 1 FROM Packages s WHERE s.package_id = e.package_id) OR NOT EXISTS "
            "(SELECT 1 FROM Files d WHERE d.file_id = e.file_id)"
            ) == checks["PackageContainsFile → Packages/Files"]
    assert "MethodCalls → Methods" in checks
    assert s.cross_repo_tables() == ["CrossRepoCalls", "CrossRepoRef", "CrossRepoImports"]


def test_the_database_comes_from_config(monkeypatch):
    # config's default is codedoc-go (tests/test_config.py); main() passes it on
    assert s.DATABASE_ID == config.SPANNER_DATABASE
    seen = {}
    monkeypatch.setattr("sys.argv", ["prog", "--verify"])
    monkeypatch.setattr(s, "verify",
                        lambda project, instance, database: seen.update(db=database) or True)
    s.main()
    assert seen["db"] == config.SPANNER_DATABASE


class _FakeOp:
    def result(self, timeout=None):
        return None


class _FakeSnapshot:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute_sql(self, sql, params=None, param_types=None):
        self._db.queries.append(sql)
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [[t] for t in self._db.tables]
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [[t, c, typ] for t, cols in self._db.columns.items() if t in self._db.tables
                    for c, typ in cols.items()]
        if "INFORMATION_SCHEMA.PROPERTY_GRAPHS" in sql:
            return [[g] for g in self._db.graphs]
        return [[0]]


class _FakeDatabase:
    def __init__(self, tables, columns, graphs=()):
        self.tables, self.columns, self.graphs = tables, columns, list(graphs)
        self.applied_ddl = []
        self.queries = []

    def exists(self):
        return True

    def snapshot(self):
        return _FakeSnapshot(self)

    def update_ddl(self, ddl):
        self.applied_ddl.extend(ddl)
        return _FakeOp()


def _up_to_date():
    columns = {t: {c: s._TYPE_SQL[typ] for c, typ in cols}
               for t, (_pk, cols, *_rest) in {**s.NODE_SPEC, **s.EDGE_SPEC}.items()}
    return list(columns), columns


def test_migrate_adds_missing_tables_and_columns_bool_included():
    tables, columns = _up_to_date()
    tables.remove("CrossRepoImports")
    tables.remove("PackageContainsFile")
    del columns["Files"]["build_included"]
    del columns["Methods"]["entry_point"]
    del columns["PossiblyCalls"]["open_world"]
    db = _FakeDatabase(tables, columns)
    s.migrate_schema(db)
    ddl = "\n".join(db.applied_ddl)
    assert "CREATE TABLE CrossRepoImports (" in ddl and "CREATE TABLE PackageContainsFile (" in ddl
    assert "ALTER TABLE Files ADD COLUMN build_included BOOL" in db.applied_ddl
    assert "ALTER TABLE Methods ADD COLUMN entry_point STRING(MAX)" in db.applied_ddl
    assert "ALTER TABLE PossiblyCalls ADD COLUMN open_world BOOL" in db.applied_ddl
    assert len(db.applied_ddl) == 5
    assert sum("INFORMATION_SCHEMA.COLUMNS" in q for q in db.queries) == 1  # one query, all tables


def test_migrate_noop_when_up_to_date(capsys):
    db = _FakeDatabase(*_up_to_date())
    s.migrate_schema(db)
    assert db.applied_ddl == [] and "up to date" in capsys.readouterr().out


def test_migrate_reports_a_column_of_another_type_and_leaves_it(capsys):
    tables, columns = _up_to_date()
    columns["Files"]["generated"] = "STRING(MAX)"
    db = _FakeDatabase(tables, columns)
    s.migrate_schema(db)
    out = capsys.readouterr().out
    assert db.applied_ddl == []
    assert "Files.generated is STRING(MAX), the spec says BOOL" in out
    assert "setup spanner" in out  # the fix is named


def test_create_database_existing_migrates_and_refreshes_graph(monkeypatch):
    db = _FakeDatabase(*_up_to_date())
    fake_client = types.SimpleNamespace(
        instance=lambda iid: types.SimpleNamespace(database=lambda name: db))
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: fake_client)
    s.create_database("proj", "inst", "db")
    assert db.applied_ddl == [s.graph_ddl()]


def test_create_database_new_creates_every_table_then_the_graph(monkeypatch):
    created = {}

    class _NewDb(_FakeDatabase):
        def exists(self):
            return False

        def create(self):
            return _FakeOp()

    db = _NewDb([], {})

    def database(name, ddl_statements=None):
        if ddl_statements is not None:
            created["ddl"] = ddl_statements
        return db

    fake_client = types.SimpleNamespace(
        instance=lambda iid: types.SimpleNamespace(database=database))
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: fake_client)
    s.create_database("proj", "inst", "codedoc-go")
    assert created["ddl"] == s.node_table_ddl() + s.edge_table_ddl()
    assert db.applied_ddl == [s.graph_ddl()]


def test_verify_reports_missing_tables_columns_types_and_graph(monkeypatch, capsys):
    tables, columns = _up_to_date()
    tables.remove("CrossRepoImports")
    del columns["Packages"]["file_count"]
    columns["Methods"]["has_body"] = "STRING(MAX)"
    db = _FakeDatabase(tables, columns, graphs=["other_graph"])
    fake_client = types.SimpleNamespace(
        instance=lambda iid: types.SimpleNamespace(database=lambda name: db))
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: fake_client)
    monkeypatch.setattr(config, "GRAPH_NAME", "code_graph_a")
    assert s.verify("proj", "inst", "db") is False
    out = capsys.readouterr().out
    assert "MISSING tables: CrossRepoImports" in out
    assert "MISSING columns: Packages.file_count" in out
    assert "WRONG types: Methods.has_body is STRING(MAX), the spec says BOOL" in out
    assert "MISSING property graph code_graph_a" in out


def test_verify_passes_on_the_spec_schema(monkeypatch, capsys):
    db = _FakeDatabase(*_up_to_date(), graphs=["code_graph_a"])
    fake_client = types.SimpleNamespace(
        instance=lambda iid: types.SimpleNamespace(database=lambda name: db))
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: fake_client)
    monkeypatch.setattr(config, "GRAPH_NAME", "code_graph_a")
    assert s.verify("proj", "inst", "db") is True
    out = capsys.readouterr().out
    assert "Tables (20)" in out and "MISSING" not in out and "WRONG" not in out
    assert "Property graphs (1): code_graph_a" in out


def test_verify_reports_a_failed_graph_query_as_a_missing_graph(monkeypatch, capsys):
    class _NoGraphs(_FakeDatabase):
        def snapshot(self):
            snap = super().snapshot()
            plain = snap.execute_sql

            def execute_sql(sql, **kw):
                if "PROPERTY_GRAPHS" in sql:
                    raise RuntimeError("not found")
                return plain(sql, **kw)
            snap.execute_sql = execute_sql
            return snap

    db = _NoGraphs(*_up_to_date())
    fake_client = types.SimpleNamespace(
        instance=lambda iid: types.SimpleNamespace(database=lambda name: db))
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: fake_client)
    assert s.verify("proj", "inst", "db") is False
    out = capsys.readouterr().out
    assert "Property graphs: none yet (not found)" in out and "MISSING property graph" in out
    assert "fix: run `python -m graph_generator setup spanner`" in out


def test_a_failed_graph_update_is_reported_not_raised(capsys):
    from google.api_core.exceptions import FailedPrecondition

    class _Refusing(_FakeDatabase):
        def update_ddl(self, ddl):
            raise FailedPrecondition("graph is busy")

    s._add_graphs(_Refusing(*_up_to_date()))
    assert "Graph creation note" in capsys.readouterr().out
