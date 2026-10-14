"""pytest plugin of codedoc_go_leiden (pytest.ini loads it: -p tests.leiden_plugin).

The suite is codedoc_go's, synced by scripts/sync_from_codedoc_go.sh, plus the
leiden tests (tests/test_topics_*.py, tests/test_leiden_*.py). A codedoc_go
test whose expectation is codedoc_go's own is skipped here, with the reason
and the leiden test that covers the same behaviour. Every other codedoc_go
test runs unchanged against the leiden code.
"""
from __future__ import annotations

import pytest

_DEFAULTS = "codedoc_go's database, service or image name (tests/test_leiden_defaults.py)"
_SCHEMA = ("codedoc_go's 6 node and 14 edge tables; this variant adds the topic tables "
           "(tests/test_leiden_schema.py)")
_ROWS = "the preflight table gains the Leiden rows (tests/test_leiden_preflight.py)"
_GRAPH = ("the graph track runs Phase 4a and writes the topic tables "
          "(tests/test_leiden_graph_track.py)")
_WIKI = ("codedoc_go's LLM topics (Phases 4, 5, 10); this variant names Leiden communities "
         "(tests/test_leiden_wiki_track.py)")

# Node ID, or a prefix ending in "::" or the test name before "[": the reason.
SKIPS: dict[str, str] = {
    "tests/test_agent_instruction.py::test_codedoc_go_uses_the_llm_topics":
        "TOPIC_SOURCE is leiden (tests/test_leiden_defaults.py)",
    "tests/test_agent_instruction.py::test_config_reads_only_the_root_dotenv_and_defaults": _DEFAULTS,
    "tests/test_cli.py::test_doctor_stub_prints_a_table": _ROWS,
    "tests/test_cli.py::test_timing_report_banner_and_monotonic_clock":
        "Phase 4 is 4b here (tests/test_leiden_graph_track.py::test_the_timing_report_names_the_leiden_phases)",
    "tests/test_config.py::test_defaults": _DEFAULTS,
    "tests/test_config.py::test_env_example_lists_every_setting":
        "the topic settings are added (tests/test_leiden_defaults.py)",
    "tests/test_deploy_script.py::test_defaults_name_the_go_service_image_and_database": _DEFAULTS,
    "tests/test_deploy_script.py::test_iap_grants_the_iap_agent_invoker": _DEFAULTS,
    "tests/test_deploy_script.py::test_upload_allow_list_and_build_config": _DEFAULTS,
    "tests/test_deploy_script.py::test_service_name_override_keeps_this_variants_image": _DEFAULTS,
    "tests/test_deploy_script.py::test_mcp_readme_states_this_variants_defaults":
        _DEFAULTS + " (tests/test_leiden_defaults.py::test_mcp_readme_states_this_variants_defaults)",
    "tests/test_gql_lint.py::test_cli_passes_on_the_instruction_and_the_skill":
        "lints against codedoc_go's schema fixture (tests/test_leiden_defaults.py lints the real schema)",
    "tests/test_gql_lint.py::test_cli_no_docs_lints_the_instruction_and_the_skill_only":
        "lints against codedoc_go's schema fixture (tests/test_leiden_defaults.py lints the real schema)",
    "tests/test_graph_checkpoint.py::test_checkpoint_has_the_c7_shape": _DEFAULTS + "; " + _GRAPH,
    "tests/test_graph_prune.py::test_reingest_removes_what_the_code_no_longer_has":
        "its topics are codedoc_go's LLM topics, which --graph leaves alone; here --graph replaces the "
        "Leiden topic tables (tests/test_leiden_graph_track.py)",
    "tests/test_hermetic.py::test_no_configuration_variable_reaches_a_test":
        _DEFAULTS + " (tests/test_leiden_defaults.py::test_the_suite_is_hermetic_for_the_leiden_settings)",
    "tests/test_graph_checkpoint.py::test_same_inputs_skip_phases_8_and_9": _GRAPH,
    "tests/test_graph_checkpoint.py::test_interrupted_phase_9_resumes_without_rewriting_nodes": _GRAPH,
    "tests/test_preflight.py::test_missing_wp_d_modules_are_todo_rows": _ROWS,
    "tests/test_preflight.py::test_toolchain_and_helper_ok": _ROWS,
    "tests/test_preflight.py::test_doctor_reports_groups_go_lines_and_missing_modules": _ROWS,
    "tests/test_preflight.py::test_spanner_schema_present": _DEFAULTS,
    "tests/test_preflight.py::test_checks_per_mode": _ROWS,
    "tests/test_setup_spanner.py::test_node_tables_match_spec": _SCHEMA,
    "tests/test_setup_spanner.py::test_edge_tables_match_spec": _SCHEMA,
    "tests/test_setup_spanner.py::test_six_node_and_fourteen_edge_tables_in_spec_order": _SCHEMA,
    "tests/test_setup_spanner.py::test_schema_equals_the_spec_fixture_types_included": _SCHEMA,
    "tests/test_setup_spanner.py::test_flag_columns_are_bool_and_counts_are_int": _SCHEMA,
    "tests/test_setup_spanner.py::test_ddl_covers_every_table_with_its_primary_key": _SCHEMA,
    "tests/test_setup_spanner.py::test_orphan_checks_and_cross_repo_tables_come_from_the_spec": _SCHEMA,
    "tests/test_setup_spanner.py::test_verify_passes_on_the_spec_schema": _SCHEMA,
    "tests/test_surface_files.py::test_env_example_values_follow_spec[SPANNER_DATABASE-codedoc-go]": _DEFAULTS,
    "tests/test_surfaces.py::test_agent_reads_model_database_and_graph_from_env": _DEFAULTS,
    "tests/test_tracks.py::test_all_runs_the_graph_track_before_the_wiki_track": _GRAPH + "; " + _WIKI,
    "tests/test_tracks.py::test_graph_mode_makes_no_wiki_calls": _GRAPH,
    "tests/test_tracks.py::test_wiki_mode_reuses_the_finished_graph": _WIKI,
    "tests/test_tracks.py::test_no_spanner_writes_jsonl_rows_and_no_spanner_calls": _GRAPH,
    "tests/test_tracks.py::test_wiki_track_writes_pages_by_topic_id": _WIKI,
    "tests/test_validate.py::test_validate_queries_only_spec_tables": _SCHEMA,
    "tests/test_wiki_layers.py::test_write_back_updates_replaces_topics_and_embeds": _WIKI,
    "tests/test_wiki_layers.py::test_write_back_without_spanner_writes_topic_rows_locally": _WIKI,
    "tests/test_wiki_layers.py::test_only_graph_nodes_are_updated": _WIKI,
    "tests/test_wiki_layers.py::test_wiki_track_end_to_end_then_fully_cached": _WIKI,
    "tests/test_wiki_layers.py::test_unchanged_summaries_make_no_embedding_calls": _WIKI,
    "tests/test_wiki_layers.py::test_changed_summary_reembeds_only_that_node": _WIKI,
    "tests/test_wiki_layers.py::test_embedding_cache_key_has_model_and_dimension": _WIKI,
    "tests/test_wiki_preconditions.py::test_wiki_refuses_on_spanner_target_mismatch": _DEFAULTS,
    "tests/test_wiki_preconditions.py::test_cli_wiki_exit_codes[flags0-0]":
        "its graph has no communities, so --wiki refuses (tests/test_leiden_wiki_track.py)",
}


def _matches(nodeid: str, key: str) -> bool:
    return nodeid == key or nodeid.startswith(key + "[") or nodeid.startswith(key + "::")


def pytest_collection_modifyitems(config, items):
    for item in items:
        reason = next((r for key, r in SKIPS.items() if _matches(item.nodeid, key)), None)
        if reason:
            item.add_marker(pytest.mark.skip(reason=f"codedoc_go_leiden: {reason}"))
