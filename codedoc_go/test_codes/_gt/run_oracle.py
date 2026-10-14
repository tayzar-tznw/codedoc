#!/usr/bin/env python3
"""Runtime oracle for a Go fixture's ground truth (stdlib only, plus the Go toolchain).

Usage: python3 run_oracle.py <fixture-dir> [--go PATH] [--scratch DIR] [--entries a,b] [--timeout S]

Steps (master plan C11):
  1. build goracle from test_codes/_gt/oracle;
  2. goracle instrument --src <fixture> --out <scratch>/copy (tag mode all);
  3. run every entry of <fixture>/entries.json in the copy: `go run` or
     `go test -count=1 -vet=off`, both with -gcflags=all=-l
     -ldflags=-checklinkname=0 -buildvcs=false, GORACLE_OUT=<scratch>/hits/<entry>
     and an environment built from scratch: GOPROXY=file://<test_codes/_proxy>,
     GOSUMDB=off, GOFLAGS=-modcacherw (never -mod=mod), GOOS=linux GOARCH=amd64
     CGO_ENABLED=0, GOTOOLCHAIN=local, GOENV=off, an isolated module cache,
     plus the entry's env (GOWORK and non-GO* variables; a relative GOWORK is a
     work file of the fixture, resolved in the copy);
  4. goracle report over the hits of all entries;
  5. goracle judge against <fixture>/ground_truth/manifest.json.

The toolchain is --go, else $GORACLE_GO, else $GO_TOOLCHAIN (a go binary or a
GOROOT), else `go` on PATH; it must be go1.27 (the runtime part of goracle is
pinned to it). GOCACHE is inherited when set. The scratch directory is
--scratch, else $GORACLE_SCRATCH, else ~/.cache/codedoc-goracle; the fixture's
copy, hits, logs, obs.jsonl and verdicts.json go to <scratch>/<fixture>/.
Exit code 0 means every entry exited 0 and no case failed (judge verdicts
MISS, IFACE-OTHER, DEFER-GROUP-OTHER, NO-SITE, TABLE-MISMATCH, NOT-RUN,
ENTRY-FAILED fail; UNEXEC, SKIPPED, UNVERIFIABLE are listed).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ORACLE_SRC = os.path.join(HERE, "oracle")
PROXY = os.path.normpath(os.path.join(HERE, "..", "_proxy"))
BUILD_FLAGS = ["-gcflags=all=-l", "-ldflags=-checklinkname=0", "-buildvcs=false"]
DEFAULT_TIMEOUT = 600


class OracleError(Exception):
    pass


def find_go(explicit: str | None = None) -> str:
    for cand in (explicit, os.environ.get("GORACLE_GO"), os.environ.get("GO_TOOLCHAIN")):
        if not cand:
            continue
        if os.path.isdir(cand):
            cand = os.path.join(cand, "bin", "go")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.abspath(cand)
        raise OracleError(f"no go binary at {cand}")
    found = shutil.which("go")
    if not found:
        raise OracleError("no go toolchain: pass --go or set GORACLE_GO")
    return found


def go_version(go: str) -> str:
    out = subprocess.run([go, "version"], capture_output=True, text=True, env=base_env(go, None), timeout=60)
    words = out.stdout.split()
    return words[2] if len(words) > 2 else out.stdout.strip()


def proxy_url() -> str:
    """GOPROXY for the synthetic proxy: the go command accepts only an absolute file:/// URL (spec R22)."""
    if not os.path.isabs(PROXY):
        raise OracleError(f"the proxy path is not absolute: {PROXY}")
    return "file://" + PROXY


def base_env(go: str, scratch: str | None) -> dict[str, str]:
    """The environment of every go command, built from scratch. GOFLAGS sets no -mod: an
    explicit -mod=readonly would switch off the default vendor mode of vendored modules."""
    env = {"PATH": os.pathsep.join([os.path.dirname(go), "/usr/bin", "/bin"]),
           "GOTOOLCHAIN": "local", "GOENV": "off", "GOFLAGS": "-modcacherw", "GOSUMDB": "off",
           "GOPROXY": proxy_url(), "GOVCS": "*:off", "GOOS": "linux", "GOARCH": "amd64",
           "CGO_ENABLED": "0", "NO_COLOR": "1"}
    for key in ("HOME", "USER", "GOCACHE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if scratch:
        env.update({"GOMODCACHE": os.path.join(scratch, "modcache"), "GOPATH": os.path.join(scratch, "gopath"),
                    "TMPDIR": os.path.join(scratch, "tmp"), "GOTMPDIR": os.path.join(scratch, "tmp"),
                    "XDG_CONFIG_HOME": os.path.join(scratch, "xdg")})
        env.setdefault("GOCACHE", os.path.join(scratch, "gocache"))
    return env


def load_entries(fixture: str) -> dict[str, dict]:
    path = os.path.join(fixture, "entries.json")
    try:
        with open(path, encoding="utf-8") as handle:
            entries = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise OracleError(f"entries.json: {exc}") from None
    if not isinstance(entries, dict):
        raise OracleError("entries.json must map entry names to entries")
    for name, entry in entries.items():
        words = list(entry.get("cmd", [])) + list(entry.get("test", [])) + list(entry.get("test_args", []))
        words += list((entry.get("env") or {}).values())
        if any("-mod=" in str(w) for w in words):
            raise OracleError(f"entry {name}: -mod flags are not allowed")
        for key in entry.get("env") or {}:
            if (key.startswith("GO") and key != "GOWORK") or key in ("CGO_ENABLED", "PATH", "HOME"):
                raise OracleError(f"entry {name}: env may not set {key}")
        if ("cmd" in entry) == ("test" in entry):
            raise OracleError(f"entry {name}: needs exactly one of cmd and test")
        if "cmd" in entry and entry["cmd"][:2] != ["go", "run"]:
            raise OracleError(f"entry {name}: cmd must start with go run")
    return entries


def entry_argv(go: str, entry: dict) -> list[str]:
    if "cmd" in entry:
        return [go, "run", *BUILD_FLAGS, *entry["cmd"][2:]]
    return [go, "test", "-count=1", "-vet=off", *BUILD_FLAGS, *entry.get("test_args", []), *entry["test"]]


def entry_env(go: str, scratch: str, entry: dict, hits: str, copy: str | None = None) -> dict[str, str]:
    """The entry's environment; a relative GOWORK names a work file of the fixture, so it is
    resolved against the fixture copy (the go command accepts only an absolute GOWORK)."""
    env = base_env(go, scratch)
    env.update(entry.get("env") or {})
    gowork = env.get("GOWORK")
    if copy and gowork and gowork != "off" and not os.path.isabs(gowork):
        env["GOWORK"] = os.path.join(copy, gowork)
    env["GORACLE_OUT"] = hits
    return env


class Runner:
    def __init__(self, fixture: str, go: str, scratch: str, only: list[str] | None, timeout: int,
                 printer=print) -> None:
        self.fixture = os.path.abspath(fixture)
        self.name = os.path.basename(self.fixture)
        self.go = go
        self.work = os.path.join(os.path.abspath(scratch), self.name)
        self.only = only
        self.timeout = timeout
        self.printer = printer

    def path(self, *parts: str) -> str:
        return os.path.join(self.work, *parts)

    def sh(self, argv: list[str], cwd: str, env: dict[str, str], log: str, timeout: int) -> int:
        with open(log, "w", encoding="utf-8") as handle:
            handle.write("$ " + " ".join(argv) + f"\n# cwd {cwd}\n")
            handle.flush()
            try:
                proc = subprocess.run(argv, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                      timeout=timeout)
            except subprocess.TimeoutExpired:
                handle.write(f"\n# timeout after {timeout} s\n")
                return 124
        return proc.returncode

    def run(self) -> int:
        manifest = os.path.join(self.fixture, "ground_truth", "manifest.json")
        if not os.path.isfile(manifest):
            raise OracleError(f"{manifest} is missing: run generate_manifest.py first")
        entries = load_entries(self.fixture)
        if self.only:
            unknown = sorted(set(self.only) - set(entries))
            if unknown:
                raise OracleError(f"unknown entries: {unknown}")
            entries = {k: v for k, v in entries.items() if k in self.only}
        version = go_version(self.go)
        if not version.startswith("go1.27"):
            raise OracleError(f"goracle is pinned to go1.27, the toolchain is {version}")
        if version != "go1.27.2":
            self.printer(f"warning: goracle was validated with go1.27.2, the toolchain is {version}")
        for sub in ("copy", "hits", "logs"):
            shutil.rmtree(self.path(sub), ignore_errors=True)
        for sub in ("logs", "bin", "tmp", "gopath", "xdg"):
            os.makedirs(self.path(sub), exist_ok=True)
        env = base_env(self.go, self.work)
        env["GOWORK"] = "off"
        env["GOPROXY"] = "off"
        goracle = self.path("bin", "goracle")
        if self.sh([self.go, "build", "-buildvcs=false", "-o", goracle, "."], ORACLE_SRC, env,
                   self.path("logs", "build-goracle.log"), 600) != 0:
            raise OracleError(f"building goracle failed; see {self.path('logs', 'build-goracle.log')}")
        self.printer(f"run_oracle: {self.name} ({version}, scratch {self.work})")
        rc = self.sh([goracle, "instrument", "--src", self.fixture, "--out", self.path("copy")], self.work, env,
                     self.path("logs", "instrument.log"), 600)
        if rc != 0:
            raise OracleError(f"goracle instrument failed; see {self.path('logs', 'instrument.log')}")
        with open(self.path("logs", "instrument.log"), encoding="utf-8") as handle:
            self.printer("  " + handle.read().splitlines()[-1])
        ran, failed, hit_dirs = [], [], []
        for name, entry in entries.items():
            hits = self.path("hits", name)
            os.makedirs(hits, exist_ok=True)
            hit_dirs.append(hits)
            cwd = os.path.join(self.path("copy"), entry.get("dir", "."))
            log = self.path("logs", f"entry-{name}.log")
            rc = self.sh(entry_argv(self.go, entry), cwd,
                         entry_env(self.go, self.work, entry, hits, self.path("copy")), log,
                         int(entry.get("timeout", self.timeout)))
            if rc == 0:
                ran.append(name)
                self.printer(f"  entry {name}: ok")
            else:
                failed.append(name)
                self.printer(f"  entry {name}: FAILED (exit {rc}); see {log}")
                with open(log, encoding="utf-8", errors="replace") as handle:
                    for line in handle.read().splitlines()[-15:]:
                        self.printer("    | " + line)
        obs = self.path("obs.jsonl")
        report_log = self.path("logs", "report.log")
        if self.sh([goracle, "report", "--copy", self.path("copy"), "--hits", ",".join(hit_dirs), "--out", obs],
                   self.work, env, report_log, 600) != 0:
            raise OracleError(f"goracle report failed; see {report_log}")
        with open(report_log, encoding="utf-8") as handle:
            for line in handle.read().splitlines()[2:]:
                self.printer("  " + line)
        judge_log = self.path("logs", "judge.log")
        rc = self.sh([goracle, "judge", "--obs", obs, "--manifest", manifest, "--ran", ",".join(ran) or "-",
                      "--failed", ",".join(failed), "--out", self.path("verdicts.json")], self.work, env,
                     judge_log, 600)
        with open(judge_log, encoding="utf-8") as handle:
            for line in handle.read().splitlines()[2:]:
                self.printer("  " + line)
        if rc not in (0, 1):
            raise OracleError(f"goracle judge failed; see {judge_log}")
        return 1 if failed or rc != 0 else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run_oracle.py", description="Runtime oracle for a Go fixture.")
    parser.add_argument("fixture")
    parser.add_argument("--go", help="go binary or GOROOT (default: $GORACLE_GO, $GO_TOOLCHAIN, PATH)")
    parser.add_argument("--scratch", help="work directory (default: $GORACLE_SCRATCH or ~/.cache/codedoc-goracle)")
    parser.add_argument("--entries", help="comma-separated entries to run (default: all)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per entry (default 600)")
    args = parser.parse_args(argv[1:])
    scratch = args.scratch or os.environ.get("GORACLE_SCRATCH") or os.path.join(
        os.path.expanduser("~"), ".cache", "codedoc-goracle")
    only = [e for e in (args.entries or "").split(",") if e] or None
    try:
        runner = Runner(args.fixture, find_go(args.go), scratch, only, args.timeout)
        return runner.run()
    except OracleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
