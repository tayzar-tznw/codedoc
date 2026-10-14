"""Group codedoc_python test files by component and count lines (read-only)."""
import glob, os, re, sys
root = sys.argv[1]
groups = [
 ("extractor", r"test_treesitter_"),
 ("discovery-units/envs", r"test_py_(units|envs)\b"),
 ("layout", r"test_py_layout"),
 ("lsp client/session/captures", r"test_lsp_|test_py_session|test_pyright_fixtures|fake_lsp_server|pyright_captures"),
 ("classify/semantics/model", r"test_py_classify|test_py_semantics|test_py_model|decl_builders"),
 ("rules/gates/importtable", r"test_rules_|test_gates_|test_importtable|rule_worlds|gate_worlds|rules_support|table_world"),
 ("resolution orchestrator", r"test_resolution|resolution_fakes"),
 ("crossref", r"test_crossref"),
 ("evaluate", r"test_evaluate|test_verification_table"),
 ("provisioning", r"test_py_provision"),
 ("docs/agent/surfaces", r"test_doc|test_gql_lint|test_agent_instruction|test_skill_doc|test_surface|test_deploy_script|test_prompts"),
 ("pipeline/graph/cli/config", r"test_cli|test_config|test_scan|test_pipeline|test_graph_|test_entities_store|test_phase1|test_setup_spanner|test_spanner_project|test_validate|test_entry_points_env|graph_samples|conftest"),
]
tot = {}
seen = set()
files = sorted(glob.glob(os.path.join(root, "tests", "*.py")))
for f in files:
    b = os.path.basename(f)
    n = sum(1 for _ in open(f, encoding="utf-8"))
    for name, pat in groups:
        if re.search(pat, b):
            tot.setdefault(name, [0, 0]); tot[name][0] += n; tot[name][1] += 1; seen.add(b); break
    else:
        tot.setdefault("other", [0, 0]); tot["other"][0] += n; tot["other"][1] += 1
        print("unassigned:", b)
fx = sum(sum(1 for _ in open(f, encoding="utf-8")) for f in glob.glob(os.path.join(root, "tests", "fixtures", "**", "*"), recursive=True) if os.path.isfile(f))
for k, (n, c) in tot.items():
    print(f"{k:32s} {c:3d} files {n:6d} lines")
print("fixtures (json captures):", fx)
print("total py test lines:", sum(v[0] for v in tot.values()))
