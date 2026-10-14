"""
Setup script for Spanner Graph infrastructure.

Creates (or idempotently migrates):
- Spanner Enterprise instance
- Database (default codedoc-go) with 6 node tables, 14 edge tables and one
  property graph
- Property graph: config.GRAPH_NAME (default code_graph_a)

The schema is declared once in NODE_SPEC / EDGE_SPEC (spec §6.1, §6.2) and
everything (CREATE TABLE DDL, the property-graph definition, the pipeline's
insert column lists, the idempotent ALTER migration for existing databases,
--verify and `validate`) is derived from it — so the DDL and the Phase 8/9
writes can never drift apart.

Usage:
    python -m graph_generator setup spanner              # 通常はこちら
    python -m graph_generator.setup_spanner_graph [--project PROJECT] [--region REGION]
    python -m graph_generator.setup_spanner_graph --skip-instance   # skip instance creation
    python -m graph_generator.setup_spanner_graph --migrate          # add missing tables/columns only
    python -m graph_generator.setup_spanner_graph --verify|--destroy
"""

import argparse

from . import config
from .lazy_import import LazyModule

# Imported on first use: graph_rows imports this module for write_columns, and
# the graph track with --no-spanner never touches Spanner.
spanner = LazyModule("google.cloud.spanner")
gax_exceptions = LazyModule("google.api_core.exceptions")

PROJECT_ID = config.GCP_PROJECT
INSTANCE_ID = config.SPANNER_INSTANCE
DATABASE_ID = config.SPANNER_DATABASE

REGION = "asia-northeast1"


# ===================================================================
# Schema specification (single source of truth)
# ===================================================================
#
# Column type shorthands: "str" → STRING(MAX), "int" → INT64, "bool" → BOOL
# (the flag columns; rows carry Python bools), "float" → FLOAT64
# (codedoc_go_leiden's topic metrics), "vec" → ARRAY<FLOAT64> (embeddings;
# written in Phase 10, not Phase 8). JSON payloads (Classes.embeds) are
# STRING(MAX).

_TYPE_SQL = {"str": "STRING(MAX)", "int": "INT64", "bool": "BOOL", "float": "FLOAT64",
             "vec": "ARRAY<FLOAT64>"}

# Node tables: name → (pk, [(column, type), ...]), columns in spec §6.1 order.
# Every node table ends with an `embedding` vector (Phase 10 embeds Files,
# Packages, Classes, Methods and Topics). `Classes` holds Go types and the
# per-file `<package>` pseudo-type; `Topics` are the wiki's topics, not Go
# modules (the module path is Files.module).
NODE_SPEC: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "Files": ("file_id", [
        ("file_id", "str"), ("file_name", "str"), ("extension", "str"), ("directory", "str"),
        ("path", "str"), ("origin", "str"), ("generated", "bool"), ("package", "str"),
        ("package_name", "str"), ("module", "str"), ("build_constraint", "str"),
        ("build_included", "bool"), ("summary", "str"), ("repo", "str"), ("embedding", "vec"),
    ]),
    "Packages": ("package_id", [
        ("package_id", "str"), ("import_path", "str"), ("name", "str"), ("directory", "str"),
        ("module", "str"), ("origin", "str"), ("file_count", "int"), ("summary", "str"),
        ("repo", "str"), ("embedding", "vec"),
    ]),
    "Classes": ("class_id", [
        ("class_id", "str"), ("name", "str"), ("qualname", "str"), ("package", "str"),
        ("fqcn", "str"), ("file_id", "str"), ("kind", "str"), ("type_params", "str"),
        ("embeds", "str"), ("start_line", "int"), ("end_line", "int"), ("origin", "str"),
        ("module", "str"), ("summary", "str"), ("repo", "str"), ("embedding", "vec"),
    ]),
    "Methods": ("method_id", [
        ("method_id", "str"), ("name", "str"), ("class_id", "str"), ("file_id", "str"),
        ("qualname", "str"), ("fqmn", "str"), ("signature", "str"), ("member_kind", "str"),
        ("receiver", "str"), ("type_params", "str"), ("has_body", "bool"), ("start_line", "int"),
        ("end_line", "int"), ("origin", "str"), ("module", "str"), ("entry_point", "str"),
        ("summary", "str"), ("repo", "str"), ("embedding", "vec"),
    ]),
    "Topics": ("topic_id", [
        ("topic_id", "str"), ("name", "str"), ("summary", "str"), ("repo", "str"),
        ("embedding", "vec"),
    ]),
    "Directories": ("dir_id", [
        ("dir_id", "str"), ("name", "str"), ("summary", "str"), ("repo", "str"),
        ("embedding", "vec"),
    ]),
}

# Edge tables: name → (pk, [(column, type)...], (src_col, src_table), (dst_col, dst_table)),
# columns in spec §6.2 order. Phase 9 writes the first ten, Phase 10 (wiki)
# FileBelongsToTopic, and `crossref` the CrossRepo* tables, whose endpoints are
# nodes in different repos.
EDGE_SPEC: dict[str, tuple[str, list[tuple[str, str]], tuple[str, str], tuple[str, str]]] = {
    "PackageImports": ("edge_id", [
        ("edge_id", "str"), ("source_file", "str"), ("target_package", "str"),
        ("import_path", "str"), ("alias", "str"), ("kind", "str"),
    ], ("source_file", "Files"), ("target_package", "Packages")),
    "FileDependsOn": ("edge_id", [
        ("edge_id", "str"), ("source_file", "str"), ("target_file", "str"), ("resolution", "str"),
    ], ("source_file", "Files"), ("target_file", "Files")),
    "ClassEmbeds": ("edge_id", [
        ("edge_id", "str"), ("child_class", "str"), ("embedded_class", "str"),
        ("pointer", "bool"), ("resolution", "str"),
    ], ("child_class", "Classes"), ("embedded_class", "Classes")),
    "ClassImplements": ("edge_id", [
        ("edge_id", "str"), ("class_id", "str"), ("interface_id", "str"),
        ("pointer_receiver", "bool"), ("resolution", "str"),
    ], ("class_id", "Classes"), ("interface_id", "Classes")),
    "MethodCalls": ("edge_id", [
        ("edge_id", "str"), ("caller_method", "str"), ("callee_method", "str"),
        ("callee_name", "str"), ("resolution", "str"), ("site_kind", "str"), ("call_line", "int"),
    ], ("caller_method", "Methods"), ("callee_method", "Methods")),
    "PossiblyCalls": ("edge_id", [
        ("edge_id", "str"), ("caller_method", "str"), ("callee_method", "str"),
        ("callee_name", "str"), ("reason", "str"), ("candidate_count", "int"),
        ("open_world", "bool"),
    ], ("caller_method", "Methods"), ("callee_method", "Methods")),
    "FileDefinesClass": ("edge_id", [
        ("edge_id", "str"), ("file_id", "str"), ("class_id", "str"),
    ], ("file_id", "Files"), ("class_id", "Classes")),
    "ClassDefinesMethod": ("edge_id", [
        ("edge_id", "str"), ("class_id", "str"), ("method_id", "str"),
    ], ("class_id", "Classes"), ("method_id", "Methods")),
    "DirContainsFile": ("edge_id", [
        ("edge_id", "str"), ("dir_id", "str"), ("file_id", "str"),
    ], ("dir_id", "Directories"), ("file_id", "Files")),
    "PackageContainsFile": ("edge_id", [
        ("edge_id", "str"), ("package_id", "str"), ("file_id", "str"),
    ], ("package_id", "Packages"), ("file_id", "Files")),
    "FileBelongsToTopic": ("edge_id", [
        ("edge_id", "str"), ("file_id", "str"), ("topic_id", "str"),
    ], ("file_id", "Files"), ("topic_id", "Topics")),
    "CrossRepoCalls": ("edge_id", [
        ("edge_id", "str"), ("caller_method", "str"), ("callee_method", "str"),
        ("source_repo", "str"), ("target_repo", "str"), ("symbol", "str"), ("site_kind", "str"),
    ], ("caller_method", "Methods"), ("callee_method", "Methods")),
    "CrossRepoRef": ("edge_id", [
        ("edge_id", "str"), ("source_class", "str"), ("target_class", "str"),
        ("source_repo", "str"), ("target_repo", "str"), ("symbol", "str"), ("kind", "str"),
    ], ("source_class", "Classes"), ("target_class", "Classes")),
    "CrossRepoImports": ("edge_id", [
        ("edge_id", "str"), ("source_file", "str"), ("target_package", "str"),
        ("source_repo", "str"), ("target_repo", "str"), ("import_path", "str"),
    ], ("source_file", "Files"), ("target_package", "Packages")),
}

from . import topics_schema  # noqa: E402
NODE_SPEC, EDGE_SPEC = topics_schema.extend(NODE_SPEC, EDGE_SPEC)  # LEIDEN-HOOK: leiden spec §4

NODE_TABLES = list(NODE_SPEC)
EDGE_TABLES = list(EDGE_SPEC)

# Secondary indexes the DDL creates, per table (none). Spanner counts a deleted
# row as one mutation plus one per index entry, so pipeline._batch_delete
# sizes its commits from this.
SECONDARY_INDEXES: dict[str, tuple[str, ...]] = {}


def write_columns(table: str) -> list[str]:
    """Column names Phase 8/9 write for a table (embedding excluded — it is
    populated separately in Phase 10). This is what the pipeline imports so
    its inserts always match the DDL."""
    if table in NODE_SPEC:
        cols = NODE_SPEC[table][1]
    else:
        cols = EDGE_SPEC[table][1]
    return [c for c, _t in cols if c != "embedding"]


def orphan_checks() -> list[tuple[str, str]]:
    """(label, SQL) per edge table: count the rows whose source or destination
    key matches no node. Derived from EDGE_SPEC, so `validate` never names a
    table by hand."""
    checks = []
    for name, (_pk, _cols, (scol, stab), (dcol, dtab)) in EDGE_SPEC.items():
        spk, dpk = NODE_SPEC[stab][0], NODE_SPEC[dtab][0]
        label = f"{name} → {stab}" if stab == dtab else f"{name} → {stab}/{dtab}"
        checks.append((label,
                       f"SELECT COUNT(*) FROM {name} e "
                       f"WHERE NOT EXISTS (SELECT 1 FROM {stab} s WHERE s.{spk} = e.{scol}) "
                       f"OR NOT EXISTS (SELECT 1 FROM {dtab} d WHERE d.{dpk} = e.{dcol})"))
    return checks


def cross_repo_tables() -> list[str]:
    """Edge tables that carry source_repo/target_repo (the crossref layer)."""
    return [name for name, (_pk, cols, _src, _dst) in EDGE_SPEC.items()
            if {"source_repo", "target_repo"} <= {c for c, _t in cols}]


def _columns_sql(cols: list[tuple[str, str]]) -> str:
    return ",\n        ".join(f"{name:<12} {_TYPE_SQL[t]}" for name, t in cols)


def node_table_ddl() -> list[str]:
    out = []
    for name, (pk, cols) in NODE_SPEC.items():
        out.append(f"CREATE TABLE {name} (\n        {_columns_sql(cols)}\n    ) "
                   f"PRIMARY KEY ({pk})")
    return out


def edge_table_ddl() -> list[str]:
    out = []
    for name, (pk, cols, _src, _dst) in EDGE_SPEC.items():
        out.append(f"CREATE TABLE {name} (\n        {_columns_sql(cols)}\n    ) "
                   f"PRIMARY KEY ({pk})")
    return out


def graph_ddl() -> str:
    node_list = ",\n    ".join(NODE_TABLES)
    edge_defs = []
    for name, (_pk, _cols, (scol, stab), (dcol, dtab)) in EDGE_SPEC.items():
        spk = NODE_SPEC[stab][0]
        dpk = NODE_SPEC[dtab][0]
        edge_defs.append(
            f"{name}\n"
            f"      SOURCE KEY ({scol}) REFERENCES {stab} ({spk})\n"
            f"      DESTINATION KEY ({dcol}) REFERENCES {dtab} ({dpk})")
    edge_list = ",\n    ".join(edge_defs)
    return (f"CREATE OR REPLACE PROPERTY GRAPH {config.GRAPH_NAME}\n"
            f"  NODE TABLES (\n    {node_list}\n  )\n"
            f"  EDGE TABLES (\n    {edge_list}\n  )")


def _all_table_ddl() -> list[str]:
    return node_table_ddl() + edge_table_ddl()


# ===================================================================
# Instance / database lifecycle
# ===================================================================


def create_instance(project_id: str | None, instance_id: str, region: str):
    """Create Spanner Enterprise instance with 100 PU. Without a project the
    client library's default (ADC / gcloud) names the resources."""
    from google.cloud.spanner_admin_instance_v1.types import spanner_instance_admin

    client = spanner.Client(project=project_id, disable_builtin_metrics=True)
    project_id = project_id or client.project

    instance = client.instance(instance_id)
    if instance.exists():
        print(f"  Instance '{instance_id}' already exists, skipping.")
        return

    config_name = f"projects/{project_id}/instanceConfigs/regional-{region}"
    admin_api = client.instance_admin_api
    instance_proto = spanner_instance_admin.Instance(
        name=f"projects/{project_id}/instances/{instance_id}",
        config=config_name,
        display_name="CodeDoc Graph RAG",
        processing_units=100,
        edition=spanner_instance_admin.Instance.Edition.ENTERPRISE,
    )

    print(f"  Creating instance '{instance_id}' (Enterprise, 100 PU, {region})...")
    operation = admin_api.create_instance(
        parent=f"projects/{project_id}",
        instance_id=instance_id,
        instance=instance_proto,
    )
    print("  Waiting for instance creation...")
    operation.result(timeout=300)
    print(f"  Instance '{instance_id}' created (Enterprise edition).")


def create_database(project_id: str, instance_id: str, database_id: str):
    """Create the database, or migrate an existing one to the current schema."""
    client = spanner.Client(project=project_id, disable_builtin_metrics=True)
    instance = client.instance(instance_id)

    database = instance.database(database_id)
    if database.exists():
        print(f"  Database '{database_id}' already exists — migrating schema...")
        migrate_schema(database)
        _add_graphs(database)
        return

    table_ddl = _all_table_ddl()
    print(f"  Creating database '{database_id}' with {len(table_ddl)} tables...")
    database = instance.database(database_id, ddl_statements=table_ddl)
    operation = database.create()
    print("  Waiting for database creation...")
    operation.result(timeout=600)
    print(f"  Database '{database_id}' created with {len(table_ddl)} tables.")

    _add_graphs(database)


def _existing_tables(database) -> set[str]:
    with database.snapshot() as snapshot:
        rows = snapshot.execute_sql(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ''")
        return {r[0] for r in rows}


def _existing_column_types(database) -> dict[str, dict[str, str]]:
    """table → {column: SPANNER_TYPE} for the default schema, in one query."""
    out: dict[str, dict[str, str]] = {}
    with database.snapshot() as snapshot:
        for table, column, spanner_type in snapshot.execute_sql(
                "SELECT TABLE_NAME, COLUMN_NAME, SPANNER_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ''"):
            out.setdefault(table, {})[column] = spanner_type
    return out


def schema_drift(tables: set[str], columns: dict[str, dict[str, str]]
                 ) -> tuple[list[str], list[tuple[str, str, str]], list[tuple[str, str, str, str]]]:
    """How a database differs from NODE_SPEC / EDGE_SPEC: the missing tables,
    the missing columns of existing tables as (table, column, type) and the
    columns of another type as (table, column, type found, type in the spec).
    Types are DDL text (STRING(MAX), BOOL, ...); extra tables and columns are
    not reported."""
    specs = {**{n: cols for n, (_pk, cols) in NODE_SPEC.items()},
             **{n: cols for n, (_pk, cols, _src, _dst) in EDGE_SPEC.items()}}
    missing_tables, missing_columns, wrong_types = [], [], []
    for table, cols in specs.items():
        if table not in tables:
            missing_tables.append(table)
            continue
        have = columns.get(table, {})
        for col, typ in cols:
            want = _TYPE_SQL[typ]
            if col not in have:
                missing_columns.append((table, col, want))
            elif have[col] != want:
                wrong_types.append((table, col, have[col], want))
    return missing_tables, missing_columns, wrong_types


_RECREATE_FIX = ("Spanner cannot change a column's type in place: drop the database "
                 "(python -m graph_generator.setup_spanner_graph --destroy) and run "
                 "`python -m graph_generator setup spanner` again")


def migrate_schema(database):
    """Idempotently add missing tables and columns to an existing database.

    Reads INFORMATION_SCHEMA and issues only the deltas (CREATE TABLE for new
    tables, ALTER TABLE ADD COLUMN for new columns) so upgrading a graph built
    by an older version never drops data. A column whose type differs from the
    spec is reported, never changed. The property graph is refreshed
    separately via CREATE OR REPLACE.
    """
    missing_tables, missing_columns, wrong_types = schema_drift(
        _existing_tables(database), _existing_column_types(database))
    create_map = dict(zip(NODE_TABLES + EDGE_TABLES, _all_table_ddl()))
    ddl = [create_map[name] for name in missing_tables]
    ddl += [f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}"
            for table, col, sql_type in missing_columns]
    for table, col, have, want in wrong_types:
        print(f"  WARNING: {table}.{col} is {have}, the spec says {want}. {_RECREATE_FIX}.")

    if not ddl:
        print("  Schema already up to date — no table/column changes.")
        return
    print(f"  Applying {len(ddl)} schema change(s):")
    for stmt in ddl:
        print(f"    {stmt.splitlines()[0].strip()[:80]}")
    operation = database.update_ddl(ddl)
    operation.result(timeout=600)
    print("  Schema migration complete.")


def _add_graphs(database):
    """Add / refresh the property graph definition."""
    try:
        print("  Adding property graph definition...")
        operation = database.update_ddl([graph_ddl()])
        print("  Waiting for graph creation...")
        operation.result(timeout=300)
        print(f"  Property graph created: {config.GRAPH_NAME}")
    except gax_exceptions.GoogleAPICallError as e:
        print(f"  Graph creation note: {e}")


def destroy_all(project_id: str, instance_id: str, database_id: str):
    """Drop the database. The instance stays: the sibling variants keep their
    databases in the same instance (codedoc-instance)."""
    client = spanner.Client(project=project_id, disable_builtin_metrics=True)
    database = client.instance(instance_id).database(database_id)
    if database.exists():
        print(f"  Dropping database '{database_id}'...")
        database.drop()
        print("  Database dropped.")
    else:
        print(f"  Database '{database_id}' does not exist.")
    print(f"  Instance '{instance_id}' is kept; other databases may live in it.")


def verify(project_id: str, instance_id: str, database_id: str) -> bool:
    """Check the database against NODE_SPEC / EDGE_SPEC: tables, columns and
    their types, and the property graph config.GRAPH_NAME. Prints every
    problem with the fix; True when there is none."""
    client = spanner.Client(project=project_id, disable_builtin_metrics=True)
    database = client.instance(instance_id).database(database_id)

    tables = sorted(_existing_tables(database))
    print(f"  Tables ({len(tables)}): {', '.join(tables)}")
    missing_tables, missing_columns, wrong_types = schema_drift(
        set(tables), _existing_column_types(database))
    if missing_tables:
        print(f"  MISSING tables: {', '.join(missing_tables)}")
    if missing_columns:
        print("  MISSING columns: " + ", ".join(f"{t}.{c}" for t, c, _typ in missing_columns))
    if wrong_types:
        print("  WRONG types: " + "; ".join(f"{t}.{c} is {have}, the spec says {want}"
                                           for t, c, have, want in wrong_types))

    graphs: list[str] = []
    with database.snapshot() as snapshot:
        try:
            graphs = [row[0] for row in snapshot.execute_sql(
                "SELECT PROPERTY_GRAPH_NAME FROM INFORMATION_SCHEMA.PROPERTY_GRAPHS")]
            print(f"  Property graphs ({len(graphs)}): {', '.join(graphs)}")
        except Exception as e:
            print(f"  Property graphs: none yet ({e})")
    if config.GRAPH_NAME not in graphs:
        print(f"  MISSING property graph {config.GRAPH_NAME}")

    if wrong_types:
        print(f"  fix: {_RECREATE_FIX}")
    elif missing_tables or missing_columns or config.GRAPH_NAME not in graphs:
        print("  fix: run `python -m graph_generator setup spanner` (or this module with "
              "--migrate); it adds the missing tables, columns and the property graph")
    return not (missing_tables or missing_columns or wrong_types
                or config.GRAPH_NAME not in graphs)


def main():
    parser = argparse.ArgumentParser(
        prog="python -m graph_generator.setup_spanner_graph",
        description="Setup Spanner Graph for CodeDoc",
    )
    parser.add_argument("--project", default=PROJECT_ID, help="GCP project ID")
    parser.add_argument("--region", default=REGION, help="Spanner region")
    parser.add_argument("--instance", default=INSTANCE_ID, help="Spanner instance ID")
    parser.add_argument("--database", default=DATABASE_ID, help="Spanner database ID")
    parser.add_argument("--skip-instance", action="store_true", help="Skip instance creation")
    parser.add_argument("--migrate", action="store_true",
                        help="Only add missing tables/columns to an existing database")
    parser.add_argument("--destroy", action="store_true",
                        help="Drop the database (the instance is kept)")
    parser.add_argument("--verify", action="store_true",
                        help="Check tables, columns, types and the property graph; "
                             "exit 1 on a problem")
    args = parser.parse_args()

    print(f"Project: {args.project}")
    print(f"Instance: {args.instance}")
    print(f"Database: {args.database}")
    print(f"Region: {args.region}")
    print()

    if args.destroy:
        print(f"DROPPING database '{args.database}'...")
        confirm = input("Type 'yes' to confirm: ")
        if confirm != "yes":
            print("Aborted.")
            return
        destroy_all(args.project, args.instance, args.database)
        return

    if args.verify:
        print("Verifying setup...")
        if not verify(args.project, args.instance, args.database):
            raise SystemExit(1)
        return

    if args.migrate:
        print("Migrating existing database schema...")
        client = spanner.Client(project=args.project, disable_builtin_metrics=True)
        database = client.instance(args.instance).database(args.database)
        migrate_schema(database)
        _add_graphs(database)
        return

    if not args.skip_instance:
        print("Step 1: Creating Spanner instance...")
        create_instance(args.project, args.instance, args.region)
    else:
        print("Step 1: Skipping instance creation (--skip-instance)")

    print("\nStep 2: Creating database with tables and graphs...")
    create_database(args.project, args.instance, args.database)

    print("\nStep 3: Verifying setup...")
    verify(args.project, args.instance, args.database)

    print("\nDone! Spanner Graph infrastructure is ready.")
    print(f"  Instance: {args.instance}")
    print(f"  Database: {args.database}")
    print(f"  Graph: {config.GRAPH_NAME}")


if __name__ == "__main__":
    main()
