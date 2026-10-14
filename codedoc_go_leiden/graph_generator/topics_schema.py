"""The schema additions of codedoc_go_leiden (leiden spec §4, L-R40).

setup_spanner_graph.py keeps codedoc_go's NODE_SPEC / EDGE_SPEC and passes
them through `extend` (its LEIDEN-HOOK), so the DDL, the property graph, the
insert column lists, migrations and `validate` all carry the topic tables:

| Table | Change |
|---|---|
| `Topics` | level, parent_topic_id, size, file_count, cohesion, conductance, hub_class_id, label_source, basis, membership_hash |
| `FileBelongsToTopic` | role (`primary` / `secondary`) |
| `ClassBelongsToTopic` | new: Classes → Topics, with the member's role |
| `TopicDependsOn` | new: directed calls between leaf topics |
| `TopicContainsTopic` | new: a topic and its subtopics |
"""

from __future__ import annotations

TOPIC_COLUMNS = [
    ("level", "int"), ("parent_topic_id", "str"), ("size", "int"), ("file_count", "int"),
    ("cohesion", "float"), ("conductance", "float"), ("hub_class_id", "str"),
    ("label_source", "str"), ("basis", "str"), ("membership_hash", "str"),
]
FILE_TOPIC_COLUMNS = [("role", "str")]
NEW_EDGES = {
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


def _with(columns: list, extra: list) -> list:
    have = {name for name, _type in columns}
    return list(columns) + [c for c in extra if c[0] not in have]


def extend(node_spec: dict, edge_spec: dict) -> tuple[dict, dict]:
    """New NODE_SPEC / EDGE_SPEC dicts with the topic columns and tables (the
    inputs are not changed; extending twice changes nothing). The new edge
    tables follow codedoc_go's, so codedoc_go's tables keep their DDL order."""
    nodes = dict(node_spec)
    pk, columns = nodes["Topics"]
    nodes["Topics"] = (pk, _with(columns, TOPIC_COLUMNS))
    edges = dict(edge_spec)
    pk, columns, src, dst = edges["FileBelongsToTopic"]
    edges["FileBelongsToTopic"] = (pk, _with(columns, FILE_TOPIC_COLUMNS), src, dst)
    for name, spec in NEW_EDGES.items():
        edges.setdefault(name, spec)
    return nodes, edges
