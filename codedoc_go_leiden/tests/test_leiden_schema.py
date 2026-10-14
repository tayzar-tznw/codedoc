"""codedoc_go_leiden's schema additions (leiden spec §4, L-R40): topics_schema
extends codedoc_go's NODE_SPEC / EDGE_SPEC, and every topic row the pipeline
writes carries exactly the spec's columns."""
from __future__ import annotations

import math

from graph_generator import setup_spanner_graph as schema
from graph_generator import topics_schema
from graph_generator.topics import community, graph_builder, synthetic
from tests import go_schema_spec as go


def test_the_schema_module_carries_the_leiden_tables():
    assert schema.NODE_SPEC == go.LEIDEN_NODE_SPEC
    assert schema.EDGE_SPEC == go.LEIDEN_EDGE_SPEC
    assert list(schema.EDGE_SPEC) == list(go.LEIDEN_EDGE_SPEC)  # DDL order
    assert schema.NODE_TABLES == list(go.LEIDEN_NODE_SPEC)
    assert len(schema.NODE_TABLES) == 6 and len(schema.EDGE_TABLES) == 17


def test_extend_does_not_change_its_inputs():
    nodes, edges = dict(go.NODE_SPEC), dict(go.EDGE_SPEC)
    topics_before = list(nodes["Topics"][1])
    out_nodes, out_edges = topics_schema.extend(nodes, edges)
    assert nodes["Topics"][1] == topics_before and "ClassBelongsToTopic" not in edges
    assert (out_nodes, out_edges) == (go.LEIDEN_NODE_SPEC, go.LEIDEN_EDGE_SPEC)


def test_extend_is_idempotent():
    once = topics_schema.extend(go.NODE_SPEC, go.EDGE_SPEC)
    assert topics_schema.extend(*once) == once


def test_property_graph_has_the_topic_edges():
    ddl = schema.graph_ddl()
    assert ("ClassBelongsToTopic\n      SOURCE KEY (class_id) REFERENCES Classes (class_id)\n"
            "      DESTINATION KEY (topic_id) REFERENCES Topics (topic_id)") in ddl
    assert ("TopicDependsOn\n      SOURCE KEY (source_topic) REFERENCES Topics (topic_id)\n"
            "      DESTINATION KEY (target_topic) REFERENCES Topics (topic_id)") in ddl
    assert ("TopicContainsTopic\n      SOURCE KEY (parent_topic) REFERENCES Topics (topic_id)\n"
            "      DESTINATION KEY (child_topic) REFERENCES Topics (topic_id)") in ddl
    assert "cohesion     FLOAT64" in "\n".join(schema.node_table_ddl())


def test_orphan_checks_cover_the_topic_edges():
    labels = [label for label, _sql in schema.orphan_checks()]
    assert "ClassBelongsToTopic → Classes/Topics" in labels
    assert "TopicDependsOn → Topics" in labels and "TopicContainsTopic → Topics" in labels


def test_topic_rows_carry_exactly_the_write_columns():
    """Every row of community.topic_rows has the table's write columns, plus the
    embedding the Topics rows leave to Phase 10; floats are finite."""
    nodes, edges, _features = synthetic.synthetic_repo(n_atoms=120, seed=0)
    projection = graph_builder.build_projection(nodes, edges, "strict")
    communities = community.detect(projection, {"repo": "iryo"})
    rows = community.topic_rows(communities, nodes)
    assert set(rows) == set(community.TOPIC_TABLES)
    for table, table_rows in rows.items():
        assert table_rows, table
        expected = set(schema.write_columns(table)) | ({"embedding"} if table == "Topics" else set())
        for row in table_rows:
            assert set(row) == expected, table
            for value in row.values():
                if isinstance(value, float):
                    assert math.isfinite(value)
