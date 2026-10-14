"""Entry point: python -m graph_generator <command> [args]

Commands:
    init                              Interactive .env setup
    setup go                          Install the pinned Go toolchain into .tools/go
    setup spanner                     Create Spanner instance + database + tables + graph
    doctor [target_dir]               Preflight checks, printed as a table

    analyze <target_dir> --graph      Graph track: Phases 1, 1.5, 1.6, 8, 9 (no LLM)
    analyze <target_dir> --wiki       Wiki track on a finished graph: Phases 2-6, 10
    analyze <target_dir> --all        The graph track, then the wiki track

    validate                          Validate Spanner graph data
    crossref [--dry-run]              Cross-repo edges, after every repo is ingested
    evaluate [--fixture NAME] [--env] GCP-free, network-free regression gate on test_codes/
    trial <target_dir> --repo-name N  Real-repo trial: graph track and oracle precision check

Exit codes: 0 ok, 1 other errors, 2 usage, 3 preflight failure, 4 --wiki precondition failure.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time

from . import config, pipeline
from .pipeline import PipelineData, PreflightError, WikiPreconditionError, _get_spanner_db

EVAL_FIXTURES = ("iryo-kanri-system", "go_layouts", "multi_repo")
# `setup go` installs here; go_toolchain.find_toolchain looks here when GO_TOOLCHAIN is unset.
TOOLS_GO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tools", "go")
EXIT_CODES = "Exit codes: 0 ok, 1 other errors, 2 usage, 3 preflight failure, 4 --wiki precondition failure."


class UsageError(Exception):
    """Bad command-line input; exit code 2 (C9)."""


_PHASE_LABELS = [
    ("phase1_scan", "Phase 1: Scan and discovery"),
    ("phase1b_extract", "Phase 1.5: Extraction"),
    ("phase1c_resolve", "Phase 1.6: Resolution"),
    ("phase8_write_nodes", "Phase 8: Write graph nodes"),
    ("phase9_write_edges", "Phase 9: Write graph edges"),
    ("phase1_rescan", "Phase 1: Re-scan"),
    ("phase2_node_summaries", "Phase 2: Node summaries"),
    ("phase3_package_dir_summaries", "Phase 3: Package and dir summaries"),
    ("phase4_topics", "Phase 4: Topics"),
    ("phase5_topic_pages", "Phase 5: Topic pages"),
    ("phase6_index", "Phase 6: Wiki index"),
    ("phase10_write_back", "Phase 10: Write-back and embeddings"),
]


def _print_timing_report(data, total_elapsed: float):
    """Print timing report and statistics."""
    print("\n" + "=" * 70)
    print("  TIMING REPORT")
    print("=" * 70)

    for key, label in _PHASE_LABELS:
        if key in data.timings:
            t = data.timings[key]
            mins = int(t // 60)
            secs = t % 60
            if mins > 0:
                print(f"  {label:<38} {mins}m {secs:05.2f}s")
            else:
                print(f"  {label:<38} {secs:.2f}s")

    print("  " + "-" * 48)
    total_mins = int(total_elapsed // 60)
    total_secs = total_elapsed % 60
    if total_mins > 0:
        print(f"  {'TOTAL':<38} {total_mins}m {total_secs:05.2f}s")
    else:
        print(f"  {'TOTAL':<38} {total_secs:.2f}s")
    print("=" * 70)

    by_status = (data.resolutions or {}).get("stats", {}).get("by_status", {})
    statistics = {
        "files_scanned": len(data.file_hashes),
        "entities_extracted": len(data.extracted_entities),
        "sites_by_status": dict(by_status),
        "rows_written": dict(data.graph_counts),
        "file_summaries": len(data.file_summaries),
        "node_summaries": sum(len(nodes) for nodes in data.node_summaries.values()),
        "package_summaries": len(data.package_summaries),
        "dir_summaries": len(data.dir_summaries),
        "topics": len(data.topics),
    }
    print(f"\n  Files scanned:       {statistics['files_scanned']}")
    print(f"  Entities extracted:  {statistics['entities_extracted']}")
    print("  Sites by status:     " + (", ".join(f"{s}={n}" for s, n in by_status.items()) or "-"))
    print("  Rows written:        " + (", ".join(f"{t}={n}" for t, n in data.graph_counts.items())
                                       or "-"))
    print(f"  Summaries:           files={statistics['file_summaries']}, "
          f"nodes={statistics['node_summaries']}, packages={statistics['package_summaries']}, "
          f"dirs={statistics['dir_summaries']}")
    print(f"  Topics:              {statistics['topics']}")
    if data.gemini_usage:  # R49: thinking tokens apart from the answer's, both billed as output
        labels = dict(_PHASE_LABELS)
        total = {key: sum(u.get(key, 0) for u in data.gemini_usage.values())
                 for key in pipeline.USAGE_KEYS}
        statistics["gemini"] = {**data.gemini_usage, "total": total}
        print("\n  Gemini calls and tokens (input / output / thinking):")
        for phase, u in [*data.gemini_usage.items(), ("total", total)]:
            label = "TOTAL" if phase == "total" else labels.get(phase, phase)
            print(f"  {label:<38} {u.get('calls', 0):>5} calls  {u.get('input_tokens', 0):,} / "
                  f"{u.get('output_tokens', 0):,} / {u.get('thinking_tokens', 0):,}")

    # Save timing report
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    os.makedirs(out_root, exist_ok=True)
    report = {
        "target_dir": data.target_dir,
        "repo": data.repo,
        "model": config.MODEL,
        "concurrency": config.GEMINI_CONCURRENCY,
        "total_seconds": total_elapsed,
        "timings": data.timings,
        "statistics": statistics,
    }
    report_path = os.path.join(out_root, "timing_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Timing report saved: {report_path}")


def _project_label() -> str:
    """GOOGLE_CLOUD_PROJECT, else the ADC / gcloud default project as resolved
    by google.auth. There is no hardcoded default (master Global Constraints)."""
    if config.GCP_PROJECT:
        return config.GCP_PROJECT
    try:
        import google.auth
        _credentials, project = google.auth.default()
    except Exception:
        project = None
    return f"{project} (ADC / gcloud default)" if project else "ADC / gcloud default (unresolved)"


def _check_config():
    """Print the GCP project this run uses. GOOGLE_CLOUD_PROJECT has no default:
    when it is unset, the Google client libraries use the ADC / gcloud project."""
    print(f"  Project:     {_project_label()}")


def cmd_init(args):
    """Interactive setup — generate .env from template."""
    env_path = os.path.join(os.getcwd(), ".env")

    if os.path.exists(env_path) and not getattr(args, "force", False):
        print(f".env already exists at {env_path}")
        print("  Use --force to overwrite")
        return

    print("=" * 60)
    print("  CodeDoc Setup")
    print("=" * 60)

    project = input(f"  GCP Project ID [{_project_label()}]: ").strip()
    project = project or config.GCP_PROJECT or ""

    instance = input(f"  Spanner Instance [{config.SPANNER_INSTANCE}]: ").strip()
    instance = instance or config.SPANNER_INSTANCE

    database = input(f"  Spanner Database [{config.SPANNER_DATABASE}]: ").strip()
    database = database or config.SPANNER_DATABASE

    lines = [
        "# CodeDoc Configuration (auto-generated by 'init')",
        f"GOOGLE_CLOUD_PROJECT={project}" if project else
        "# GOOGLE_CLOUD_PROJECT=  (unset: the ADC / gcloud default project is used)",
        "GOOGLE_GENAI_USE_VERTEXAI=true",
        "",
        f"SPANNER_INSTANCE={instance}",
        f"SPANNER_DATABASE={database}",
        "",
        "# Uncomment to fix SSL issues on GCE:",
        "# GCE_METADATA_MTLS_MODE=none",
    ]

    with open(env_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  .env written to {env_path}")
    print("  You can now run: python -m graph_generator analyze <dir> --all")


def _print_banner(mode: str, data: PipelineData):
    print("=" * 70)
    print(f"  CodeDoc (Go) — analyze --{mode} (repo: {data.repo})")
    print("=" * 70)
    print(f"  Target:      {os.path.abspath(data.target_dir)}")
    print(f"  Spanner:     {pipeline._target_label(pipeline._spanner_target(data.no_spanner))}")
    if mode != "wiki":
        print(f"  Modules:     {data.mode}")
    if mode != "graph":
        print(f"  Model:       {config.MODEL} (concurrency {config.GEMINI_CONCURRENCY})")
    print(f"  Output:      {config.OUTPUT_DIR}")
    print("=" * 70)


# ── Peers (spec §8.2) ─────────────────────────────────────────────

def _peer_specs(repo: str, target_dir: str, args) -> list[tuple[str, str]]:
    """(name, real root) of every peer of one repo (spec §8.2): every other
    --repos entry under its manifest name, then --peer values, then PEER_REPOS
    (os.pathsep-separated).

    `[name=]path`; the name defaults to the directory name as given (a path
    reached through a link keeps the link's name, as repo names do) and must
    equal the name that repo is, or will be, ingested under (crossref finds nodes
    by it). A leading part holding a path separator belongs to the path. These
    raise UsageError: a name given for two paths (duplicate manifest names
    included), one path under two names, a --peer or PEER_REPOS entry that is
    the repo being ingested (under any name), an empty path, a missing directory.
    The repo's own --repos entry is skipped.
    """
    specs = ([(n, p, True) for n, p in _load_repos_manifest(args.repos)]
             if getattr(args, "repos", None) else [])
    raw = list(getattr(args, "peer", None) or [])
    if config.PEER_REPOS:
        raw += config.PEER_REPOS.split(os.pathsep)
    for spec in (s.strip() for s in raw):
        if spec:
            name, sep, path = spec.partition("=")
            if not sep or "/" in name or os.sep in name:
                name, path = "", spec
            specs.append((name.strip(), path.strip(), False))
    own = os.path.realpath(target_dir)
    roots: dict[str, str] = {}
    names: dict[str, str] = {}
    for name, path, listed in specs:
        if not path:
            raise UsageError(f"peer {name or '?'!r} has no path")
        given = os.path.abspath(os.path.expanduser(path))
        root = os.path.realpath(given)
        if not os.path.isdir(root):
            raise UsageError(f"peer {name or path!r}: directory not found: {given}")
        name = name or os.path.basename(given.rstrip(os.sep))
        if root == own and not listed:
            raise UsageError(f"peer {name!r} is the repo being ingested ({target_dir})")
        if roots.setdefault(name, root) != root:
            raise UsageError(f"peer name {name!r} names two paths: {roots[name]} and {root}")
        if names.setdefault(root, name) != name:
            raise UsageError(f"{root} is given under two names: {names[root]!r} and {name!r}")
    if roots.get(repo, own) != own:
        raise UsageError(f"peer name {repo!r} is the repo being ingested ({target_dir})")
    return [(name, root) for name, root in roots.items() if root != own]


def _build_peer(name: str, root: str):
    """A peer repo discovered from files only (spec §8.2), as go_model.Peer."""
    return pipeline._handoff("go_units", "discover_peer", "WP-D")(name, root)


def _peers_for(repo: str, target_dir: str, args) -> list:
    return [_build_peer(name, root) for name, root in _peer_specs(repo, target_dir, args)]


# ── Repos ─────────────────────────────────────────────────────────

def _resolve_repo_name(target_dir, args) -> str:
    """Repo name for a target: --repo-name if given, else the dir basename."""
    name = getattr(args, "repo_name", None)
    if name:
        return name
    return os.path.basename(os.path.abspath(target_dir).rstrip(os.sep)) or "repo"


@contextlib.contextmanager
def _repo_output_dir(repo):
    """Isolate a repo's checkpoints/outputs under OUTPUT_DIR/repos/<repo> so
    repos ingested into the same graph never clobber each other's state."""
    base = config.OUTPUT_DIR
    config.OUTPUT_DIR = os.path.join(base, "repos", repo)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    try:
        yield
    finally:
        config.OUTPUT_DIR = base


def _load_repos_manifest(path):
    """Parse a --repos manifest: a JSON list of {name?, path} (bare path
    strings also accepted). Returns [(name, abs_path)]."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        out = []
        for e in entries:
            p = e if isinstance(e, str) else e["path"]
            name = None if isinstance(e, str) else e.get("name")
            p_abs = os.path.abspath(p)
            out.append((name or os.path.basename(p_abs.rstrip(os.sep)) or "repo", p_abs))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise UsageError(f"cannot read the --repos manifest {path}: {exc!r}") from exc
    return out


def _repos(args) -> list[tuple[str, str]]:
    """[(repo, abs path)]: the positional target, or each --repos entry."""
    if args.repos and args.target_dir:
        raise UsageError("give a target directory or --repos <manifest.json>, not both")
    if args.repos and args.repo_name:
        raise UsageError("--repo-name names a single target; a --repos manifest names its repos")
    if args.repos:
        pairs = _load_repos_manifest(args.repos)
    elif args.target_dir:
        pairs = [(_resolve_repo_name(args.target_dir, args), os.path.abspath(args.target_dir))]
    else:
        raise UsageError("provide a target directory, or --repos <manifest.json>")
    for _repo, path in pairs:
        if not os.path.isdir(path):
            raise UsageError(f"directory not found: {path}")
    return pairs


def _raise_on_errors(checks):
    errors = [c for c in checks if c.status == "error"]
    if errors:
        raise PreflightError(
            f"{len(errors)} check(s) failed: {', '.join(c.name for c in errors)}. Fix them and "
            "run again; `python -m graph_generator doctor <dir>` prints the same table.")


# ── Commands ──────────────────────────────────────────────────────

def cmd_analyze(args):
    """One mode for each repo: --graph, --wiki, or --all (graph, then wiki)."""
    mode = "all" if args.all else "graph" if args.graph else "wiki"
    repos = _repos(args)
    peers = {repo: _peers_for(repo, path, args) for repo, path in repos} if mode != "wiki" else {}
    modules = "download" if args.download_modules or config.GO_MODULES_DOWNLOAD else "offline"
    _check_config()
    # The doctor checks first (R15); the graph modes include each repo's discovery.
    checks, toolchain = pipeline.preflight(mode, no_spanner=args.no_spanner, repos=repos,
                                           peers=peers, modules=modules)
    if checks:
        print(pipeline.format_checks(checks))
    _raise_on_errors(checks)

    states = {}
    if mode == "wiki":
        # Every repo's preconditions hold before the first Gemini call (spec §2.7).
        for repo, path in repos:
            with _repo_output_dir(repo):
                data = pipeline.load_graph_state(path, repo)
                pipeline.prepare_wiki(data, no_spanner=args.no_spanner)
                states[repo] = data

    for repo, path in repos:
        with _repo_output_dir(repo):
            total_start = time.perf_counter()
            data = states.get(repo) or PipelineData(
                target_dir=path, repo=repo, mode=modules, no_spanner=args.no_spanner,
                peers=peers.get(repo, []), toolchain=toolchain)
            _print_banner(mode, data)
            if mode in ("graph", "all"):
                pipeline.run_graph_track(data)
            if mode == "all":
                pipeline.prepare_wiki(data, no_spanner=args.no_spanner)
            if mode in ("wiki", "all"):
                asyncio.run(pipeline.run_wiki_track(data))
            _print_timing_report(data, time.perf_counter() - total_start)


def cmd_doctor(args):
    """Preflight (spec §2.4): toolchain and helper, the repo's groups and go
    lines, offline modules, Spanner and Gemini, printed as one table."""
    target = args.target_dir
    if target and not os.path.isdir(target):
        raise UsageError(f"directory not found: {target}")
    print("=" * 70)
    print("  CodeDoc (Go) — doctor")
    print("=" * 70)
    _check_config()
    repos = [(_resolve_repo_name(target, args), os.path.abspath(target))] if target else []
    checks, _toolchain = pipeline.preflight("doctor", repos=repos)
    print(pipeline.format_checks(checks))
    todo = [c.name for c in checks if c.status == "todo"]
    if todo:
        print(f"\n  Not implemented yet: {', '.join(todo)} (each row names its work package).")
    _raise_on_errors(checks)


def cmd_setup_go(args):
    """Install the pinned Go toolchain into codedoc_go/.tools/go (spec §2.4)."""
    setup_go = pipeline._handoff("go_toolchain", "setup_go", "WP-D")
    toolchain = setup_go(TOOLS_GO)
    print(f"  Installed {toolchain.version}: {toolchain.go}")
    print("  It is used when GO_TOOLCHAIN is unset.")


def cmd_setup_spanner(args):
    """Create Spanner instance, database, tables, and property graph."""
    _check_config()
    from .setup_spanner_graph import create_instance, create_database, verify

    project = config.GCP_PROJECT
    instance = getattr(args, "instance", None) or config.SPANNER_INSTANCE
    database = getattr(args, "database", None) or config.SPANNER_DATABASE
    region = getattr(args, "region", None) or "asia-northeast1"

    print("=" * 60)
    print("  Setup Spanner Graph")
    print("=" * 60)
    print(f"  Project:  {_project_label()}")
    print(f"  Instance: {instance}")
    print(f"  Database: {database}")
    print(f"  Region:   {region}")
    print("=" * 60)

    if not getattr(args, "skip_instance", False):
        print("\nStep 1: Creating Spanner instance...")
        create_instance(project, instance, region)
    else:
        print("\nStep 1: Skipping instance creation (--skip-instance)")

    print("\nStep 2: Creating database + tables + property graph...")
    create_database(project, instance, database)

    print("\nStep 3: Verifying...")
    verify(project, instance, database)
    print("\nSpanner setup complete.")


def cmd_trial(args):
    """The real-repo trial (spec §9.6), GCP-free."""
    if not os.path.isdir(args.target_dir):
        raise UsageError(f"directory not found: {args.target_dir}")
    run_trial = pipeline._handoff("trial", "run_trial", "WP-B6")
    ok = run_trial(os.path.abspath(args.target_dir), args.repo_name, packages=args.packages or [],
                   download=args.download_modules or config.GO_MODULES_DOWNLOAD,
                   skip_oracle=args.skip_oracle)
    sys.exit(0 if ok else 1)


def cmd_crossref(args):
    """Derive + write cross-repo dependency edges and print the coupling matrix."""
    run_crossref = pipeline._handoff("crossref", "run_crossref", "WP-B5")
    _check_config()
    print("=" * 70)
    print("  CodeDoc — Cross-Repository Analysis")
    print("=" * 70)
    run_crossref(printer=print, write=not getattr(args, "dry_run", False))
    print("=" * 70)


def cmd_evaluate(args):
    """Score the resolver against the committed fixtures (no GCP, no network)."""
    run_evaluation = pipeline._handoff("evaluate", "run_evaluation", "WP-B5")
    fixtures = list(EVAL_FIXTURES) if args.fixture == "all" else [args.fixture]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ok = run_evaluation(fixtures, repo_root, env_mode=getattr(args, "env", False),
                        dump_edges=getattr(args, "dump_edges", False),
                        quiet_misses=getattr(args, "quiet_misses", False))
    sys.exit(0 if ok else 1)


def cmd_validate(args):
    """Validate Spanner graph data: row counts, nodes per repo, orphan edges and
    cross-repo coupling. Every table comes from NODE_SPEC / EDGE_SPEC."""
    from .setup_spanner_graph import EDGE_TABLES, NODE_TABLES, cross_repo_tables, orphan_checks

    db = _get_spanner_db()
    print("=" * 70)
    print("  GRAPH VALIDATION")
    print("=" * 70)
    _check_config()

    for table in NODE_TABLES + EDGE_TABLES:
        with db.snapshot() as snap:
            rows = list(snap.execute_sql(f"SELECT COUNT(*) FROM {table}"))
            count = rows[0][0] if rows else 0
            print(f"  {table:<25} {count:>10,}")

    # Per-repo node counts — surfaces an accidental cross-repo merge (a repo
    # missing, or two repos sharing a name) at a glance.
    print("\n  Nodes by repo:")
    for table in NODE_TABLES:
        with db.snapshot() as snap:
            rows = list(snap.execute_sql(
                f"SELECT repo, COUNT(*) AS n FROM {table} GROUP BY repo ORDER BY repo"))
            if rows:
                summary = ", ".join(f"{r[0] or '(none)'}={r[1]:,}" for r in rows)
                print(f"  {table:<25} {summary}")

    # Both endpoints of every edge must be a node (upserts never delete rows,
    # so renamed or deleted code can leave orphans behind).
    print("\n  Orphan checks:")
    for label, query in orphan_checks():
        with db.snapshot() as snap:
            rows = list(snap.execute_sql(query))
            count = rows[0][0] if rows else 0
            status = "OK" if count == 0 else f"WARNING: {count} orphans"
            print(f"  {label:<45} {status}")

    print("\n  Cross-repo coupling:")
    coupling: dict[tuple[str, str], dict[str, int]] = {}
    for table in cross_repo_tables():
        with db.snapshot() as snap:
            for row in snap.execute_sql(
                    f"SELECT source_repo, target_repo, COUNT(*) FROM {table} "
                    "GROUP BY source_repo, target_repo"):
                if len(row) >= 3:
                    coupling.setdefault((row[0], row[1]), {})[table] = row[2]
    if coupling:
        for (src, tgt) in sorted(coupling):
            counts = ", ".join(f"{t}={n}" for t, n in sorted(coupling[(src, tgt)].items()))
            print(f"    {src} → {tgt} :  {counts}")
    else:
        print("    (none — run `crossref` after ingesting 2+ repos)")

    print("=" * 70)


# ── Parser and dispatch ───────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m graph_generator",
        description="CodeDoc (Go): a code graph in Spanner and a Japanese wiki for Go modules",
        epilog=EXIT_CODES,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    p_init = subparsers.add_parser("init", help="Setup: generate .env configuration")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing .env")

    # analyze — exactly one mode (C9)
    p = subparsers.add_parser(
        "analyze", help="Build the graph (--graph), the wiki (--wiki) or both (--all)",
        description="Graph track: Phases 1, 1.5, 1.6, 8, 9 (no LLM). Wiki track: Phases 2-6, "
                    "10 on a finished --graph of the same files.", epilog=EXIT_CODES)
    # target_dir optional so a run can be driven entirely by --repos manifest
    p.add_argument("target_dir", nargs="?", help="Source code directory (one repo)")
    p.add_argument("--repo-name", help="Repository name in the graph (default: dir basename)")
    p.add_argument("--repos", metavar="MANIFEST",
                   help="JSON manifest of repos [{name?, path}] to ingest into the same graph; "
                        "each is a peer of the others")
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--all", action="store_true", help="The graph track, then the wiki track")
    modes.add_argument("--graph", action="store_true",
                       help="Graph track: Phases 1, 1.5, 1.6, 8, 9 (no LLM calls)")
    modes.add_argument("--wiki", action="store_true",
                       help="Wiki track on a finished --graph: Phases 2-6, 10 (Gemini)")
    p.add_argument("--peer", action="append", default=[], metavar="[NAME=]PATH",
                   help="Peer repo whose modules stand in for the versions a module requires "
                        "(repeatable; NAME defaults to the directory name)")
    p.add_argument("--download-modules", action="store_true",
                   help="Download the modules the load needs into OUTPUT_DIR/go/modcache "
                        "(same as GO_MODULES_DOWNLOAD=1)")
    p.add_argument("--no-spanner", action="store_true",
                   help="Write rows and summaries under OUTPUT_DIR only")

    # doctor
    p = subparsers.add_parser("doctor", help="Preflight checks, printed as a table")
    p.add_argument("target_dir", nargs="?",
                   help="Repo to check: groups, go lines against the toolchain, offline modules")

    # setup — subcommands
    p_setup = subparsers.add_parser("setup", help="Install the Go toolchain or create Spanner resources")
    setup_sub = p_setup.add_subparsers(dest="setup_command", metavar="{go,spanner}", required=True)
    setup_sub.add_parser("go", help="Install the pinned Go toolchain into .tools/go")
    p_ss = setup_sub.add_parser("spanner", help="Create Spanner instance + database + tables + graph")
    p_ss.add_argument("--instance", help="Spanner instance ID")
    p_ss.add_argument("--database", help="Spanner database ID")
    p_ss.add_argument("--region", help="Spanner region (default: asia-northeast1)")
    p_ss.add_argument("--skip-instance", action="store_true", help="Skip instance creation")

    # validate
    subparsers.add_parser("validate", help="Validate Spanner graph data")

    # crossref — cross-repository dependency + coupling analysis
    p_xr = subparsers.add_parser(
        "crossref", help="Analyze cross-repo dependencies + coupling (run after ingesting repos)")
    p_xr.add_argument("--dry-run", action="store_true",
                      help="Derive + print the coupling matrix without writing edges")

    # evaluate (no GCP, no network)
    p_eval = subparsers.add_parser(
        "evaluate", help="Score extraction and resolution on the test_codes/ fixtures "
                         "(no GCP, no network)")
    p_eval.add_argument("--fixture", choices=[*EVAL_FIXTURES, "all"], default="all",
                        help="Fixture to evaluate (default: all)")
    p_eval.add_argument("--env", action="store_true",
                        help="Env mode: third-party modules from the synthetic proxy "
                             "test_codes/_proxy in an isolated cache (no network)")
    p_eval.add_argument("--dump-edges", action="store_true",
                        help="Include derived edges in the report")
    p_eval.add_argument("--quiet-misses", action="store_true",
                        help="Hide per-case miss details")

    # trial
    p_trial = subparsers.add_parser(
        "trial", help="Real-repo trial: the graph track with --no-spanner and the oracle "
                      "precision check (no GCP)")
    p_trial.add_argument("target_dir", help="Repo to try")
    p_trial.add_argument("--repo-name", required=True, help="Repository name")
    p_trial.add_argument("--packages", nargs="+", metavar="P",
                         help="Packages whose unit tests the oracle runs "
                              "(default: those of the runtime-oracle report)")
    p_trial.add_argument("--download-modules", action="store_true",
                         help="Also run the download-mode leg (needs the network)")
    p_trial.add_argument("--skip-oracle", action="store_true",
                         help="Skip the precision check (goracle on a copy of the repo)")
    return parser


_HANDLERS = {"init": "cmd_init", "analyze": "cmd_analyze", "doctor": "cmd_doctor",
             "validate": "cmd_validate", "crossref": "cmd_crossref",
             "evaluate": "cmd_evaluate", "trial": "cmd_trial"}
_SETUP_HANDLERS = {"go": "cmd_setup_go", "spanner": "cmd_setup_spanner"}


def _fail(message: str, code: int):
    sys.stdout.flush()  # the table and phase lines come first, also in a piped log
    print(message, file=sys.stderr)
    sys.exit(code)


def main(argv: list[str] | None = None):
    parser = build_parser()
    args = parser.parse_args(argv)  # usage errors exit with 2
    if args.command is None:
        parser.print_help()
        sys.exit(2)
    name = (_SETUP_HANDLERS[args.setup_command] if args.command == "setup"
            else _HANDLERS[args.command])
    try:
        globals()[name](args)
    except UsageError as exc:
        _fail(f"Error: {exc}", 2)
    except PreflightError as exc:
        _fail(f"Preflight failed: {exc}", 3)
    except WikiPreconditionError as exc:
        _fail(f"--wiki cannot run: {exc}", 4)
    except NotImplementedError as exc:
        _fail(f"Error: {exc}", 1)


if __name__ == "__main__":
    main()
