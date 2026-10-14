"""Recorded gohelper outputs for the resolution tests (spec §10).

A case directory tests/fixtures/helper/<case>/ holds a Go repo under repo/
and what the real helper printed for it: extract.jsonl, one
resolve-<group>-<build>.jsonl per group load, and meta.json (the helper
revision and toolchain it was recorded with). Absolute paths are stored as
$REPO, $GOROOT and $OUT, so a test can replay the records against a copy of
the repo anywhere. Record again (needs a Go toolchain) with

    .venv/bin/python tests/helper_fixtures.py tests/fixtures/helper/<case>
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))

from graph_generator.go_model import BuildConfig  # noqa: E402

CASE_BUILDS = {
    "basic": [BuildConfig("linux-amd64", "linux", "amd64", (), False, True),
              BuildConfig("windows-amd64", "windows", "amd64", (), False, False)],
}


def _subst(text: str, mapping: dict[str, str]) -> str:
    for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(old, new)
    return text


def record(case_dir: str, out_dir: str | None = None) -> dict:
    """Run the real helper on case_dir/repo; write the normalized outputs
    into out_dir (default: case_dir). Returns {file name: text}."""
    from go_worlds import GO
    from graph_generator import go_extract, go_toolchain, go_units
    case = os.path.basename(os.path.normpath(case_dir))
    builds = CASE_BUILDS[case]
    target = out_dir or case_dir
    work = os.path.realpath(os.path.join(target, ".record-out"))
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        repo = os.path.realpath(os.path.join(case_dir, "repo"))
        tc = go_toolchain.probe_toolchain(GO)
        helper = go_toolchain.ensure_helper(tc, work)
        disc = go_units.discover(repo, case, builds=builds, peers=[], mode="offline",
                                 output_dir=work, toolchain=tc)
        go_units.prepare_control_dirs(disc, work)
        mapping = {repo: "$REPO", tc.goroot: "$GOROOT", work: "$OUT"}
        outputs: dict[str, str] = {}
        rels, _skipped = go_units.scan_go_files(repo)
        env = go_toolchain.base_env(tc, work)
        env.update({"GOWORK": "off", "GOFLAGS": "-buildvcs=false", "GOPROXY": "off", "GOSUMDB": "off",
                    "CGO_ENABLED": "0", "GOOS": builds[0].goos, "GOARCH": builds[0].goarch})
        listing = os.path.join(work, "files.txt")
        with open(listing, "w") as f:
            f.write("".join(r + "\n" for r in sorted(rels)))
        proc = subprocess.run([helper, "extract", "--root", repo, "--files-from", listing],
                              env=env, capture_output=True, text=True, check=True)
        outputs["extract.jsonl"] = _subst(proc.stdout, mapping)
        for b in builds:
            for g in disc.groups:
                if g.status != "ok":
                    continue
                genv = go_toolchain.env_for(g, b, mode="offline", output_dir=work, user_modcache=None,
                                            toolchain=tc)
                argv = [helper, "resolve", "--repo-root", repo, "--dir", g.dir, "--pattern", g.pattern,
                        "--gowork", g.gowork or "off", "--modfile", g.modfile or "",
                        "--goos", b.goos, "--goarch", b.goarch, "--tags", ",".join(b.tags),
                        "--cgo", "1" if b.cgo else "0", "--build-id", b.id,
                        "--main-modules", ",".join(g.modules)]
                proc = subprocess.run(argv, cwd=g.dir, env=genv, capture_output=True, text=True,
                                      check=True)
                lines = [json.loads(line) for line in proc.stdout.splitlines()]
                for rec in lines:                       # timings and memory change every run
                    if rec.get("kind") == "summary":
                        for key in ("load_seconds", "seconds", "peak_rss_bytes"):
                            rec[key] = 0
                text = "".join(json.dumps(rec, sort_keys=True) + "\n" for rec in lines)
                outputs[f"resolve-{g.id}-{b.id}.jsonl"] = _subst(text, mapping)
        outputs["meta.json"] = json.dumps({"helper_revision": go_extract.helper_revision(),
                                           "toolchain": tc.version, "builds": [b.id for b in builds],
                                           "groups": {g.id: g.status for g in disc.groups}},
                                          indent=1, sort_keys=True) + "\n"
        os.makedirs(target, exist_ok=True)
        for name, text in outputs.items():
            with open(os.path.join(target, name), "w") as f:
                f.write(text)
        return outputs
    finally:
        shutil.rmtree(work, ignore_errors=True)


def replay(case_dir: str, repo: str, goroot: str, out: str) -> tuple[list[dict], dict[tuple[str, str], list[dict]], dict]:
    """(extract lines, {(group, build): resolve lines}, meta) with the
    placeholders replaced by these paths."""
    mapping = {"$REPO": repo, "$GOROOT": goroot, "$OUT": out}

    def lines(name):
        with open(os.path.join(case_dir, name)) as f:
            return [json.loads(line) for line in _subst(f.read(), mapping).splitlines()]

    with open(os.path.join(case_dir, "meta.json")) as f:
        meta = json.load(f)
    resolves = {}
    for name in sorted(os.listdir(case_dir)):
        if name.startswith("resolve-") and name.endswith(".jsonl"):
            group, build = name[len("resolve-"):-len(".jsonl")].split("-", 1)
            resolves[(group, build)] = lines(name)
    return lines("extract.jsonl"), resolves, meta


if __name__ == "__main__":
    for case_dir in sys.argv[1:]:
        names = sorted(record(case_dir))
        print(f"recorded {case_dir}: {', '.join(names)}")
