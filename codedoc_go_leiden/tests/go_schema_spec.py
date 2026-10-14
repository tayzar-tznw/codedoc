"""The Go graph schema of spec §6.1/§6.2 and the vocabularies of §5.7, as a fixture.

graph_generator/setup_spanner_graph.py becomes the single source of truth once
WP-B3 adapts it; until then the surfaces (agent instruction, SKILL.md, docs)
are checked against this copy, and test_agent_instruction.py checks that the
two agree as soon as the real module carries the Go schema. Shapes as in
NODE_SPEC / EDGE_SPEC: `{table: (pk, [(column, type), ...])}` and
`{table: (pk, [(column, type), ...], (src_col, src_table), (dst_col, dst_table))}`.

Usable as a linter spec: `python -m scripts.gql_lint --spec tests.go_schema_spec`.
"""
from __future__ import annotations

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

# codedoc_go_leiden: the schema additions of the leiden spec §4.
LEIDEN_NODE_SPEC = {**NODE_SPEC, "Topics": ("topic_id", NODE_SPEC["Topics"][1] + [
    ("level", "int"), ("parent_topic_id", "str"), ("size", "int"), ("file_count", "int"),
    ("cohesion", "float"), ("conductance", "float"), ("hub_class_id", "str"),
    ("label_source", "str"), ("basis", "str"), ("membership_hash", "str"),
])}
LEIDEN_EDGE_SPEC = {
    **EDGE_SPEC,
    "FileBelongsToTopic": ("edge_id", EDGE_SPEC["FileBelongsToTopic"][1] + [("role", "str")],
                           ("file_id", "Files"), ("topic_id", "Topics")),
    "ClassBelongsToTopic": ("edge_id", [
        ("edge_id", "str"), ("class_id", "str"), ("topic_id", "str"), ("role", "str"),
    ], ("class_id", "Classes"), ("topic_id", "Topics")),
    "TopicDependsOn": ("edge_id", [
        ("edge_id", "str"), ("source_topic", "str"), ("target_topic", "str"),
        ("weight", "float"), ("call_count", "int"),
    ], ("source_topic", "Topics"), ("target_topic", "Topics")),
    "TopicContainsTopic": ("edge_id", [
        ("edge_id", "str"), ("parent_topic", "str"), ("child_topic", "str"),
    ], ("parent_topic", "Topics"), ("child_topic", "Topics")),
}

# Spec §5.7 and §6.1. WP-D publishes the same tuples in graph_generator/go_model.py;
# each name present there is used instead of the spec copy, and
# test_agent_instruction.py checks that the two still agree.
SPEC_VOCABULARY: dict[str, tuple[str, ...]] = {
    "STATUSES": ("resolved", "external", "builtin", "ambiguous", "dynamic", "unresolved"),
    "VIA": ("types", "convention:funclit", "convention:func-alias", "convention:linkname",
            "convention:range", "none"),
    "POSSIBLY_REASONS": ("implementation", "variant"),
    "UNRESOLVED_REASONS": ("missing-module", "incomplete-type", "excluded-by-build", "toolchain",
                           "no-declaration", "stale", "syntax-error", "unscanned", "outside-repo",
                           "no-module", "load-error"),
    "SITE_KINDS": ("call", "go", "defer", "range"),
    "FILE_ORIGINS": ("package", "command", "test", "xtest"),
    "CLASS_KINDS": ("struct", "interface", "named", "alias", "package"),
    "MEMBER_KINDS": ("func", "method", "interface_method", "init", "closure", "var_init"),
}


def _vocabulary() -> dict[str, tuple[str, ...]]:
    from graph_generator import go_model

    return {name: tuple(getattr(go_model, name, spec)) for name, spec in SPEC_VOCABULARY.items()}


VOCABULARY = _vocabulary()
STATUSES = VOCABULARY["STATUSES"]
VIA = VOCABULARY["VIA"]
POSSIBLY_REASONS = VOCABULARY["POSSIBLY_REASONS"]
UNRESOLVED_REASONS = VOCABULARY["UNRESOLVED_REASONS"]
SITE_KINDS = VOCABULARY["SITE_KINDS"]
ORIGINS = VOCABULARY["FILE_ORIGINS"]
CLASS_KINDS = VOCABULARY["CLASS_KINDS"]
MEMBER_KINDS = VOCABULARY["MEMBER_KINDS"]
IMPORT_KINDS = ("normal", "dot", "blank")
CROSS_REF_KINDS = ("embeds", "implements")
# Leiden spec §4 with L-R9 and L-R26 (leiden-core's community.ROLES and label sources).
LEIDEN_CLASS_ROLES = ("primary", "attached-file", "attached-test", "attached-generated",
                      "attached-excluded", "unassigned")
LEIDEN_FILE_ROLES = ("primary", "secondary")
LEIDEN_LABEL_SOURCES = ("hub", "llm", "inherited", "fixed")
