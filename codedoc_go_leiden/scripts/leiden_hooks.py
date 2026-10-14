#!/usr/bin/env python3
"""Apply or verify the LEIDEN-HOOK edits in the files codedoc_go_leiden shares
with codedoc_go.

scripts/sync_from_codedoc_go.sh copies codedoc_go over this directory, except
the leiden-owned paths in scripts/leiden_owned.txt, and then runs `apply` and
`verify`. Each hook replaces one exact anchor text with a text that carries the
marker LEIDEN-HOOK once (or appends a block at the end of a file). Applying is
idempotent. An anchor that is missing or not unique stops the sync with the
hook's name, so a change in codedoc_go never drops a hook silently: re-anchor
the hook here, then sync again.

The leiden logic itself lives in leiden-only modules (graph_generator/topics/,
graph_generator/topics_phase.py, graph_generator/topics_schema.py); the hooks
only call into them or change a variant default.

Usage: python3 scripts/leiden_hooks.py apply|verify [--root DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

MARKER = "LEIDEN-HOOK"


@dataclass(frozen=True)
class Hook:
    name: str
    path: str        # relative to the codedoc_go_leiden root
    old: str | None  # exact anchor text; None appends `new` at the end of the file
    new: str


HOOKS: list[Hook] = [
    # ── graph_generator/config.py ─────────────────────────────────────
    Hook("config: database default", "graph_generator/config.py",
         'SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-go")\n',
         'SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-go-leiden")  # LEIDEN-HOOK\n'),
    Hook("config: topic settings", "graph_generator/config.py",
         'SOURCE_EXTENSIONS = {".go"}\n',
         'SOURCE_EXTENSIONS = {".go"}\n'
         '\n'
         '# ── Leiden topics (leiden spec §3.3, §7) ── LEIDEN-HOOK ─────────\n'
         '# Integers, read as text: topics_phase.topic_params checks them, so a bad value is a\n'
         '# preflight error (exit 3) in every mode, doctor included, naming the key, the\n'
         '# value and the allowed form.\n'
         '# The level-1 topic count band of the resolution sweep.\n'
         'TOPIC_MIN = _env("TOPIC_MIN", "5")\n'
         'TOPIC_MAX = _env("TOPIC_MAX", "25")\n'
         '# A level-1 topic with more primary atoms is split into subtopics.\n'
         'SUBTOPIC_MIN_ATOMS = _env("SUBTOPIC_MIN_ATOMS", "12")\n'
         '# "strict", or a "+" combination of degree_norm, calls_only, no_implements, with_tests.\n'
         'TOPIC_PROFILE = _env("TOPIC_PROFILE", "strict")\n'
         '# Comma-separated Leiden seeds; checked by the preflight.\n'
         'TOPIC_SEEDS = _env("TOPIC_SEEDS", "42,43,44,45,46")\n'),

    # ── graph_generator/pipeline.py ───────────────────────────────────
    Hook("pipeline: preflight rows", "graph_generator/pipeline.py",
         "    if wiki:\n"
         "        checks.append(_check_gemini(soft=doctor))\n"
         "    return checks, toolchain\n",
         "    if wiki:\n"
         "        checks.append(_check_gemini(soft=doctor))\n"
         "    checks += topics_phase.preflight_checks(mode)  # LEIDEN-HOOK: Leiden backend, topic settings\n"
         "    return checks, toolchain\n"),
    Hook("pipeline: checkpoint stores the communities hash", "graph_generator/pipeline.py",
         '"communities_hash": None})\n',
         '"communities_hash": topics_phase.communities_hash(data)})  # LEIDEN-HOOK\n'),
    Hook("pipeline: checkpoint compares the communities hash", "graph_generator/pipeline.py",
         '            or cp.get("spanner") != _spanner_target(data.no_spanner)):\n',
         '            or cp.get("spanner") != _spanner_target(data.no_spanner)\n'
         '            or cp.get("communities_hash") != topics_phase.communities_hash(data)):  # LEIDEN-HOOK\n'),
    Hook("pipeline: Phase 4a before Phase 8 (L-R24)", "graph_generator/pipeline.py",
         "    data.resolutions_digest = resolutions_hash(data.resolutions)\n"
         "    last = _load_graph_checkpoint(data)\n",
         "    data.resolutions_digest = resolutions_hash(data.resolutions)\n"
         "    topics_phase.phase4a_communities(data)  # LEIDEN-HOOK: Phase 4a (L-R24)\n"
         "    last = _load_graph_checkpoint(data)\n"),
    Hook("pipeline: topic tables after Phase 9", "graph_generator/pipeline.py",
         "            raise\n"
         '        _save_graph_checkpoint(data, "phase9", complete=True)\n',
         "            raise\n"
         "        topics_phase.write_graph_topics(data)  # LEIDEN-HOOK: the five topic tables (R10)\n"
         '        _save_graph_checkpoint(data, "phase9", complete=True)\n'),
    Hook("pipeline: --wiki needs matching communities", "graph_generator/pipeline.py",
         "    if problems:\n"
         '        raise WikiPreconditionError("\\n\\n".join(problems))\n'
         "    data.no_spanner = no_spanner\n",
         "    problems += topics_phase.wiki_problems(data, cp)  # LEIDEN-HOOK: communities (leiden spec §2)\n"
         "    if problems:\n"
         '        raise WikiPreconditionError("\\n\\n".join(problems))\n'
         "    data.no_spanner = no_spanner\n"),
    Hook("pipeline: call estimate for 4b and 5", "graph_generator/pipeline.py",
         '            "phase4": len(chunks) + 1, "phase5": 15}\n',
         '            "phase4": len(chunks) + 1, "phase5": 15,\n'
         "            **topics_phase.estimate_calls(data)}  # LEIDEN-HOOK: 4b batches, 5 pages\n"),
    Hook("pipeline: wiki track runs 4b", "graph_generator/pipeline.py",
         "    await phase4_topics(data, client)\n",
         "    await topics_phase.phase4b_naming(data, client)  # LEIDEN-HOOK: 4b replaces Phase 4\n"),
    Hook("pipeline: wiki track runs the reports", "graph_generator/pipeline.py",
         "    await phase5_topic_pages(data, client)\n",
         "    await topics_phase.phase5_reports(data, client)  # LEIDEN-HOOK: Phase 5 from structural evidence\n"),
    Hook("pipeline: Phase 6 keeps subtopic pages", "graph_generator/pipeline.py",
         "    current = {f\"{t['id']}.md\" for t in data.topics}\n",
         "    current = {f\"{t['id']}.md\" for t in data.topics} | topics_phase.page_names(data)  # LEIDEN-HOOK\n"),
    Hook("pipeline: Phase 10 topic rows", "graph_generator/pipeline.py",
         "    topics, belongs = _topic_rows(data, rows)\n",
         "    topics, belongs = topics_phase.wiki_topic_rows(data, rows)  # LEIDEN-HOOK\n"),
    Hook("pipeline: Phase 10 replaces the topic tables", "graph_generator/pipeline.py",
         "    _replace_topics(db, data.repo, topics, belongs)\n",
         '    topics_phase.replace_topic_tables(db, data, "Phase 10")  # LEIDEN-HOOK: batched, not atomic\n'),
    Hook("pipeline: import topics_phase", "graph_generator/pipeline.py", None,
         "\n\n"
         "# The leiden-only phases (Phase 4a, topic tables, 4b, 5 reports); imported last because\n"
         "# topics_phase uses this module.\n"
         "from . import topics_phase  # noqa: E402  LEIDEN-HOOK\n"),

    # ── graph_generator/__main__.py ───────────────────────────────────
    Hook("cli: docstring graph track", "graph_generator/__main__.py",
         "    analyze <target_dir> --graph      Graph track: Phases 1, 1.5, 1.6, 8, 9 (no LLM)\n",
         "    analyze <target_dir> --graph      Graph track: Phases 1, 1.5, 1.6, 4a, 8, 9 (no LLM) [LEIDEN-HOOK]\n"),
    Hook("cli: docstring wiki track", "graph_generator/__main__.py",
         "    analyze <target_dir> --wiki       Wiki track on a finished graph: Phases 2-6, 10\n",
         "    analyze <target_dir> --wiki       Wiki track on a finished graph: Phases 2, 3, 4b, 5, 6, 10 [LEIDEN-HOOK]\n"),
    Hook("cli: docstring compare-topics", "graph_generator/__main__.py",
         "    trial <target_dir> --repo-name N  Real-repo trial: graph track and oracle precision check\n",
         "    trial <target_dir> --repo-name N  Real-repo trial: graph track and oracle precision check\n"
         "    compare-topics --llm DIR --leiden DIR   Compare codedoc_go's and the Leiden topics [LEIDEN-HOOK]\n"),
    Hook("cli: import topics_phase", "graph_generator/__main__.py",
         "from .pipeline import PipelineData, PreflightError, WikiPreconditionError, _get_spanner_db\n",
         "from .pipeline import PipelineData, PreflightError, WikiPreconditionError, _get_spanner_db\n"
         "from . import topics_phase  # LEIDEN-HOOK\n"),
    Hook("cli: timing labels", "graph_generator/__main__.py",
         '    ("phase10_write_back", "Phase 10: Write-back and embeddings"),\n]\n',
         '    ("phase10_write_back", "Phase 10: Write-back and embeddings"),\n]\n'
         "_PHASE_LABELS = topics_phase.phase_labels(_PHASE_LABELS)  # LEIDEN-HOOK: 4a, topic tables, 4b\n"),
    Hook("cli: analyze banner", "graph_generator/__main__.py",
         '    print(f"  CodeDoc (Go) — analyze --{mode} (repo: {data.repo})")\n',
         '    print(f"  CodeDoc (Go, Leiden topics) — analyze --{mode} (repo: {data.repo})")  # LEIDEN-HOOK\n'),
    Hook("cli: doctor banner", "graph_generator/__main__.py",
         '    print("  CodeDoc (Go) — doctor")\n',
         '    print("  CodeDoc (Go, Leiden topics) — doctor")  # LEIDEN-HOOK\n'),
    Hook("cli: description", "graph_generator/__main__.py",
         '        description="CodeDoc (Go): a code graph in Spanner and a Japanese wiki for Go modules",\n',
         '        description="CodeDoc (Go, Leiden topics): a code graph in Spanner and a Japanese wiki "\n'
         '                    "for Go modules",  # LEIDEN-HOOK\n'),
    Hook("cli: analyze description", "graph_generator/__main__.py",
         '        description="Graph track: Phases 1, 1.5, 1.6, 8, 9 (no LLM). Wiki track: Phases 2-6, "\n'
         '                    "10 on a finished --graph of the same files.", epilog=EXIT_CODES)\n',
         '        description="Graph track: Phases 1, 1.5, 1.6, 4a, 8, 9 (no LLM). Wiki track: Phases 2, 3, "\n'
         '                    "4b, 5, 6, 10 on a finished --graph of the same files.", epilog=EXIT_CODES)  # LEIDEN-HOOK\n'),
    Hook("cli: --graph help", "graph_generator/__main__.py",
         '                       help="Graph track: Phases 1, 1.5, 1.6, 8, 9 (no LLM calls)")\n',
         '                       help="Graph track: Phases 1, 1.5, 1.6, 4a, 8, 9 (no LLM calls)")  # LEIDEN-HOOK\n'),
    Hook("cli: --wiki help", "graph_generator/__main__.py",
         '                       help="Wiki track on a finished --graph: Phases 2-6, 10 (Gemini)")\n',
         '                       help="Wiki track on a finished --graph: Phases 2, 3, 4b, 5, 6, 10 (Gemini)")'
         '  # LEIDEN-HOOK\n'),
    Hook("cli: compare-topics parser", "graph_generator/__main__.py",
         "    return parser\n\n\n_HANDLERS = {",
         "    topics_phase.add_compare_parser(subparsers)  # LEIDEN-HOOK: compare-topics (leiden spec §6)\n"
         "    return parser\n\n\n_HANDLERS = {"),
    Hook("cli: compare-topics handler", "graph_generator/__main__.py",
         '_SETUP_HANDLERS = {"go": "cmd_setup_go", "spanner": "cmd_setup_spanner"}\n',
         '_SETUP_HANDLERS = {"go": "cmd_setup_go", "spanner": "cmd_setup_spanner"}\n'
         '_HANDLERS["compare-topics"] = "cmd_compare_topics"\n'
         "cmd_compare_topics = topics_phase.cmd_compare_topics  # LEIDEN-HOOK\n"),

    # ── graph_generator/setup_spanner_graph.py ────────────────────────
    Hook("schema: topic tables (leiden spec §4)", "graph_generator/setup_spanner_graph.py",
         "NODE_TABLES = list(NODE_SPEC)\n",
         "from . import topics_schema  # noqa: E402\n"
         "NODE_SPEC, EDGE_SPEC = topics_schema.extend(NODE_SPEC, EDGE_SPEC)  # LEIDEN-HOOK: leiden spec §4\n"
         "\n"
         "NODE_TABLES = list(NODE_SPEC)\n"),

    # ── query surfaces and deployment ─────────────────────────────────
    Hook("agent: database default", "graph_query_agent/config.py",
         'SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-go")\n',
         'SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-go-leiden")  # LEIDEN-HOOK\n'),
    Hook("agent: topic source", "graph_query_agent/agent.py",
         'TOPIC_SOURCE = "llm"\n',
         'TOPIC_SOURCE = "leiden"  # LEIDEN-HOOK\n'),
    Hook("deploy: database comment", "mcp_server/deploy.sh",
         "#   SPANNER_DATABASE          env, .env, fallback codedoc-go (the pipeline's default)\n",
         "#   SPANNER_DATABASE          env, .env, fallback codedoc-go-leiden (the pipeline's default) [LEIDEN-HOOK]\n"),
    Hook("deploy: service comment", "mcp_server/deploy.sh",
         "#   SERVICE_NAME              Cloud Run service (fallback: codedoc-go-mcp)\n",
         "#   SERVICE_NAME              Cloud Run service (fallback: codedoc-go-leiden-mcp) [LEIDEN-HOOK]\n"),
    Hook("deploy: service", "mcp_server/deploy.sh",
         'SERVICE_NAME="${SERVICE_NAME:-codedoc-go-mcp}"\n',
         'SERVICE_NAME="${SERVICE_NAME:-codedoc-go-leiden-mcp}"  # LEIDEN-HOOK\n'),
    Hook("deploy: database", "mcp_server/deploy.sh",
         'SPANNER_DATABASE="${SPANNER_DATABASE:-codedoc-go}"\n',
         'SPANNER_DATABASE="${SPANNER_DATABASE:-codedoc-go-leiden}"  # LEIDEN-HOOK\n'),
    Hook("deploy: image", "mcp_server/deploy.sh",
         'IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/codedoc-go-mcp"\n',
         'IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/codedoc-go-leiden-mcp"  # LEIDEN-HOOK\n'),
    Hook("cloudbuild: image example", "mcp_server/cloudbuild.yaml",
         "#              e.g. asia-northeast1-docker.pkg.dev/PROJECT/codedoc/codedoc-go-mcp\n",
         "#              e.g. asia-northeast1-docker.pkg.dev/PROJECT/codedoc/codedoc-go-leiden-mcp [LEIDEN-HOOK]\n"),
    Hook("cloudbuild: manual invocation", "mcp_server/cloudbuild.yaml",
         "#     --substitutions _IMAGE=REGION-docker.pkg.dev/PROJECT/REPO/codedoc-go-mcp,SHORT_SHA=manual\n",
         "#     --substitutions _IMAGE=REGION-docker.pkg.dev/PROJECT/REPO/codedoc-go-leiden-mcp,SHORT_SHA=manual\n"
         "#   (LEIDEN-HOOK: codedoc_go_leiden's image name)\n"),
    Hook("env example: header", ".env.example",
         "# CodeDoc (Go) Configuration\n",
         "# CodeDoc (Go, Leiden topics) Configuration [LEIDEN-HOOK]\n"),
    Hook("env example: database", ".env.example",
         "SPANNER_DATABASE=codedoc-go\n",
         "# LEIDEN-HOOK: codedoc_go_leiden's own database\n"
         "SPANNER_DATABASE=codedoc-go-leiden\n"),
    Hook("env example: topic settings", ".env.example",
         "# --- Peers ---\n",
         "# --- Leiden topics --- [LEIDEN-HOOK]\n"
         "# Level-1 topic count band of the resolution sweep\n"
         "TOPIC_MIN=5\n"
         "TOPIC_MAX=25\n"
         "# A level-1 topic with more primary atoms is split into subtopics\n"
         "SUBTOPIC_MIN_ATOMS=12\n"
         "# strict, or a + combination of degree_norm, calls_only, no_implements, with_tests\n"
         "TOPIC_PROFILE=strict\n"
         "# Leiden seeds, comma-separated\n"
         "TOPIC_SEEDS=42,43,44,45,46\n"
         "\n"
         "# --- Peers ---\n"),
    Hook("skill: variant", ".claude/skills/codedoc-query/SKILL.md",
         "The agent translates questions into GQL over this schema (the Go variant of\n"
         "CodeDoc, `codedoc_go`):\n",
         "The agent translates questions into GQL over this schema (the Go variant of\n"
         "CodeDoc with Leiden topics, `codedoc_go_leiden`): <!-- LEIDEN-HOOK -->\n"),
    Hook("skill: edge list", ".claude/skills/codedoc-query/SKILL.md",
         "- **Edges** (14): `PackageImports`, `FileDependsOn`, `ClassEmbeds`,\n"
         "  `ClassImplements`, `MethodCalls`, `PossiblyCalls`, `FileDefinesClass`,\n"
         "  `ClassDefinesMethod`, `DirContainsFile`, `PackageContainsFile`,\n"
         "  `FileBelongsToTopic`, `CrossRepoCalls`, `CrossRepoRef`, `CrossRepoImports`.\n",
         "- **Edges** (17): `PackageImports`, `FileDependsOn`, `ClassEmbeds`,\n"
         "  `ClassImplements`, `MethodCalls`, `PossiblyCalls`, `FileDefinesClass`,\n"
         "  `ClassDefinesMethod`, `DirContainsFile`, `PackageContainsFile`,\n"
         "  `FileBelongsToTopic`, `ClassBelongsToTopic`, `TopicDependsOn`,\n"
         "  `TopicContainsTopic`, `CrossRepoCalls`, `CrossRepoRef`, `CrossRepoImports`. <!-- LEIDEN-HOOK -->\n"),
    Hook("skill: topics", ".claude/skills/codedoc-query/SKILL.md",
         "- **`Topics` are functional topics** that Gemini groups files into, not Go\n"
         "  modules. Ask about a module by its module path, not as a topic.\n",
         "- **`Topics` are structural topics**, not Go modules: Leiden community\n"
         "  detection groups types (and each file's `<package>` pseudo-type) by their\n"
         "  confirmed calls, embeds and implements; Gemini only names the groups.\n"
         "  Level-1 topics can split into level-2 subtopics (`TopicContainsTopic`);\n"
         "  `ClassBelongsToTopic` gives a type's topic at each level with its role\n"
         "  (`primary`, `attached-*`, or `unassigned` for types placed in no topic).\n"
         "  `TopicDependsOn` links leaf topics by confirmed calls, embeds and\n"
         "  implements; its `call_count` counts distinct caller-callee method pairs and\n"
         "  is 0 when only embeds or implements link them. Ask about a module by its\n"
         "  module path, not as a topic. <!-- LEIDEN-HOOK -->\n"),
    Hook("skill: topic question", ".claude/skills/codedoc-query/SKILL.md",
         '  - "give a high-level overview of this codebase" / "what does the `<topic>` topic cover?"\n',
         '  - "give a high-level overview of this codebase" / "what does the `<topic>` topic cover?"\n'
         '  - "which topic does `<import/path.TypeName>` belong to, and which topics does that topic depend on?"'
         ' <!-- LEIDEN-HOOK -->\n'),

    # ── dependencies and tests ────────────────────────────────────────
    Hook("requirements: Leiden backend", "requirements.txt", None,
         "# LEIDEN-HOOK: Phase 4a needs graspologic-native (which needs numpy and scipy) and networkx\n"
         "graspologic-native==1.3.1\n"
         "networkx==3.7\n"
         "numpy==2.5.4\n"
         "scipy==1.18.1\n"),
    Hook("conftest: topic settings are configuration", "tests/conftest.py",
         '_CONFIG_ENV_PREFIXES = ("SPANNER_", "GO_", "WIKI_", "EMBED_", "GEMINI_", "GOOGLE_CLOUD_", "GOOGLE_GENAI_")\n',
         '_CONFIG_ENV_PREFIXES = ("SPANNER_", "GO_", "WIKI_", "EMBED_", "GEMINI_", "GOOGLE_CLOUD_", "GOOGLE_GENAI_")\n'
         '_CONFIG_ENV_PREFIXES += ("TOPIC_", "SUBTOPIC_")  # LEIDEN-HOOK: the topic settings\n'),
    Hook("pytest: leiden plugin", "pytest.ini",
         '\naddopts = -m "not go and not network" --cov=graph_generator',
         "\n# LEIDEN-HOOK: tests/leiden_plugin.py skips the codedoc_go tests whose expectations are codedoc_go's own\n"
         'addopts = -m "not go and not network" -p tests.leiden_plugin --cov=graph_generator'),
]


def _read(root: str, rel: str) -> str:
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def _write(root: str, rel: str, text: str) -> None:
    path = os.path.join(root, rel)
    tmp = path + ".leiden-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, os.stat(path).st_mode)
    os.replace(tmp, path)


def _check_definitions() -> list[str]:
    problems = []
    names = [h.name for h in HOOKS]
    problems += [f"duplicate hook name {n!r}" for n in sorted({n for n in names if names.count(n) > 1})]
    for h in HOOKS:
        if h.new.count(MARKER) != 1:
            problems.append(f"{h.name}: the new text must carry {MARKER} exactly once")
        if h.old is not None and MARKER in h.old:
            problems.append(f"{h.name}: the anchor must not carry {MARKER}")
    return problems


def hooked_text(rel: str, text: str, problems: list[str] | None = None) -> str:
    """The text of file `rel` (codedoc_go's, or this tree's) with every hook
    of that file applied, in HOOKS order. A hook already in the text is left
    alone; an anchor that is not there exactly once goes to `problems`."""
    for h in HOOKS:
        if h.path != rel or h.new in text:
            continue
        if h.old is None:
            text = (text if text.endswith("\n") or not text else text + "\n") + h.new
            continue
        n = text.count(h.old)
        if n != 1:
            if problems is not None:
                problems.append(f"{h.name} ({rel}): the anchor occurs {n} times, expected once; "
                                "re-anchor this hook in scripts/leiden_hooks.py")
            continue
        text = text.replace(h.old, h.new)
    return text


def apply(root: str) -> list[str]:
    """Apply every hook that is not applied yet; returns the problems."""
    problems = _check_definitions()
    if problems:
        return problems
    for rel in dict.fromkeys(h.path for h in HOOKS):
        try:
            text = _read(root, rel)
        except OSError as exc:
            problems.append(f"{rel}: {exc}")
            continue
        hooked = hooked_text(rel, text, problems)
        if hooked != text:
            _write(root, rel, hooked)
    return problems


def verify(root: str) -> list[str]:
    """Every hook is applied once, no file carries an unknown marker, and the
    hooked Python files compile."""
    problems = _check_definitions()
    by_file: dict[str, list[Hook]] = {}
    for h in HOOKS:
        by_file.setdefault(h.path, []).append(h)
    for rel, hooks in sorted(by_file.items()):
        try:
            text = _read(root, rel)
        except OSError as exc:
            problems.append(f"{rel}: {exc}")
            continue
        for h in hooks:
            n = text.count(h.new)
            if n != 1:
                problems.append(f"{h.name} ({rel}): applied {n} times, expected once")
        if text.count(MARKER) != len(hooks):
            problems.append(f"{rel}: {text.count(MARKER)} {MARKER} markers, {len(hooks)} hooks")
        if rel.endswith(".py"):
            try:
                compile(text, rel, "exec")
            except SyntaxError as exc:
                problems.append(f"{rel}: does not compile: {exc}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("apply", "verify"))
    parser.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        help="the codedoc_go_leiden directory (default: this script's parent)")
    args = parser.parse_args(argv)
    problems = (apply if args.action == "apply" else verify)(args.root)
    for p in problems:
        print(f"leiden_hooks {args.action}: {p}", file=sys.stderr)
    if not problems:
        files = len({h.path for h in HOOKS})
        print(f"leiden_hooks {args.action}: {len(HOOKS)} hooks in {files} files ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
