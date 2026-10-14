"""The fork tooling (scripts/sync_from_codedoc_go.sh, scripts/leiden_hooks.py,
scripts/leiden_owned.txt): every hook is applied once, applying is
idempotent and stops on a lost anchor, the owned list protects every file
codedoc_go does not have, and the Phase 2 cache inputs stay codedoc_go's."""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess

import pytest

from scripts import leiden_hooks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.join(os.path.dirname(ROOT), "codedoc_go")
needs_parent = pytest.mark.skipif(not os.path.isfile(os.path.join(PARENT, "graph_generator", "pipeline.py")),
                                  reason="codedoc_go is not next to this directory")


def test_every_hook_is_applied_once():
    assert leiden_hooks.verify(ROOT) == []


def test_apply_is_idempotent_and_stops_on_a_lost_anchor(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("X = 1\nY = 2\n")
    (tmp_path / "b.txt").write_text("pins\n")
    hooks = [leiden_hooks.Hook("x", "a.py", "X = 1\n", "X = 3  # LEIDEN-HOOK\n"),
             leiden_hooks.Hook("tail", "b.txt", None, "extra  # LEIDEN-HOOK\n")]
    monkeypatch.setattr(leiden_hooks, "HOOKS", hooks)
    assert leiden_hooks.apply(str(tmp_path)) == []
    assert leiden_hooks.apply(str(tmp_path)) == []
    assert (tmp_path / "a.py").read_text() == "X = 3  # LEIDEN-HOOK\nY = 2\n"
    assert (tmp_path / "b.txt").read_text() == "pins\nextra  # LEIDEN-HOOK\n"
    assert leiden_hooks.verify(str(tmp_path)) == []
    (tmp_path / "a.py").write_text("X = 2\nY = 2\n")  # codedoc_go changed the anchor
    problems = leiden_hooks.apply(str(tmp_path))
    assert problems == ["x (a.py): the anchor occurs 0 times, expected once; "
                        "re-anchor this hook in scripts/leiden_hooks.py"]
    assert leiden_hooks.verify(str(tmp_path)) == ["x (a.py): applied 0 times, expected once",
                                                  "a.py: 0 LEIDEN-HOOK markers, 1 hooks"]


def test_verify_reports_a_hooked_file_that_does_not_compile(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("X = (  # LEIDEN-HOOK\n")
    monkeypatch.setattr(leiden_hooks, "HOOKS", [leiden_hooks.Hook("x", "a.py", "X\n", "X = (  # LEIDEN-HOOK\n")])
    assert any("does not compile" in p for p in leiden_hooks.verify(str(tmp_path)))


def test_hook_definitions_carry_one_marker_each():
    assert leiden_hooks._check_definitions() == []


def _owned_patterns() -> list[str]:
    with open(os.path.join(ROOT, "scripts", "leiden_owned.txt"), encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def _owned(rel: str, patterns: list[str]) -> bool:
    """rsync's exclude semantics for the patterns the list uses: a leading /
    anchors at the root, a trailing / names a directory, * stays in one part."""
    parts = rel.split("/")
    for pattern in patterns:
        pp = pattern.strip("/").split("/")
        is_dir = pattern.endswith("/")
        starts = [0] if pattern.startswith("/") else range(len(parts))
        for start in starts:
            window = parts[start:start + len(pp)]
            if len(window) < len(pp) or not all(fnmatch.fnmatchcase(a, b) for a, b in zip(window, pp)):
                continue
            if not is_dir or start + len(pp) < len(parts):
                return True
    return False


@needs_parent
def test_the_owned_list_protects_every_file_codedoc_go_does_not_have():
    """rsync --delete removes every unlisted file that codedoc_go lacks."""
    patterns = _owned_patterns()
    unprotected = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".venv", "node_modules", ".git")]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), ROOT).replace(os.sep, "/")
            if not os.path.lexists(os.path.join(PARENT, rel)) and not _owned(rel, patterns):
                unprotected.append(rel)
    assert unprotected == []


def test_the_owned_list_names_the_leiden_modules():
    patterns = _owned_patterns()
    for rel in ("graph_generator/topics/community.py", "graph_generator/topics_phase.py",
                "graph_generator/topics_schema.py", "tests/test_topics_community.py",
                "tests/test_leiden_hooks.py", "tests/leiden_plugin.py", "tests/leiden_worlds.py",
                "scripts/leiden_hooks.py", "scripts/sync_from_codedoc_go.sh", ".env",
                "graph_query_agent/.env", "webapp/frontend/.env",
                "docs/superpowers/specs/x.md", "output_docs_pipeline/repos/r/units.json",
                "webapp/frontend/node_modules/x/y.js", "graph_query_agent/.adk/session.db",
                "sample_prompt.txt", "README.md", "CLAUDE.md",
                "docs/ARCHITECTURE.md", "docs/GRAPH_SCHEMA.md", "mcp_server/README.md"):
        assert _owned(rel, patterns), rel
    for rel in ("graph_generator/pipeline.py", "tests/test_tracks.py", "docs/LSP_RESOLUTION.md",
                "docs/CROSS_PROJECT.md", "test_codes/iryo-kanri-system/go.work", ".env.example",
                "graph_query_agent/.env.example"):
        assert not _owned(rel, patterns), rel


@pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")
def test_the_sync_neither_copies_nor_deletes_a_nested_env(tmp_path):
    """rsync itself, with the real owned list: codedoc_go's .env files at any
    depth stay out, this variant's own nested .env files stay in, and
    .env.example still syncs."""
    parent, leiden = tmp_path / "codedoc_go", tmp_path / "codedoc_go_leiden"
    for tree, rels in ((parent, (".env", "graph_query_agent/.env", "webapp/frontend/.env", ".env.example")),
                       (leiden, (".env", "webapp/frontend/.env", "graph_query_agent/only_here/.env"))):
        for rel in rels:
            path = tree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"from {tree.name}\n")
    subprocess.run(["rsync", "-a", "--delete", f"--exclude-from={ROOT}/scripts/leiden_owned.txt",
                    f"{parent}/", f"{leiden}/"], check=True)
    for rel in (".env", "webapp/frontend/.env", "graph_query_agent/only_here/.env"):
        assert (leiden / rel).read_text() == "from codedoc_go_leiden\n", rel
    assert not (leiden / "graph_query_agent" / ".env").exists()
    assert (leiden / ".env.example").read_text() == "from codedoc_go\n"


def test_the_sync_script_is_valid_bash():
    subprocess.run(["bash", "-n", os.path.join(ROOT, "scripts", "sync_from_codedoc_go.sh")], check=True)


def test_the_sync_script_refuses_any_other_tree(tmp_path):
    """A copy of the script in another tree (say codedoc_go_v2_leiden) never runs rsync."""
    copy = tmp_path / "codedoc_go_v2_leiden" / "scripts"
    copy.mkdir(parents=True)
    for name in ("sync_from_codedoc_go.sh", "leiden_owned.txt"):
        shutil.copy(os.path.join(ROOT, "scripts", name), copy / name)
    proc = subprocess.run(["bash", str(copy / "sync_from_codedoc_go.sh"), "--dry-run"],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "is not codedoc_go_leiden" in proc.stderr


@needs_parent
@pytest.mark.parametrize("rel", ["graph_generator/prompts.py", "graph_generator/node_summaries.py"])
def test_the_phase_2_cache_inputs_are_codedoc_gos(rel):
    """R11: both variants share WIKI_SUMMARY_CACHE, keyed by PROMPT_VERSION and
    the prompts; a leiden edit here would split the cache silently."""
    with open(os.path.join(ROOT, rel), encoding="utf-8") as a, \
            open(os.path.join(PARENT, rel), encoding="utf-8") as b:
        assert a.read() == b.read()
