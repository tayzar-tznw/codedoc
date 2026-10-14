# codedoc_go design: CodeDoc for Go modules

**Status:** v1, 2026-10-13.
- The user set the goal on 2026-10-13: "design, implement and test until everything is working correctly and efficiently. Make the whole knowledge graph building process as smooth as possible."
- This spec records the decisions taken under that goal. The user's answers are in §14.1.

**Basis:**
- five research reports with hands-on probes, run on 2026-10-13: [research/2026-10-13/](../research/2026-10-13/README.md), summarized in Appendix A;
- the code and specs of `codedoc_python` (shell) and `codedoc_csharp` (resolver model).

**Where:** `codedoc_go/docs/superpowers/`, at the user's request ("save this work in codedoc_go"). The Leiden-topics sibling `codedoc_go_leiden/` has its own delta spec: `codedoc_go_leiden/docs/superpowers/specs/2026-10-13-codedoc-go-leiden-design.md`.

---

## 0. Summary

`codedoc_go/` is a fifth sibling of the CodeDoc variants. It reads plain Go modules (`go.mod`, `go.work`, `vendor/`), loads a code knowledge graph into Cloud Spanner, and writes Japanese wiki pages and node summaries with Gemini.

**What it takes from where:**
- **Shell from `codedoc_python`:** freshness by content hash, per-repo outputs, multi-repo CLI, `crossref`, `evaluate`, query stack, docs gate.
- **Resolver model from `codedoc_csharp`:**
  - the compiler's binding is authoritative;
  - external targets are classified by location;
  - anything that is not certain is refused;
  - the corpus has distractors and is checked at runtime.

Six choices shape the design:

1. **Engine.** A small Go helper replaces both tree-sitter and the language server.
   - **Extraction** uses `go/parser`, stdlib only.
   - **Resolution** uses one `golang.org/x/tools/go/packages` + `go/types` load per build configuration.
   - **Evidence (2026-10-13).** On etcd v3.7.2 and Prometheus v3.15.0, gopls and the helper named the same target at all 187,431 call sites.
   - **Cost.** The helper takes 2.3 s / 5.7 s against gopls's 40–65 s, and uses less memory.
   - **Safety.** Only the helper has the type facts that detect wrong answers offline.
   - **Why not tree-sitter-go.** It fails on Go 1.26 syntax in 32 etcd files.
2. **Three run modes.**
   - `analyze --graph` builds the graph and makes no LLM calls.
   - `analyze --wiki` adds Gemini summaries for every node, plus topics, wiki pages and embeddings. It requires a finished `--graph` on the same file hashes.
   - `analyze --all` runs both.
3. **Nothing runs from the repo, and nothing is written into it.**
   - Discovery parses `go.mod`, `go.work`, `vendor/modules.txt` and build constraints in Python.
   - Every go command and the helper run with an environment built from scratch.
   - That environment uses a `go.work` or a `-modfile` copy generated under `OUTPUT_DIR`, with `-buildvcs=false`, `CGO_ENABLED=0` and `GOTOOLCHAIN=local`.
4. **Offline by default, opt-in download.**
   - Offline, modules come from `vendor/`, or from the user's module cache read through a `file://` proxy. Nothing is ever written to that cache.
   - `--download-modules` fetches the needed modules into an isolated cache under `OUTPUT_DIR`.
5. **Zero wrong edges, open-world interfaces.**
   - A static target becomes a `MethodCalls` edge.
   - An interface call is an edge to the interface method, plus `PossiblyCalls` to its in-repo implementers. Go interfaces are satisfied implicitly, so a single in-repo implementer is never promoted to an edge.
   - Offline hazards are gated with type facts.
6. **Proof.** Three fixtures are scored by `evaluate`, in both modes, with no GCP and no network:
   - F1: a Go port of `iryo-kanri-system`;
   - F2: layouts;
   - F3: multi-repo.

   A runtime oracle confirms the ground truth. On etcd and Prometheus, the same oracle checks every executed `MethodCalls` edge against what the repos' own unit tests actually call.

---

## 1. Scope and "done"

**In scope**
- `codedoc_go/`, a standalone sibling, for Go modules built with the go command: single modules, multi-module repos, `go.work` workspaces, `vendor/` (module and workspace), local `replace`.
- **Graph track:** Phases 1, 1.5, 1.6, 8 and 9.
- **Wiki track:** Phases 2–6 and 10, consisting of node summaries, directory, package and topic summaries, wiki pages, and embeddings.
- **Run modes and tooling:**
  - the three run modes, with resume and checkpoints;
  - multi-repo ingest and peers;
  - `crossref`, `validate`, `evaluate`, `setup spanner`;
  - new: `setup go`, `doctor`, `trial`.
- **Query stack:** `graph_query_agent`, MCP server, webapp and the Cloud Run deploy tooling, adapted.
- **Docs:**
  - the same five-file Japanese set as every sibling variant (user request, 2026-10-13): `README.md`, `docs/ARCHITECTURE.md`, `docs/LSP_RESOLUTION.md`, `docs/GRAPH_SCHEMA.md`, `docs/CROSS_PROJECT.md`;
  - English `CLAUDE.md`;
  - adapted `.claude/skills/codedoc-query/SKILL.md`, `sample_prompt.txt`, `.env.example`, `Dockerfile`, `mcp_server/README.md`.
- **Tests:** pytest with fakes (≥ 85 % coverage), `go test` for the helper, and the scored corpus.
- **Real-repo trial:** etcd v3.7.2 and Prometheus v3.15.0. It runs the graph track offline and in download mode, plus the oracle precision check. GCP-free.

**Out of scope**
- **GOPATH mode** (`GO111MODULE=off`). Files under no `go.mod` are scanned and extracted but not resolved (reason `no-module`).
- **Bazel / `GOPACKAGESDRIVER`.**
- **cgo resolution.** `CGO_ENABLED=0` is pinned by default. `GO_BUILD_CGO=1` is allowed, and the docs say it runs gcc and pkg-config.
- **Framework conventions:** DI containers (wire, fx), reflection-based routing, `go:generate`. They end as `dynamic` or `external`.
- **Points-to analysis** (VTA/RTA) for interface dispatch. It is listed as later work.
- **Private modules in download mode** (`GOPRIVATE`, VCS fetches). No VCS tool ever runs.
- **Other variants:** the shared-core refactor, and any change to the other variants.
- **Stale rows written outside the R48 conditions:** rows written under another Spanner target, or by a run whose `graph_rows/` is gone, are not pruned (R48 prunes everything else on re-ingest).
- **CI wiring.**

**Done means, each shown with output**
1. `python -m graph_generator evaluate` exits 0 in offline mode and in `--env` mode, with the §9.5 gates and 0 wrong. Neither mode needs network or GCP.
2. `.venv/bin/python -m pytest -q` passes at ≥ 85 % coverage without a Go toolchain. `-m go` builds the real helper and passes. `go test ./...` in `gohelper/` passes.
3. The runtime oracle confirms every executed ground-truth case.
4. The real-repo trial on etcd and Prometheus finishes in both modes. It reports counts, time and memory, and the precision check reports 0 wrong `MethodCalls` among executed sites.
5. The docs gate passes.
6. A real Spanner and Gemini run happens only when the user asks. It is billable.

---

## 2. Architecture

### 2.1 Run modes and phases

```
python -m graph_generator analyze <dir> (--all | --graph | --wiki) [options]
```

Exactly one mode flag is required. The other options are listed in §2.4.

| Mode | Phases | LLM | Spanner writes | Precondition |
|---|---|---|---|---|
| `--graph` | 1 → 1.5 → 1.6 → 8 → 9 | none | nodes and edges | Spanner schema exists (checked first), unless `--no-spanner` |
| `--wiki` | 1 (re-scan only) → 2 → 3 → 4 → 5 → 6 → 10 | Gemini, embeddings | summaries, topics, embeddings | A finished `--graph` on the same file hashes, same `ID_SCHEME` and same Spanner target (§7.1) |
| `--all` | `--graph`, then `--wiki` in the same process | both | both | as `--graph` |

| Phase | Work | Track |
|---|---|---|
| 1 | Scan, with the go command's skip rules; SHA-256 per file; discover modules, workspaces, vendor and build configuration → `units.json` (§3) | graph (re-scan in wiki) |
| 1.5 | Helper `extract`: declarations, sites and imports → `entities.json` (§4). Changed files are re-extracted by hash | graph |
| 1.6 | Helper `resolve`: one load per group × build configuration → classification and gates → `resolutions.json` (§5) | graph |
| 8 | Spanner nodes. Summaries start as the Go doc comment; unchanged files get their cached summaries back (R48) | graph |
| 9 | Spanner edges | graph |
| 2 | Node summaries with Gemini: batched per file, one call per node for large files (§7.2) | wiki |
| 3 | Package and directory summaries, bottom-up | wiki |
| 4 | Topics | wiki |
| 5 | Topic pages | wiki |
| 6 | Wiki index | wiki |
| 10 | Write-back: summary columns, `Topics` and `FileBelongsToTopic`, embeddings | wiki |

Phase 7 stays absent, as in the family. This reverses the siblings' order, where docs come first and summaries feed Phase 8. Here the graph is the deterministic base, and the wiki track is a layer that reads it.

### 2.2 Components

The layout follows `codedoc_python`, so the variants stay diffable.

| Path | Source | Content |
|---|---|---|
| `gohelper/` | new (Go) | One binary with the subcommands `extract`, `resolve` and `version`. `golang.org/x/tools` v0.52.0 is vendored, so the build is offline and reproducible. `go test` suite with fixture modules |
| `graph_generator/__main__.py` | Python, adapted | `analyze --all/--graph/--wiki`, `setup go`, `setup spanner`, `doctor`, `trial`, `validate`, `crossref`, `evaluate`, `init` |
| `graph_generator/config.py` | Python | Family keys plus `GO_*` and `WIKI_*` (§2.5) |
| `graph_generator/go_toolchain.py` | new | Find or install the toolchain; version checks; build and cache the helper; the environment allowlist (§3.6) |
| `graph_generator/go_units.py` | new | go.mod / go.work / modules.txt parsers, build constraints, units, groups, peers → `units.json` (§3) |
| `graph_generator/go_modcache.py` | new | Offline module sources and the download mode (§3.7) |
| `graph_generator/go_extract.py` | new | Run `extract`; the entities store; declaration spans for the chunker |
| `graph_generator/resolution.py` | Python skeleton | Orchestration of groups × configurations, the artifact, stats, freshness checks |
| `graph_generator/go_classify.py` | new, Python `DeclIndex` | Location classification, node mapping, record building (§5.3) |
| `graph_generator/go_gates.py` | new | Gates on helper facts (§5.6) |
| `graph_generator/node_summaries.py` | new | Phase 2: batching, the per-node fallback, graph context, cache (§7.2) |
| `graph_generator/pipeline.py` | Python | Tracks reordered; builders `build_node_rows` / `derive_edge_rows` for the Go schema |
| `graph_generator/setup_spanner_graph.py` | Python | `NODE_SPEC` / `EDGE_SPEC` for §6 |
| `graph_generator/crossref.py`, `evaluate.py`, `trial.py` | Python / new | §8, §9 |
| `graph_query_agent/`, `mcp_server/`, `webapp/` | Python | Instruction rewritten for §6; deploy defaults (§11) |
| `scripts/` | Python | `doccheck.py`, `gql_lint.py`, `verification_table.py`, adapted |
| `test_codes/` | new | F1, F2, F3, `_gt/` (ground-truth tools, runtime oracle), `_proxy/` (synthetic module proxy) |

Python-specific parts of `codedoc_python` are not carried over:
- tree-sitter;
- pyright, and everything about LSP sessions and execution environments;
- environment discovery for nine package managers, and provisioning;
- the MRO, decorator, dunder and conditional-binding rule layer (H1–H5, R1–R11, U1, G1–G9).

The code-seams report measured them at about 60 % of `graph_generator`'s statements.

### 2.3 Outputs per repo (`OUTPUT_DIR/repos/<repo>/`)

| File | Written by | Content |
|---|---|---|
| `units.json` | Phase 1 | Modules, groups, build configurations, mode, peers, warnings (§3.8) |
| `entities.json` | Phase 1.5 | Per-file records (§4.1) |
| `resolutions.json` | Phase 1.6 | Per-site records and stats (§5.8) |
| `go/<group>/go.work`, `go/<group>/go.mod` | Phase 1.6 | Generated workspace or `-modfile` copy; the group's control directory (§3.5) |
| `graph_checkpoint.json`, `graph_meta.json` | Phases 8–9 | `ID_SCHEME`, file hashes, hash of `resolutions.json`, Spanner target, completion flag |
| `graph_rows/*.jsonl` | Phases 8–9 with `--no-spanner` | Node and edge rows, for inspection and for the trial |
| `summaries/nodes/<relpath>.json`, `summaries/dirs.json`, `summaries/topics.json` | Phases 2–5 | Summary cache keyed by content hash (§7.4) |
| `index.md`, `topics/topic_tree.json`, `topics/<topic id>.md` | Phases 4–6 | Wiki pages, in the siblings' per-repo layout; the webapp reads them per repo (`WEBAPP_REPO`, or the only repo present) |

`OUTPUT_DIR/go/` holds state shared by repos:
- `modcache/`: the isolated module cache;
- `gocache/`, `tmp/`, `xdg/`: build cache, temp files, telemetry off;
- `bin/gohelper-<go version>-<source hash>`: the cached helper binary.

### 2.4 CLI

| Command | Meaning |
|---|---|
| `analyze <dir> --all\|--graph\|--wiki` | One repo. `--repo-name N` (default: the directory name) |
| `analyze --repos manifest.json --all\|--graph\|--wiki` | Several repos; each is a peer of the others (§8.2) |
| `--peer [name=]path` (repeatable), `PEER_REPOS` | Explicit peers |
| `--download-modules`, `GO_MODULES_DOWNLOAD=1` | Download mode (§3.7) |
| `--no-spanner` | Write rows and summaries under `OUTPUT_DIR` only |
| `doctor [<dir>]` | Preflight, printed as a table: toolchain and helper build; repo `go` lines against the toolchain; groups; module availability offline, with a predicted recall loss; Spanner and Gemini configuration. `analyze` runs the same checks first and stops on errors |
| `setup go` | Installs the pinned Go toolchain (go1.27.2, SHA-256 pinned in code) into `codedoc_go/.tools/go` |
| `setup spanner` | Instance, database, tables and property graph (as in the siblings) |
| `validate` | Row counts and orphan checks from `NODE_SPEC` / `EDGE_SPEC` |
| `crossref [--dry-run]` | Cross-repo edges after ingest (§8.3) |
| `evaluate [--fixture NAME] [--env]` | The GCP-free, network-free regression gate (§9.5) |
| `trial <dir> --repo-name N [--packages P...] [--download-modules]` | The real-repo trial (§9.6) |
| `init` | Interactive `.env` |

### 2.5 Settings

The family keys stay:
- `GOOGLE_CLOUD_PROJECT` (no default, as in Python A2), `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_LOCATION`;
- `GEMINI_MODEL`, `GEMINI_CONCURRENCY`, `MAX_OUTPUT_TOKENS`;
- `SPANNER_INSTANCE`, `GRAPH_NAME`, `ID_PREFIX`, `OUTPUT_DIR`;
- `EMBED_CONCURRENCY`, `POSSIBLY_CALLS_MAX_CANDIDATES`, `GCE_METADATA_MTLS_MODE`.

`PY_*` keys go. New or changed:

| Key | Default | Meaning |
|---|---|---|
| `SPANNER_DATABASE` | `codedoc-go` | Changed default in every place that holds it |
| `GO_TOOLCHAIN` | unset | Path to a `go` binary. If unset: `codedoc_go/.tools/go/bin/go` when installed, else `go` on `PATH` |
| `GO_BUILD_GOOS`, `GO_BUILD_GOARCH` | `linux`, `amd64` | Primary build configuration |
| `GO_BUILD_TAGS` | empty | Comma-separated build tags |
| `GO_BUILD_CGO` | `0` | `1` lets the go command run gcc and pkg-config |
| `GO_EXTRA_BUILDS` | empty | Extra configurations, `goos/goarch[+tag+tag]`, separated by spaces, for files the primary build excludes (§3.4) |
| `GO_MODULES_DOWNLOAD` | `0` | `1` = `--download-modules` |
| `GO_PROXY` | `https://proxy.golang.org` | Download mode only. No `,direct` |
| `GO_USER_MODCACHE` | unset | User module cache to read offline. If unset: `go env GOMODCACHE` with the user's environment |
| `GO_HELPER_TIMEOUT` | `1800` | Seconds per helper run |
| `WIKI_FILE_MAX_CHARS` | `40000` | Above this, Phase 2 summarizes the file node by node |
| `WIKI_FILE_MAX_NODES` | `40` | Above this many declarations, likewise |
| `PEER_REPOS` | unset | `[name=]path` entries separated by `os.pathsep` |

### 2.6 Freshness

Positions from an old extraction are never paired with fresh resolution answers. The rules from `codedoc_python` §2.6 stay:
1. Phase 1 records each file's SHA-256.
2. `entities.json` and the pickle store each file's hash.
3. Every run re-extracts changed files and drops deleted ones.
4. Phase 1.6 checks hashes before and after the helper runs. A site in a file that changed during the run is `unresolved` (`stale`).
5. `evaluate` recreates its working directories.

Additions:
- **Revision stamp.** The stamp that decides entity reuse includes the extractor revision, the helper source hash and the **build configuration**. `build_included` and receiver owners depend on GOOS, GOARCH, tags and cgo.
- **Matching hashes.** The helper reports the SHA-256 of every file it loaded. Python refuses any site whose file hash differs from `entities.json`.
- **Wiki guard.** `--wiki` re-scans and compares hashes with `graph_checkpoint.json`. Any difference stops it with the list of changed files and the command to run (§7.1).

### 2.7 Smoothness

These are requirements, not extras:
- **Preflight.** One preflight (`doctor`) runs before any work and prints every problem at once, each with its fix. `analyze` stops before spending time or money.
- **Toolchain and helper.** The toolchain and the helper are found or built automatically. The helper is cached by toolchain version and source hash; a warm start costs about 0.1 s.
- **Refusals name their fix.** Examples: "run `--graph` first", "set `GO_TOOLCHAIN` to go ≥ 1.28 or run `setup go`", "37 modules are missing offline, so calls into them will be `unresolved`; add `--download-modules`".
- **Progress.** Each phase prints a progress line with counts and elapsed time. The run ends with a timing table and a status summary (sites by status, edges written).
- **Resume.**
  - Graph phases resume from `graph_checkpoint.json`.
  - Wiki summaries are cached by content hash, so a re-run summarizes only changed files and the directories and topics above them.
  - Spanner writes are idempotent upserts.
- **Inspection without Spanner.** `--no-spanner` writes everything locally, so a user can inspect a graph before loading it.
- **Budget.** Phases 1–9 on a 2,000-file repo, excluding Spanner write time, take under 2 minutes. The trial measures it (§9.6).

---

## 3. Units, workspaces and the build environment

Discovery reads files only. It never runs a go command or project code. The parsers below were checked against the go command (go-modules R1): 3,682 go.mod files with 0 disagreements, and 167,184 build-constraint decisions with 0 mismatches.

### 3.1 Units

- **A unit is a module**, meaning a directory with `go.mod`. Its key is the module path, and its display name is the same.
- **Ownership.** The deepest `go.mod` above a file owns it.
- **Skipped as the go command skips them:**
  - `testdata/`, and directories whose names start with `_` or `.`;
  - `vendor/`;
  - nested module directories, which belong to their own module.

  Symlinked files and directories whose real path is already scanned are skipped and counted. etcd has 11 symlinked test files.
- **Files under no module** are extracted for the wiki track but not resolved (reason `no-module`).
- **Parsed for each unit:**
  - from `go.mod`: `module`, `go`, `toolchain`, `require` (with `// indirect`), `replace`, `exclude`, `retract`, `tool` and `ignore`;
  - `vendor/modules.txt`, with its consistency check against `go.mod`.
- **In-repo dependencies** are modules of the same repo reached through `go.work` `use` or through a local `replace` whose target is inside the repo.
- **Packages.** A package is a directory plus a clause name. Its import path is the module path plus the relative directory; an external test package gets the suffix `_test`. The clause name can differ from the last path element: there are 9 such packages in etcd, for example `client/v3` → `clientv3`.
- **Origin:**
  - `package`;
  - `command` (`package main`);
  - `test` (`_test.go` in the same package);
  - `xtest` (external `_test` package).
- **Generated files** are flagged with `ast.IsGenerated`: a `// Code generated … DO NOT EDIT.` line before the package clause.

### 3.2 Groups and the runtime truth

A **group** is the set of modules loaded together, so one helper load answers for all of them. The group decides which code a call really reaches (go-modules R10), so it follows how the go command builds the repo:

| Repo layout | Group | Why |
|---|---|---|
| A `go.work` that lists the module | One workspace group per `go.work` | The go command finds `go.work` by walking up, so developers build in workspace mode: MVS across all `use` modules, and `go.work` replaces win |
| A module not listed in any `go.work` | One single-module group (`GOWORK=off`) | Exact single-module semantics. A sibling module that is not replaced resolves to its published version, which is external |

A call between modules is an in-repo edge exactly when the go command would compile the in-repo source for it.

### 3.3 Toolchain

- **Which toolchain.** One toolchain runs everything, found as in §2.5. The helper is built with it, because `go/types` must understand the repo's language version.
- **Too new.** Every `go.work` / `go.mod` **`go` line** is compared with the toolchain version before loading. A group whose `go` line is newer fails closed: its sites are `unresolved` (`toolchain`), and `doctor` names the line and the fix (`setup go`, `GO_TOOLCHAIN`). `GOTOOLCHAIN=local` makes sure no toolchain is downloaded.
- **`toolchain` lines.** A newer `toolchain` line does **not** fail closed. With `GOTOOLCHAIN=local`, the go command builds the module with the local toolchain whenever the `go` line is satisfied (go-modules R6; checked by the F2 fixture). The group gets a warning that stdlib targets may differ from the pinned toolchain.
- **Minimum version.** The higher of the helper's `go.mod` `go` line and 1.25 (needed for the `work` package pattern).

### 3.4 Build configurations

- **Primary.** `GOOS=linux GOARCH=amd64 CGO_ENABLED=0`, plus `GO_BUILD_TAGS`.
- **Extra** (`GO_EXTRA_BUILDS`). Each extra configuration is one more load. A file is resolved under the first configuration that includes it. real-repos checked this against gopls on 64 of 64 sites.
- **Build twins.** A **build twin** is a declaration with the same qualified name in several files of one package under mutually exclusive constraints: 30 qualnames in etcd and 68 in Prometheus. A site resolves to the twin its own configuration compiles. The other twins are recorded in `build_variants` and are not edged. This is the decided-branch case of the Python G3: the configuration decides.
- **Excluded files.** A file that no configuration includes keeps its nodes. Its sites are `unresolved` (`excluded-by-build`), with no name heuristic. That is 221 sites in etcd and 727 in Prometheus with the primary configuration alone.

### 3.5 The control directory (nothing is written into the repo)

Workspace mode writes `go.work.sum` next to `go.work`, and that happened to the research clones. Single-module downloads write `go.sum`. Plain `go list` runs `git status`, which rewrites `.git/index`. So every group gets a control directory `OUTPUT_DIR/repos/<repo>/go/<group>/`:

- **Workspace group:** a generated `go.work`, set as the absolute `GOWORK`. go-modules §2 gives the recipe:
  1. Copy the `go`, `toolchain` and `godebug` lines.
  2. Write one `use <absolute dir>` per entry. Dropping is required where the go command would fail: entries whose directory is missing are dropped with a warning, and the group is marked `degraded`. Existing out-of-repo directories are kept, because that is how the developer's build works; targets in them classify as §5.3 row 7 or row 9.
  3. Copy the `replace` lines, with local paths made absolute.
  4. Add peer replaces (§8.2).
  5. Copy the repo's `go.work.sum` beside it.
  6. If the repo has a workspace `vendor/` (first line `## workspace`), add a symlink to it; `realpath` is applied to everything.
- **Single-module group:** `GOWORK=off` plus `-modfile=<control dir>/go.mod`. This is a copy of the module's `go.mod`, plus peer replaces when the module has no `vendor/`, with `go.sum` copied beside it. The go command derives the `.sum` path from `-modfile`, so checksum writes land in the control directory. A consistent `vendor/` still applies, because the copy is identical. Milestone B1 verifies this with go/packages. Fallback: a one-`use` generated `go.work`, which ignores module `vendor/`; the docs then state that limit.

### 3.6 The environment allowlist

Every go command and every helper run gets this environment, built from scratch (go-modules §7.4). Nothing is inherited except `HOME` and `USER`.

| Variable | Offline (default) | Download mode |
|---|---|---|
| `PATH` | toolchain `bin`, `/usr/bin`, `/bin` | same |
| `GOROOT`, `GOTOOLCHAIN` | the toolchain, `local` | same |
| `GOENV` | `off` | `off` |
| `GOFLAGS` | `-mod=readonly -modcacherw -buildvcs=false`; `-mod=vendor` for a vendored group | same |
| `GOWORK` | the generated file, or `off` | same |
| `GOPROXY` | `file://<user modcache>/cache/download` if one exists, else `off` | `GO_PROXY` |
| `GOSUMDB` | `off` | `sum.golang.org` |
| `GOVCS`, `GOAUTH` | `*:off`, `off` | same |
| `GOPRIVATE`, `GONOPROXY`, `GONOSUMDB`, `GOINSECURE`, `GOCACHEPROG`, `GOEXPERIMENT`, `GODEBUG`, `CC`, `PKG_CONFIG`, `CGO_*` | unset | unset |
| `CGO_ENABLED`, `GOOS`, `GOARCH` | from the build configuration | same |
| `GOMODCACHE` | `OUTPUT_DIR/go/modcache` | same |
| `GOCACHE`, `GOPATH`, `TMPDIR`, `GOTMPDIR` | under `OUTPUT_DIR/go/` | same |
| `XDG_CONFIG_HOME` | `OUTPUT_DIR/go/xdg`, with telemetry mode `off` | same |

`-buildvcs=false` is also passed in the helper's `packages.Config.BuildFlags`, as a guard. If CodeDoc itself ever runs git on a repo, it uses `--no-optional-locks`.

### 3.7 Module sources: offline and download

- **Offline (default).**
  - A group with a consistent `vendor/` needs nothing more.
  - Otherwise, modules come from the user's module cache, read through `GOPROXY=file://<cache>/cache/download` into the isolated cache (go-modules R8). The user's cache is never written, and the isolated cache is reused by every repo and run.
  - Missing modules fail closed: calls into them, and on values of their types, are `unresolved` (`missing-module`).
  - `doctor` and `analyze` print how many required modules are missing, and that a workspace group needs the complete set of `.mod` files (go-modules R9). One missing `.mod` leaves every external package unresolved, while in-repo packages still load.
- **Download (`--download-modules`).** Before the load, run `go list -e -deps -test -compiled=false -buildvcs=false -json=ImportPath,Error <patterns>` for each group, with the download environment. This fetches exactly what the load needs, 10× smaller than `go mod download`:

  | Repo | `go list` | `go mod download` |
  |---|---|---|
  | etcd | 306 MB, 3–5 s | 3.7 GB |
  | Prometheus | 1.76 GB, 10–13 s | 2.8 GB |

  Checksums are verified against `go.sum` and sum.golang.org. No VCS tool runs. Network failures are reported per module and do not stop the run.

### 3.8 `units.json`

```json
{"version": 1, "repo": "etcd", "toolchain": "go1.27.2",
 "builds": [{"id": "linux-amd64", "goos": "linux", "goarch": "amd64", "tags": [], "cgo": false, "primary": true}],
 "mode": "offline", "module_source": "file-proxy",
 "groups": [{"id": "ws0", "kind": "workspace", "gowork": "/…/go/ws0/go.work", "modules": ["go.etcd.io/etcd/v3", "…"],
             "vendor": false, "degraded": false, "status": "ok", "missing_modules": []}],
 "units": [{"key": "go.etcd.io/etcd/server/v3", "root": "server", "go": "1.26", "toolchain": "go1.26.8",
            "group": "ws0", "requires": ["…"], "replaces": [{"old": "…", "new": "…", "local": true}],
            "in_repo_deps": ["go.etcd.io/etcd/api/v3"], "vendor": false}],
 "peers": [], "warnings": []}
```

---

## 4. Extraction (Phase 1.5)

### 4.1 Contract: `entities.json` version 1

Produced by `gohelper extract`, which uses `go/parser` only. Positions:
- taken with `PositionFor(p, false)`, so `//line` directives are ignored;
- lines are 1-based;
- columns are **1-based byte columns**.

No language server is involved, so there is no UTF-16 conversion.

Records are keyed by repo-relative POSIX path. Each record has:

| Field | Meaning |
|---|---|
| `sha256`, `module`, `package`, `package_name`, `origin`, `generated` | §3.1 |
| `build_constraint`, `build_included` | The `//go:build` expression (or the legacy `// +build` lines); whether the primary configuration compiles the file |
| `syntax_errors` | `go/parser` error lines. Declarations after the first error are dropped, and sites there are `unresolved` (`syntax-error`). This is Python's A4 rule |
| `imports[]` | `path`, `alias`, `kind` (`normal`, `dot`, `blank`), `line`, `col` |
| `types[]` | `name`, `qualname`, `kind` (`struct`, `interface`, `named`, `alias`), `type_params`, `embeds[{text, line, col, pointer}]`, `methods[]` for interfaces (each with `name`, `qualname`, `name_range`), `name_range`, `start_line`, `end_line`, `doc` |
| `funcs[]` | `name`, `qualname`, `member_kind` (`func`, `method`, `init`, `closure`, `var_init`), `receiver{text, base, pointer}`, `type_params`, `signature`, `has_body`, `linkname`, `name_range`, `start_line`, `end_line`, `doc`, `sites[]` |
| `sites[]` | `kind` (`call`, `go`, `defer`, `range`), `name` (callee token), `line`, `col`, `end_col`, `offset` (byte offset of the callee token), `shape` (`ident`, `selector`, `index`, `funclit`, `paren`, `call`, `other`), `receiver` text, `caller` qualname |

Every file has a `<package>` pseudo-type of kind `package`. It owns:
- the file's functions;
- its `init#k` functions;
- the `<init>` pseudo-function, which is the caller of package-level variable initializers.

### 4.2 Qualified names (code-seams §3.2)

| Declaration | Qualname |
|---|---|
| Function | `F` |
| Method, value or pointer receiver | `T.M`. The receiver (`*T`) goes in a column, because a type cannot declare both. Type parameters are never part of a name |
| Interface method | `I.M` |
| Closure | `F.func1`; nested `F.func1.func1`; ordinal per parent in source order. Inside `<init>`: `<init>.func1` |
| `init` functions | `init#1`, `init#2`, numbered per file |
| Blank function `func _()` | `_#1`, … |
| Package-level variable initializers | `<init>`, one per file |
| Pseudo-type | `<package>` |

On both trial repos (1,840 files) this grammar gave no duplicate (relpath, qualname) pair. The `_claim_id` guard from the siblings stays.

### 4.3 Sites and callers

**Sites:**

| Shape | Site? |
|---|---|
| `f(x)`, `x.f(x)`, `pkg.F(x)`, `G[T](x)`, `m[k](x)` | `call`. For an `index` shape the token is the base identifier |
| `go f()`, `defer x.f()` | Kind `go` / `defer` |
| `func(){…}()`, also under `go` and `defer` | A site with shape `funclit`, whose token is the `func` keyword. The closure node is the syntactic target |
| `for v := range f` where `f` names a function or method value of func type | Kind `range` (implicit call). A range over a call result `x.All()` keeps the inner call site only |
| Conversions: `T(x)`, `[]byte(s)`, `(*T)(p)` | Classified by the helper in Phase 1.6, then dropped and counted (`conversion`). Excluded from every rate |
| `f()(x)` | The inner call only (Python A3) |
| Calls inside type expressions (array lengths) | Not sites |

**Callers:**

| Where the site is | Caller |
|---|---|
| A function or method body | That function |
| A closure body | The closure |
| A package-level variable initializer | `<init>` of that file |
| An `init` function | `init#k` |

### 4.4 Chunking for Gemini

The chunker splits by top-level declaration spans from the entities, with a budget in characters. It keeps the siblings' guard against an endless loop.

---

## 5. Resolution (Phase 1.6)

### 5.1 The helper load

For each group and each build configuration, `gohelper resolve` calls `packages.Load` with:
- mode `NeedName|NeedFiles|NeedSyntax|NeedTypes|NeedTypesInfo|NeedDeps|NeedImports|NeedModule`, `Tests: true`;
- pattern `work` for workspace groups, `./...` for single-module groups;
- the §3.6 environment and `-buildvcs=false`.

It emits one JSON line per site, plus per-type facts. Every position it reports — sites and targets — is **unadjusted** (`PositionFor(p, false)`), exactly as in extraction, so a `//line` directive can never redirect a target to another file's line (spec-review probe p3). `NeedDeps` is needed, because the open-world check (§5.4) must see unexported implementers in dependencies.

**Measured (2026-10-13):**

| Repo | Time | Peak memory |
|---|---|---|
| etcd | 2.3 s | 1.5–1.7 GB |
| Prometheus | 5.7 s | 4.7–5.3 GB |

**Loader traps** that the helper handles (real-repos §6.2, §7):

| Trap | Handling |
|---|---|
| go/packages attaches in-repo files to the stub of a failed import (golang/go#39986) | A (file, package) pair is accepted only when the package belongs to a workspace main module |
| A stub can take a real import's name | A target whose package is not a real workspace, dependency or stdlib package is rejected (`missing-module`) |
| Package variants of one file: production `p`, test variant `p [p.test]`, a symlinked second path | Sites are deduplicated by (real path, offset). If two variants name different targets, the site is `ambiguous` (`variant`) with both |

### 5.2 Static classification (helper)

Per site, from `types.Info.Uses`, `Selections` and `Instances`:

| Callee | Helper class | Notes |
|---|---|---|
| Builtin (`len`, `append`, `min`, `unsafe.Sizeof`, …) | `builtin` | |
| A type | `conversion` | Dropped from the sites and counted |
| Package-level function, generic or not | `func` | Target = the generic origin (`Origin()`) |
| Concrete method, including methods promoted through embedding and method expressions | `method` | Depth, receiver kind and embedding path recorded |
| Interface method, including an anonymous interface, `error`, and calls through an embedded interface field | `iface` | §5.4 |
| Method on a type parameter, through its constraint | `typeparam` | Treated as `iface` over the constraint |
| Func-typed variable, parameter, field, map or slice element | `value` | §5.5 |
| Func literal called directly | `funclit` | Target = the closure |
| `//go:linkname` pull stub (body-less declaration with a linkname) | `linkname` | Retargeted to the named symbol |
| Selector on an invalid type, or a failed lookup | `unknown` | `unresolved` (`missing-module` or `load-error`) |

Each record carries:
- the target object's position (real path, line, column of the name);
- its package path, module path and version;
- the flags the gates need.

### 5.3 Location classification (Python)

First match wins. All paths are real paths.

| # | Location | Result |
|---|---|---|
| 1 | `$GOROOT/src/builtin/` or `unsafe/` | `builtin` |
| 2 | Other `$GOROOT/src/` files | `external`, module `std` |
| 3 | A peer repo's module directory (§8.2) | `external` with `{repo, relpath, qualname}` for `crossref` |
| 4 | The isolated module cache (`<module>@<version>/…`) | `external`, module path and version from the cache path (`!`-decoded) |
| 5 | A `vendor/` directory | `external`, module from `modules.txt` (longest prefix) |
| 6 | A scanned file of this repo | In-repo declaration lookup by name range (`DeclIndex`) |
| 7 | A file of this repo that is not scanned (skip dirs, symlink duplicates) | `unresolved` (`unscanned`) |
| 8 | A `$GOCACHE` cgo file (`_Cfunc_…`) | `external`, module `C` |
| 9 | Anything else (an out-of-repo `use` or `replace` directory that is not a peer) | `unresolved` (`outside-repo`) |

**Target mapping (exact).** An in-repo target maps to a node only when its position equals the **name range of a node of the expected kind** (a `Methods` node for func, method and interface-method targets; a `Classes` node for type targets). There is no fallback to an enclosing declaration: targets inside function bodies (local types) or inside type literals and inline constraints never map to the enclosing function. The helper reports interface types that are not package-level named types — anonymous literals, function-local named interfaces, inline constraints — with `iface.recv_type == ""`, and Python treats them like anonymous interfaces (§C5 of the plan: `ambiguous` with in-repo candidates, else `dynamic`). Any other unmapped in-repo target is `unresolved` (`no-declaration`).

### 5.4 Interface dispatch: open world

Go interfaces are satisfied by any type with the right method set, wherever it is declared. So:

1. **The static target.** An `iface` site is `resolved` to the interface method's node (via `types`) when the interface is in-repo, and `external` otherwise. This is the static binding, and it is what C#, Java and Python edge for abstract targets.
2. **Candidates.** Candidates come from the **receiver's static type**, not from the interface that declares the method.
   - For each in-repo, package-level, non-generic named type T, check `types.Implements` for T and for *T. The candidate is T's method M (promoted if needed).
   - If T gets M through its own embedded interface field, that is a second dispatch: the candidate is the embedded interface method, flagged.
   - **Flags.** `test_only` marks a candidate declared in a `_test.go` file. `open_world` marks a site where some non-repo type, stdlib or dependency and unexported ones included, also implements the interface, or an in-repo type that cannot be a candidate does (a function-local type, an anonymous struct type, an instance of a generic type), or another group builds the site's module from its in-repo directory (R54).
   - **Measured.** This costs 0.1–0.5 s per repo.
3. **Edges.** The interface method gets a `MethodCalls` edge. Each in-graph candidate gets a `PossiblyCalls` edge with reason `implementation`, up to `POSSIBLY_CALLS_MAX_CANDIDATES`. Above the cap, no rows are written and the site is counted. `open_world` is a column on the `PossiblyCalls` row.
4. **No upgrade.** A single in-repo implementer is never promoted to a `MethodCalls` edge.
   - On etcd, `testutil.TB` has one in-repo implementer, and `*testing.T` also satisfies it.
   - In a probe, the runtime target of an in-repo `Logger.Printf` call was the stdlib `log.(*Logger).Printf`.

   VTA-based pruning is later work (§13); it can shrink candidate sets but never justify an edge.

### 5.5 Rules (static targets that need a proof)

Each rule fires only on its trigger, and records `via = convention:<rule>`.

| Rule | Trigger | Result |
|---|---|---|
| `funclit` | A func literal called directly, including `go func(){}()` and `defer func(){}()` | `resolved` to the closure node |
| `func-alias` | A `value` call whose variable is a **local** variable bound exactly once, all proven from `go/types`: declared with an initializer that is a function, a method value with a concrete receiver, or a func literal; never assigned again; never address-taken; not assigned inside a closure | `resolved` to the bound target. A method value with an interface receiver behaves like an `iface` site |
| `linkname` | A body-less declaration with `//go:linkname local target` | An in-repo `target` → `resolved` to it; otherwise `external`. Body-less declarations without a linkname are assembly: the record is `resolved` to the declaration, marked `asm` |
| `range` | A `range` site over a function or method value | `resolved` as a call of that function or method |

Package-level func variables are not proven single, because tests can reassign them. They stay `dynamic`, and so do fields, parameters, maps and slices of funcs.

### 5.6 Gates

| Gate | Condition | Result |
|---|---|---|
| G1 `variant` | Package variants of one file disagree on the target (§5.1) | `ambiguous` (`variant`), candidates = the targets |
| G2 `missing-module` | The target is in a stub package, or the receiver's type is invalid | `unresolved` (`missing-module`) |
| G3 `incomplete-type` | A promoted selection (call, method value bound by `func-alias`, `range` over a promoted method value, method expression) whose receiver's embedding tree has an invalid-typed embedded field at a shallower-or-equal depth — **in any package**, with or without import errors in the calling package (the bad field may sit in a type of another in-repo package; spec-review probe p2). Evaluated per package variant; the most conservative answer wins. A missing shallower method would win at runtime (real-repos mechanism 3) | `unresolved` (`incomplete-type`) |
| G4 `iface-offline` | `iface` candidate search in a package with import errors, or with an interface or candidate whose method set involves an invalid type | No candidates (`PossiblyCalls` none). The static target stays |
| G5 `excluded` | The site's file is compiled by no configuration | `unresolved` (`excluded-by-build`) |
| G6 `toolchain` | The group failed the §3.3 check | `unresolved` (`toolchain`) |
| G7 `no-declaration` | The target is in a scanned file but no node contains it | `unresolved` (`no-declaration`) |
| G8 `stale` | The file, or the target's file, changed during the run | `unresolved` (`stale`) |
| G9 `syntax-error` | The site lies in an error region (§4.1) | `unresolved` (`syntax-error`) |
| G10 `build-variant` | The target has build twins (§3.4) | `resolved` to the compiled twin; the others are listed in `build_variants`, with no edge |

### 5.7 Statuses, provenance, reasons

| Status | Meaning | Graph |
|---|---|---|
| `resolved` | One in-repo target: a function, method, closure or interface method | `MethodCalls` |
| `external` | Target outside the repo: stdlib, a module, C, or a peer repo | Counted per module; peer targets go to `crossref` |
| `builtin` | A builtin | Counted; excluded from the edge-rate denominator |
| `ambiguous` | Several targets (`variant`) | `PossiblyCalls` to each in-graph candidate |
| `dynamic` | A func value | Counted, never edged |
| `unresolved` | No certain answer | Counted, never edged. Go has no name-heuristic `PossiblyCalls`: unresolved sites are exactly the places where type facts are missing, and a guess there is noise (code-seams §7) |

**Vocabularies:**
- **`via`:** `types`, `convention:funclit`, `convention:func-alias`, `convention:linkname`, `convention:range`, `none`.
- **`PossiblyCalls` reasons:** `implementation`, `variant`.
- **`unresolved` reasons:** `missing-module`, `incomplete-type`, `excluded-by-build`, `toolchain`, `no-declaration`, `stale`, `syntax-error`, `unscanned`, `outside-repo`, `no-module`, `load-error`.

### 5.8 `resolutions.json`

```json
{"version": 1,
 "engine": {"name": "go/types", "toolchain": "go1.27.2", "x_tools": "v0.52.0", "helper": "sha256:…",
            "entities_version": 1, "mode": "offline|download", "builds": ["linux-amd64"]},
 "repo": "iryo",
 "groups": {"ws0": {"status": "ok", "load_seconds": 1.4, "packages": 183, "errors": 0}},
 "files": {"libs/billing/fee.go": {
   "sha256": "…",
   "calls": [{"site": {"caller": "FeeCalculator.Total", "kind": "call", "name": "Calculate", "line": 41, "col": 15},
              "status": "resolved", "via": "types",
              "target": {"unit": "example.com/iryo/billing", "package": "example.com/iryo/billing",
                         "relpath": "libs/billing/fee.go", "qualname": "FeeCalculator.Calculate", "line": 12},
              "static_target": null, "external": null, "candidates": [], "open_world": false,
              "build_variants": [], "reason": null, "build": "linux-amd64"}],
   "imports": [], "embeds": [], "implements": []}},
 "stats": {"by_status": {}, "by_via": {}, "by_reason": {}, "by_class": {}, "external_by_module": {}, "conversions": 0}}
```

- It is written atomically and rebuilt on every run; the helper is fast enough.
- Phase 9's checkpoint stores its hash and restarts edge writing when the hash changes.
- `imports` hold resolved import records. A target package can be in-repo (a package node) or external (a module).
- `embeds` hold embedded-field types, resolved by `go/types`.
- `implements` hold (concrete type, in-repo interface, pointer) pairs of the primary build. Pairs involving invalid types are skipped.

---

## 6. Graph schema and identity

### 6.1 Node tables (6)

| Table | Columns |
|---|---|
| `Files` | file_id, file_name, extension, directory, path, origin (package / command / test / xtest), generated, package (import path), package_name, module (module path), build_constraint, build_included, summary, repo, embedding |
| `Packages` | package_id, import_path, name, directory, module, origin, file_count, summary, repo, embedding |
| `Classes` | class_id, name, qualname, package, fqcn (= import path + "." + qualname), file_id, kind (struct / interface / named / alias / package), type_params, embeds, start_line, end_line, origin, module, summary, repo, embedding |
| `Methods` | method_id, name, class_id, file_id, qualname, fqmn, signature, member_kind (func / method / interface_method / init / closure / var_init), receiver, type_params, has_body, start_line, end_line, origin, module, entry_point, summary, repo, embedding |
| `Topics` | topic_id, name, summary, repo, embedding. Renamed from the family's `Modules` (D7) |
| `Directories` | Unchanged |

- **`Classes` and `Methods` keep the family names.** The agent instruction says that `Classes` holds Go types.
- **`entry_point`** is the command name (the directory name) of a `func main`.
- **Interface methods** are `Methods` rows owned by their interface's `Classes` row.
- **Methods outside their type's file.** A method declared in another file than its type keeps its own `file_id`, and its `class_id` points at the type's row. This happens for 105 methods in etcd and 302 in Prometheus.

### 6.2 Edge tables (14)

Flag columns (`generated`, `build_included`, `has_body`, `pointer`, `pointer_receiver`, `open_world`) are Spanner `BOOL`; the schema spec gains a `bool` type shorthand. `resolution` columns carry the record's `via` (`types`, `convention:*`); structural edges use `types`.

| Table | Columns | Written by |
|---|---|---|
| `PackageImports` | edge_id, source_file, target_package, import_path, alias, kind | Phase 9 |
| `FileDependsOn` | edge_id, source_file, target_file, resolution | Phase 9 |
| `ClassEmbeds` | edge_id, child_class, embedded_class, pointer, resolution | Phase 9 |
| `ClassImplements` | edge_id, class_id, interface_id, pointer_receiver, resolution | Phase 9 |
| `MethodCalls` | edge_id, caller_method, callee_method, callee_name, resolution, site_kind (call / go / defer / range), call_line | Phase 9 |
| `PossiblyCalls` | edge_id, caller_method, callee_method, callee_name, reason, candidate_count, open_world | Phase 9 |
| `FileDefinesClass`, `ClassDefinesMethod`, `DirContainsFile`, `PackageContainsFile` | as in the siblings, plus the package link | Phase 9 |
| `FileBelongsToTopic` | edge_id, file_id, topic_id | Phase 10 (wiki) |
| `CrossRepoCalls` | edge_id, caller_method, callee_method, source_repo, target_repo, symbol, site_kind | `crossref` |
| `CrossRepoRef` | edge_id, source_class, target_class, source_repo, target_repo, symbol, kind (embeds / implements) | `crossref` |
| `CrossRepoImports` | edge_id, source_file, target_package, source_repo, target_repo, import_path | `crossref` |

### 6.3 Edge derivation (Phase 9, pure `derive_edge_rows`)

- **`MethodCalls`:** from `resolved` records whose target is a `Methods` node of this repo, including interface methods and closures.
- **`PackageImports`:** from import records whose target is an in-repo package. External imports are counted per module.
- **`ClassEmbeds` / `ClassImplements`:** from the `embeds` / `implements` records whose ends are both in-repo nodes.
- **`FileDependsOn`:** the files reached by `MethodCalls` and `ClassEmbeds`, excluding the file itself.
- **`PossiblyCalls`:** one row per in-graph candidate of `iface` sites and of `ambiguous` records, within the cap.
- **Edge IDs and rows.** Edge IDs hash (table, source, target). Rows are deduplicated per pair and keep the first site's line and kind.

### 6.4 Node IDs

`_make_id(kind, repo, relpath, qualname)`, with `ID_SCHEME = 1`:

| Node | Inputs |
|---|---|
| File | (`file`, repo, relpath, "") |
| Package | (`package`, repo, directory relpath, clause name) |
| Class | (`class`, repo, relpath of the declaration, qualname or `<package>`) |
| Method | (`method`, repo, relpath of the declaration, qualname) |
| Directory | (`dir`, repo, directory relpath, "") |
| Topic | (`topic`, repo, topic name, "") |

The module path is a column, not an input, so renaming a module does not change node IDs.

### 6.5 What the agent instruction covers

- the tables and columns;
- `Classes` = Go types, and the `<package>` pseudo-type;
- `Topics` against Go modules (`Files.module`);
- the qualified-name grammar: closures, `init#k`, `<init>`;
- that interface calls are edges to the interface method, with implementers in `PossiblyCalls` and the `open_world` flag;
- what each status and reason means;
- module and package columns for questions across boundaries, and the cross-repo tables;
- a GQL cookbook, written as an InstructionProvider so that qualnames with `#` and `<…>` are safe:
  - who calls X; what X calls;
  - implementers of an interface;
  - embedding tree;
  - package dependencies;
  - calls between two modules, and between repos.

---

## 7. The wiki track (`--wiki`)

### 7.1 Preconditions

`--wiki` stops with a message naming the fix when any of these fails:

| Precondition | Message names |
|---|---|
| `graph_checkpoint.json` exists and is complete for this repo | `run --graph first` |
| `ID_SCHEME` matches | `run --graph first` |
| Phase 1's re-scan finds the same set of file hashes as the graph | The list of changed files, and `run --graph` (or `--all`) |
| The Spanner target recorded in `graph_meta.json` equals the configured one | Both targets |
| If the graph was built with `--no-spanner`, `--wiki` also runs with `--no-spanner` | Summaries and pages are then written locally only |

### 7.2 Phase 2: node summaries

**Units of work** are files. A batched call carries:
- the file's source;
- a list of its declarations: types, functions, methods and interface methods (closures excluded);
- for each declaration, its **graph context** from the finished graph:
  - for functions and methods, up to 10 callees and 5 callers (`MethodCalls`), and its interface candidates;
  - for types, their embeds, implements and methods.

The call asks for JSON: `{"file": "...", "nodes": {"<qualname>": "..."}}`.

**Large files go node by node.** When the source exceeds `WIKI_FILE_MAX_CHARS`, or the file has more than `WIKI_FILE_MAX_NODES` declarations:
1. Each declaration gets its own call, with its source span and the same context.
2. The file summary is written from the node summaries.

**Gaps.** Nodes missing from a batched answer are retried one by one. The siblings' retry, cooldown and adaptive throttle stay.

**Closures** get no Gemini call. Their summary is their doc comment, or a short synthetic line naming the enclosing function.

### 7.3 Phases 3–6

- **Packages:** one call per package, from its files' summaries.
- **Directories:** bottom-up, as in the siblings.
- **Topics:** as in the siblings, with one fix. Topics link files by **relpath**, not by basename: basename linking drops 42.8 % of etcd's files, `doc.go` alone 67 times.
- **Topic pages and index:** as in the siblings.

### 7.4 Cache and resume

- **Node summaries** are stored per file in `summaries/nodes/<relpath>.json` with the file's SHA-256. An unchanged file is not summarized again.
- **Package and directory summaries** are keyed by the hash of their children's summaries.
- **Topics and pages** are rebuilt when any summary changed.

So `--graph` followed by `--wiki` after a small edit costs a few calls.

### 7.5 Phase 10: write-back

- **Summaries.** Batched `update` mutations set the summary columns of `Files`, `Packages`, `Classes`, `Methods` and `Directories`.
- **Topics.** `Topics` and `FileBelongsToTopic` rows are upserted.
- **Embeddings** cover `Files`, `Packages`, `Classes`, `Methods` (closures excluded) and `Topics`.

The siblings embed only Files, Classes and Modules. Methods are added here because they now have real summaries.

---

## 8. Cross-project

### 8.1 Within one repo

- Each module is a unit.
- In workspace groups, calls between modules resolve to in-repo source, exactly as the go command builds them.
- In single-module groups they do so only through a local `replace`; otherwise they are `external` with the published version.

Both ends carry `module` columns, which show the boundary.

### 8.2 Across repos: peers

- **Sources.** Peers come from `analyze --repos manifest.json` (every repo in the manifest is a peer of the others), `--peer [name=]path` and `PEER_REPOS`. The name must equal the name under which that repo is, or will be, ingested.
- **Discovery.** A peer is discovered like a local repo, from files only.
- **Substitution.**
  - **When.** A peer module is substituted only when some module in the group **requires** its module path, directly or indirectly. Substitution is a `replace <module path> => <absolute peer module dir>`, in the generated `go.work` or in the `-modfile` copy.
  - **Effect.** The load then reads the peer's source. This is what a developer's local `go.work` / `replace` does, and the docs state that the peer's source stands in for the required version.
  - **Not substituted:**
    - a module path provided by two peers (a fork): reported, and imports into it resolve as without peers;
    - a vendored group: its vendored copy wins.
- **Classification.** Targets under a peer's module directory are `external` with `{repo, relpath, qualname}` (§5.3 row 3).

### 8.3 `crossref` (post-ingest)

`crossref` reads every ingested repo's `resolutions.json` and `entities.json`. A peer target whose repo is ingested and whose node exists becomes an edge. The node ID is computed with the same `_make_id`; no name lookup is involved.

| Peer target | Edge |
|---|---|
| Call site with a method target | `CrossRepoCalls` |
| Import | `CrossRepoImports` |
| Embed | `CrossRepoRef` kind `embeds` |
| Implements | `CrossRepoRef` kind `implements` |

**Module-cache targets of an ingested repo's module path are not matched.** The cache copy is a published version, not the ingested source. They are listed in `crossref_report.json` with the hint `--peer`. The report also lists drops: no owner, two owners, node missing, repo not ingested.

---

## 9. Corpus, ground truth, oracle, evaluate, trial

### 9.1 Fixtures

All fixtures pin `GOOS=linux GOARCH=amd64 CGO_ENABLED=0` and the toolchain line `go 1.26`, which go1.27.2 understands.

**F1 `iryo-kanri-system`.** A Go port of the C#/Java/Python corpus domain (medical management).
- **Repo and modules.** One repo with about 12 modules, named like the C# projects. Most are joined by a root `go.work`. One module stays outside it, with a local `replace` to an in-repo module and a published-version requirement on another, so §3.2's runtime truth is exercised.
- **Apps.** `reception-desk`, `admin-console` and `patient-portal` (net/http).
- **Size.** About 120–150 cases and about 90 declaration pins.

**F1 categories:**

| Prefix | Content |
|---|---|
| XPR | Byte-identical call lines that bind differently through imports (C# parity) |
| TWIN | Same-named methods in different modules (`Logger.Log`, `Validator.Validate`, `ReceiptFormatter.Format` ×3) |
| EMB | Promotion at depth 1/2/3, shadowing, pointer embedding, embedded interfaces (second dispatch) |
| IFC | Interface calls: static target plus candidates, 0/1/n implementers, stdlib-satisfied (open world), test-only fakes, sealed interfaces |
| GEN | Generic functions, methods of generic types, constraint calls, generic aliases |
| FV | Func values, the single-binding local alias, IIFE, `go` / `defer`, method values, func fields and maps (`dynamic`) |
| BLT / CNV | Builtins, conversions to named and func types |
| BLD | Build twins, files excluded by tag or GOOS |
| TST | In-package and external tests, `export_test.go`, a test-variant shadowing case |
| GNC | Generated code |
| INI | `init#k` and `<init>` callers |
| EXT | Stdlib and third-party modules |
| LNK | `//go:linkname`, asm declarations |
| MOD | Cross-module calls: workspace vs replace vs published version |
| UNR | Negative controls |

**F2 `go_layouts`.** About 20 units and 45 cases, covering:
- single and nested modules;
- `go.work` with and without a workspace `vendor/`;
- module `vendor/`;
- local replaces inside and outside the repo;
- a missing `use` directory;
- a `go` line newer than the toolchain (fails closed);
- missing `go.sum` lines;
- a directory without a module;
- `testdata/`, `_x/` and `.x/`;
- a second package in one directory behind `//go:build ignore`;
- symlinked files;
- cgo with `CGO_ENABLED=0`;
- `//line` directives;
- a dot import.

**F3 `multi_repo`.** The repos `shared`, `web` and `api`, with an exact cross-repo edge set through peers. Negative controls:
- the same module path in two repos (a fork);
- an undeclared peer dependency;
- a vendored peer copy;
- a peer that is also published in the synthetic proxy.

**Third-party modules** in the fixtures come from `test_codes/_proxy/`, a committed `file://` module proxy of small synthetic modules (`example.com/thirdparty/...`). Using it needs no network and raises no licence question.

### 9.2 Ground-truth chain (C# style)

1. **`ground_truth/cases.py` is the source of record.** Each case has `file`, a `snippet` unique in the file, `token`, `expect`, `category` and `why`.
   - **`expect`:**
     - `status`;
     - `targets[{qualname, module, kind, file}]`;
     - `candidates` (subset rule);
     - `open_world`;
     - `runtime_targets` (documented alternates);
     - **distractors:** plausible wrong targets that must never be named (C#'s distractor field).
   - **Interface cases** expect the interface method as the target, and list the implementers the oracle observes in `runtime_targets`.
2. **`generate_manifest.py` → `manifest.json`.** Line numbers are derived from the snippets.
3. **`check_ground_truth.py`** is standalone, uses the stdlib only and imports nothing from `graph_generator`. It checks:
   - snippet uniqueness and round trip;
   - anchors on declaration lines;
   - ID bijection;
   - unit truthfulness: modules parse, packages lie under their module, declared requirements exist in the fixture or the proxy;
   - XPR byte identity.
4. **The scorer inside `evaluate.py`** gives each case one verdict: correct, documented alternate, wrong or unanswered.

Resolver output may be stored only as a `today.*` baseline. It never fills `expect`.

### 9.3 Runtime oracle (`goracle`)

`test_codes/_gt/oracle/` holds `goracle`, a standalone Go program that uses the stdlib only (about 1,900 lines with its runtime package). It is ported from the runtime-oracle research prototype and is the Go counterpart of `trace_oracle.py`.

- **Instrumentation of a copy.** Every function body and func literal starts with a hook, `__goracle.E(fid)`, that records the caller's return PC: a frame-pointer fast path, with each new (function, PC) pair verified once against `runtime.Callers`.
- **One id per call site.** A `/*line FILE#SID:L:C*/` directive before each call's `(` restores the position right after it, so `Frame.File` carries the site id with the true line. Line granularity is not enough: 38.7 % of etcd's call sites and 40.2 % of Prometheus's share a line with another call. Tagging every site (mode `all`) makes it impossible to misattribute implicit calls (deferred calls at `return`, range-over-func iterators, panics).
- **Completion counters.** A counter after each simple statement detects calls that entered uninstrumented code (stdlib, dependencies): `exec > hits`.
- **Wrappers and goroutines.** `runtime.Callers` elides `-fm`, pointer-receiver, `deferwrap`/`gowrap` and reflect wrappers. A goroutine entry recovers its `go` statement from the "created by" line.
- **Build.** `-gcflags=all=-l -ldflags=-checklinkname=0 -vet=off`; pinned to Go 1.27.2.
- **Measured (2026-10-13).** All 6 etcd and all 5 Prometheus test packages pass instrumented; `promql` takes 47 s, against 8.4 s uninstrumented.
- **Modes:**
  - `instrument` and `report`;
  - `match`, which scores resolver records (§9.4);
  - `judge`, which scores ground-truth cases with codedoc_python's rule: `observed ⊆ targets ∪ runtime_targets`, and every `runtime_target` observed.

  It runs the F1 apps' demo entry points and the fixtures' tests. Unexecuted cases are listed.

### 9.4 Verdicts against the oracle

| Verdict | Rule (research report §5.2) | Counts as |
|---|---|---|
| CONFIRMED | callees = {target} and `exec == hits` | correct |
| CONFIRMED-NOEXEC | callees = {target}; the site has no counter (return, `if`/`for`/`switch` header, `go`/`defer`) | correct, own row |
| IFACE-CONSISTENT | the target is an interface method, and every observed callee is a method of that name on a type that implements the interface | correct, own row |
| DEFER-GROUP-CONSISTENT | a defer site in a function with ≥ 2 defers; the target is in the group's observed set | correct at group level, own row |
| WRONG-OTHER, WRONG-MULTI, IFACE-OTHER, DEFER-GROUP-OTHER | only another callee; the target plus another callee, or `exec > hits`; a callee that does not implement the interface; a target outside the group set | **wrong** |
| UNEXERCISED, UNVERIFIABLE-TARGET | the site never executed; the target has no hook (assembly) | says nothing |

The "0 wrong" claims of the trial and of `evaluate`'s oracle check count every WRONG-* verdict.

The same rule scores the real-repo precision check.

### 9.5 `evaluate`

- **Fixtures.** `--fixture` takes `iryo-kanri-system`, `go_layouts` or `multi_repo`; the default is all three.
- **Offline mode (default).** An empty module cache and `GOPROXY=off`. Third-party calls end as `unresolved` (`missing-module`), which is scored as unanswered, not wrong.
- **Env mode (`--env`).** `GOPROXY=file://test_codes/_proxy`, `GOSUMDB=off` and an isolated cache. No network.
- **Gates:**

  | Gate | F1 | F2 | F3 |
  |---|---|---|---|
  | 0 wrong (distractors included) | yes | yes | yes |
  | Completeness ≥ floor(0.85·N)+1 | yes | yes | yes |
  | Node coverage ≥ 0.85 over pins | yes | — | — |
  | Edge rate ≥ 0.85 | yes | — | — |
  | QA ≥ 85 % | yes | — | — |
  | Cross-repo edge set exact | — | — | yes |

- **Definitions.**
  - **Edge rate:** over every non-builtin, non-conversion call site of the fixture, the share whose status is `resolved` or `external`.
  - **Wrong:** another in-repo target; another module; a distractor; an edge where `ambiguous`, `dynamic` or `unresolved` is expected; `external` where an in-repo target is expected.

### 9.6 Real-repo trial (`trial`)

`python -m graph_generator trial <dir> --repo-name N [--download-modules] [--packages P...]`. It runs:

1. **The graph track with `--no-spanner`**, offline, then in download mode. It reports:
   - files, packages and sites by class, status and reason;
   - edges by table;
   - time per phase and peak memory;
   - the offline recall loss against download mode, and any **changed** answer between the modes. Every changed answer must be explained or gated.
2. **The precision check.**
   1. The oracle instruments a copy of the repo.
   2. It runs the unit tests of the chosen packages, skipping tests that need network or ports.
   3. Every `MethodCalls` edge is scored with §9.4.
   4. `PossiblyCalls` recall is reported: how many observed interface targets were among the candidates.

Default packages: those named in the runtime-oracle report for etcd and Prometheus. The outputs go to `OUTPUT_DIR/trial/<repo>/`, and the dated numbers go to `docs/LSP_RESOLUTION.md`.

---

## 10. Testing

- **pytest default run.**
  - It needs no Go toolchain, no network and no GCP.
  - It uses the siblings' fakes for Gemini, Spanner and embeddings.
  - Helper behaviour comes from **recorded helper outputs** under `tests/fixtures/helper/`, tagged with the helper revision.
  - `--cov-fail-under=85`.
- **pytest markers.**
  - `-m go`: tests that build and run the real helper.
  - `-m network`: download mode against the real proxy.
- **Go tests.** `go test ./...` in `gohelper/` covers the extractor and the resolver on small fixture modules, including the three offline traps and the open-world flag.
- **Unit coverage:**
  - **Discovery:** parsers, groups, the control directory, environment building, toolchain checks.
  - **Resolution:** classification rows, each gate and rule, the open-world flag, variant disagreement.
  - **Graph:** builders and schema.
  - **Wiki:** batching, the per-node fallback, the cache, preconditions, write-back.
  - **Other:** crossref, evaluate scoring, the trial's matching rule.
- **No sleeps.** No test sleeps through a real cooldown; the sleep is injected.
- **End to end.** `evaluate` is the end-to-end gate. No test calls GCP.

---

## 11. Query stack, deploy, docs

- **`graph_query_agent`.** The Python variant's setup, with the instruction rewritten per §6.5. The model and database come from the root `.env`. No nested `.env`.
- **`mcp_server`, `webapp`.** The Python variant's versions.
- **`deploy.sh` defaults:**
  - Spanner `codedoc-instance` / **`codedoc-go`**;
  - service and image **`codedoc-go-mcp`** in the Artifact Registry repo `codedoc`;
  - service account `codedoc-mcp-sa`.
- **Docs.** Japanese, in the five-file split, MECE, in the existing style. The running example is F1's `FeeCalculator.Calculate` in module `example.com/iryo/billing`.
  - `README.md`: prerequisites (a Go toolchain, or `setup go`), setup, the configuration table, the CLI and recipes for the three modes, outputs, freshness and resume, offline vs download, peers, troubleshooting.
  - `docs/ARCHITECTURE.md`: phases and tracks, components, data flow, checkpoints, the helper, the query stack, design decisions.
  - `docs/LSP_RESOLUTION.md`. The family file name is kept for parity (D10); its first paragraph states that Phase 1.6 uses go/types through the helper, not a language server. It covers:
    - the problem cases;
    - why go/types and not gopls;
    - groups and the runtime truth;
    - classification, interface dispatch, rules, gates, statuses;
    - `resolutions.json`;
    - verification numbers with dates;
    - limits: the open world, no points-to analysis, cgo, GOPATH.
  - `docs/GRAPH_SCHEMA.md`, `docs/CROSS_PROJECT.md`: as in the siblings.
- **Monorepo.** The root `.gitignore` gains `codedoc_go/.tools/`. `CODEDOC_COMPARISON.md` is not changed.

---

## 12. Delivery plan

The detailed plan comes from the writing-plans step. The outline:

| Track | Milestone | Exit criterion |
|---|---|---|
| A corpus | A1 `_proxy` and the oracle tool; A2 F1 code, cases and checker; A3 F2; A4 F3 | Checker and oracle pass per fixture |
| B pipeline | B1 fork skeleton from `codedoc_python`: strip Python parts; config; CLI modes; toolchain and helper build; control directory and `-modfile` check | pytest green with fakes; `doctor` runs |
| | B2 helper `extract` + `go_extract.py` + entities | Go and pytest tests per site shape and caller rule |
| | B3 discovery (`go_units.py`, `go_modcache.py`) | Layout tests on F2 |
| | B4 helper `resolve` + classification, rules, gates | Go tests for the traps; first F1 numbers |
| | B5 schema, builders, Phases 8–9, `validate` | Builder tests; QA questions |
| | B6 wiki track (Phases 2–6, 10) | Tests with fakes: batching, fallback, cache, preconditions |
| | B7 `evaluate` + `crossref` + peers | Both modes exit 0; F3 exact |
| | B8 `trial` on etcd and Prometheus | Numbers recorded; 0 wrong executed edges |
| C surfaces | C1 agent instruction and SKILL.md; C2 MCP, webapp, deploy; C3 docs, CLAUDE.md | Docs gate passes |
| | C4 final verification | Both `evaluate` modes, pytest, Go tests, trial, docs gate, a fresh-context review |

An agent team builds tracks A and B in parallel, with a reviewer after each milestone.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Offline answers that are wrong rather than missing | Gates G2–G4 on type facts. The trial reports changed answers between modes. F2 and the Go tests cover the three mechanisms |
| Interface calls dominate some repos, so recall depends on `PossiblyCalls` | Static edge to the interface method plus candidates. VTA pruning is listed as later work. The trial reports candidate recall |
| A repo needs a newer Go than the toolchain | It fails closed with a clear message; `setup go` gives the pinned toolchain |
| Memory on large repos (`NeedDeps`: 5 GB on Prometheus) | `doctor` warns when free memory is low. An export-data mode (0.5 / 0.9 GB) is possible later, but it loses non-repo implementers and so the open-world check |
| `-modfile` does not behave as expected with go/packages | Checked in B1. Fallback: a one-`use` generated `go.work`, with the vendor limit documented |
| Runtime truth for multi-module repos | Groups follow the go command's own rule (§3.2). F1's MOD cases and F2 pin both modes |
| Peer substitution stands in for a published version | It applies only to required module paths. Forks are refused. The docs state it |
| x/tools API changes | Vendored and pinned. The helper revision enters the freshness stamp |
| The corpus is the largest item | Track A runs in parallel from day one, reusing the C#/Python domain and case IDs |

---

## 14. Decisions

### 14.1 The user's answers (2026-10-13)

| # | Question | Answer |
|---|---|---|
| 1 | What Go code, to what bar? | Plain Go modules; codedoc_python's bar |
| 2 | May CodeDoc download modules? | Detect plus opt-in download (after asking how it affects the outcome) |
| 3 | Real-repo trial | etcd and Prometheus |
| 4 | Corpus size | Parity with Python |
| 5 | Run modes | `--all`, `--graph`, `--wiki`; `--wiki` needs `--graph` first |
| 6 | Outputs | Leave test outputs in place for verification |
| 7 | Reference | The user suggested codedoc_csharp. Agreed: Python shell plus C#-style resolver |
| 8 | Approach | Go helper plus C#-style resolver |
| 9 | Summary granularity | Batched per file; one call per node when a file is large |
| 10 | Where | Save the work in `codedoc_go` |
| 11 | Goal | Design, implement and test until it works correctly and efficiently; make the process smooth |
| 12 | Delivery | Commit and push when done |

### 14.2 Choices made in this spec

| # | Choice | Reason | Alternative |
|---|---|---|---|
| D1 | go/types helper instead of gopls | Same answers on 187,431 sites; 10–20× faster; offline traps detectable | gopls per site (siblings' pattern) |
| D2 | go/parser instead of tree-sitter-go | tree-sitter-go 0.25.0 fails on Go 1.26 syntax in 32 etcd files and lags two releases | tree-sitter-go |
| D3 | Groups follow the go command's own build rule | It decides which code a call reaches (runtime truth) | Always one workspace |
| D4 | Control directory per group; nothing written into the repo | Workspace `go.work.sum` writes and `.git/index` rewrites were observed | Run in place |
| D5 | Offline from the user's cache through a `file://` proxy | Zero writes to the user cache | Point `GOMODCACHE` at it (writes empty directories for missing modules) |
| D6 | Interface calls: static edge plus `PossiblyCalls`, no single-implementer upgrade | Go interfaces are open-world; measured counterexamples | C#/Java/Python `iface-impl` |
| D7 | `Modules` renamed `Topics` | In Go "module" means a go.mod module, which is a column; the agent must not confuse them | Keep the family name |
| D8 | New `Packages` table, `PackageImports`, `ClassEmbeds`, `ClassImplements` | Imports target packages; embedding and implementation are the Go relations | File-level imports, `ClassInherits` |
| D9 | No name-heuristic `PossiblyCalls` | Unresolved Go sites lack type facts; guesses are noise | Siblings' heuristic |
| D10 | Keep the file name `docs/LSP_RESOLUTION.md`; the engine is stated in its first paragraph | The user asked for the same doc set as the other variants | Rename to `RESOLUTION.md` (rejected after that request) |
| D11 | Graph first, wiki on top; summary cache by content hash | The user's three modes; incremental wiki runs | Siblings' docs-first order |
| D12 | Committed synthetic module proxy for fixtures | `evaluate --env` without network or licence questions | Real third-party modules over the network |
| D13 | `CGO_ENABLED=0` by default | With cgo, loads run gcc and pkg-config with repo arguments | cgo on |
| D14 | Methods embedded in Phase 10 | They now have real summaries | Siblings' Files/Classes/Modules only |

---

## Appendix A — Research evidence (2026-10-13)

The reports and probe sources are in [research/2026-10-13/](../research/2026-10-13/README.md). The raw outputs are under `/home/admin/scratch/codedoc-go-research/`, left in place for verification.

| Report | Key numbers |
|---|---|
| real-repos | A vs B on 187,431 sites: 0 disagreements. B 2.3 s / 5.7 s; gopls 40–45 s / 60–65 s. Offline: 0 changed answers in both repos, but 3 wrong-answer mechanisms shown by probe. 38 of 156 (etcd) and 20 of 145 (Prometheus) in-repo interfaces with sites have one in-repo implementer; 2 and 4 of those are also satisfied by non-repo types. Lazy download 95 zips / 62 MB (etcd) vs 741 for `go mod download` |
| engine-probe | Direct static calls are always right (runtime-confirmed). Dynamic dispatch is never resolved. Offline hazard (a) gives wrong answers (3 of 3). `implementation` is structural over workspace, dependencies and tests. A relocated go.work gives identical answers on 244 of 244 sites |
| go-modules | Parsers agree with the go command on 3,682 go.mod files and 167,184 build-constraint decisions. Offline needs `GOPROXY=off` and `GOSUMDB=off` (644 direct sumdb GETs otherwise). A `file://` proxy from a read-only user cache works with 0 errors |
| code-seams | 62 % of codedoc_python's `graph_generator` statements are Python-specific. Methods outside their type's file: 105 / 302. Basename topic linking drops 42.8 % / 23.3 % of files. go/parser: 0 errors on 1,840 files, 10–11× faster than tree-sitter end to end |
| runtime-oracle | Source instrumentation of a copy (stdlib-only `goracle`). Per-site ids through `/*line*/` directives; completion counters catch entries into uninstrumented code. All 6 etcd and 5 Prometheus test packages pass instrumented; 2,884 / 8,844 sites observed. A naive name-matching resolver scored 78 WRONG on Prometheus, all of them real shadowing errors |

---

## Appendix B — Review amendments (2026-10-13)

A fresh-context reviewer checked this spec with probes (`/home/admin/scratch/codedoc-go-research/spec-review/REVIEW.md`, probes `p1`–`p11`). **These amendments override the main text where they differ.** R1–R3 are already folded into §5.

| # | Finding | Amendment |
|---|---|---|
| R1 | G3 missed the cross-package embedding case (probe p2: a wrong in-repo edge offline with `errs=0` in the calling package) | G3 applies to every promoted selection used as a target (calls, func-alias method values, range over method values, method expressions) in any package, evaluated per package variant, most conservative wins (§5.6) |
| R2 | Targets with no node (local interface types, anonymous literals, inline constraints) could become self-edges | Exact mapping by the name range of a node of the expected kind; non-package-level interfaces have `recv_type == ""` and are treated like anonymous interfaces (`ambiguous` with candidates, else `dynamic`) (§5.3–5.4) |
| R3 | `//line` could redirect resolve positions (probe p3) | All resolve positions are unadjusted (§5.1) |
| R4 | The group rule could pick a non-ancestor go.work (probe p10) or put a module in two groups | A module's group is the go.work found by **walking up from the module root**, if that go.work lists the module; otherwise a single-module group (`GOWORK=off`), with a warning when another go.work lists it. Each module belongs to exactly one group |
| R5 | A failed load (a dependency with a newer `go` line, helper crash, timeout, OOM) had no outcome (probe p7) | The group gets status `error`; all its sites are `unresolved` with reason `toolchain` when the go command says "requires go >=", else `load-error`; the message goes to `doctor` and the run summary; other groups continue. `doctor` pre-checks dependency `go` lines from cached `.mod` files and `## explicit; go 1.N` lines in `modules.txt` |
| R6 | Oracle holes | `match` and `judge` add WRONG-OTHER for "no instrumented callee, `exec > 0`, target hooked"; when a site executed, the expected in-repo target must be observed; `external` expectations require `exec > hits` and no in-repo callee; IFACE-CONSISTENT takes the implementer set from the resolver record's `candidates` (the helper's implements facts); uninstrumented entries are allowed at interface edges (open world) |
| R7 | The trial's offline arm was not reproducible | Offline arm: an empty dedicated `GOMODCACHE` with `GOPROXY=off`; download arm: a separate cache. The trial **fails** on any offline `resolved` target that differs from download mode; answers resolved only offline are investigated. The oracle scores the download-mode records |
| R11 | Billable volume (~25k Gemini calls for both variants on etcd and Prometheus) | Phase 2 node summaries are cached by content and context hash and **shared** between codedoc_go and codedoc_go_leiden (`WIKI_SUMMARY_CACHE`, default per repo; the comparison points both variants at one cache). Generated files get deterministic summaries by default (`WIKI_SUMMARIZE_GENERATED=0`: doc comment plus "生成コード"), since they are about 46 % of etcd's per-node calls. `--wiki` prints the estimated call count before starting. The real-repo comparison runs etcd first and decides on Prometheus from the measured cost |
| R12 | go.sum in single-module groups: readonly mode never writes or asks sumdb | Offline: missing go.sum lines → `unresolved` (`missing-module`) for the affected packages. Download mode for a non-vendored single-module group uses a one-`use` generated go.work (control dir) for both download and load, so sumdb can fill `go.work.sum` there |
| R13 | `-modfile` in `GOFLAGS` breaks go/packages | `-modfile` goes only through `packages.Config.BuildFlags` and the download command line; `-mod` stays explicit |
| R14 | `go env GOMODCACHE` can trigger a toolchain download | Run it from a neutral cwd with `GOTOOLCHAIN=local`, keeping only the user's `GOMODCACHE`, `GOPATH`, `HOME` |
| R15 | Stops vs warnings unclear | **Stop** (exit 3): no usable toolchain; every group too new; Spanner schema missing without `--no-spanner`; Gemini configuration missing for `--wiki`/`--all`. **Warn**: missing modules, degraded groups, newer `toolchain` lines, low memory, inconsistent vendor in one group |
| R16 | `range` sites need type facts; goracle cannot attribute range calls | Extract emits candidates, resolve answers (`site` or informational `notcall`); goracle attributes an implicit range call to the single `range` site on the `for` line, else lists `range` edges as unverifiable |
| R17 | Blank methods collide | `T._#k`, numbered per file |
| R18 | Owner of methods whose receiver type has build twins | Same-file twin, else the twin compiled by the primary build, else the smallest relpath; receiver not found → the file's `<package>` with a `receiver_unresolved` flag |
| R19 | Dispatch interface unspecified | For calls through a struct's embedded interface field, the dispatch interface is the field's interface type; interface types are never candidates |
| R20 | No source for `--wiki` graph context after a Spanner `--graph` | `graph_rows/*.jsonl` is **always** written (also with Spanner); `--wiki` reads it |
| R21 | go.mod `ignore` directories | Not scanned (like `testdata`); targets there `unscanned` |
| R22 | Relative `file://` proxy URL is invalid (probe p8) | Absolute `file:///…/test_codes/_proxy` |
| R23 | Numbers | §0: "agreed wherever both named a target (187,431 sites; neither answered at 3,848, only gopls at 422)"; §3.7: lazy download is 1.6–12× smaller than `go mod download`; §3.4: with `CGO_ENABLED=0` etcd excludes 33 files; F2 includes a newer-`toolchain`-line case |
| R28 | Peer substitution can degrade a group offline | `doctor` checks substituted peers' requirement closure and `go` lines; the run reports per group when peers degrade external resolution |
| R29 | Entity reuse stamp misses module ownership | The stamp adds the owning go.mod path and module path |
| R30 | Summary cache ignores graph context | The Phase 2 cache key is (file SHA-256, context hash, prompt version, model) |
| R31 | Pins and QA files | `DECL_PINS` in `cases.py`, `ground_truth/qa_questions.json`; `check_ground_truth.py` validates both |
| R32 | goracle for modules outside a go.work | For single-module groups the runtime module is added with `require` + `replace` in the **copy's** go.mod, `GOWORK=off` |
| R34 | Download mode re-fetches cached modules | `GOPROXY=file://<user modcache>/cache/download,https://proxy.golang.org` (checksums still verified) |
| R35 | Inconsistent `vendor/` | Group status `error` with the go command's message (never a silent readonly build) |
| R36 | linkname / func-alias details | linkname targets parse `pkg.F` and `pkg.(*T).m` / `pkg.T.m`; func-alias and method expressions decide by the **selected method object**: an interface method behaves like an `iface` site |
| R37 | Peer target qualnames | The helper emits `qualname` (`F`, `T.M`, `I.M`) for every target from the types objects; Python uses it for peer and external targets |
| R10 | Stale topic rows (both variants) | Every topic write deletes the repo's rows in the derived topic tables (`Topics`, `FileBelongsToTopic`, and in codedoc_go_leiden also `ClassBelongsToTopic`, `TopicDependsOn`, `TopicContainsTopic`) for the current `ID_PREFIX`, then inserts |
| R41 | Download mode had no way to turn off checksum-database lookups (needed by `evaluate --env` offline and by private proxies) | New config key `GO_SUMDB` (default `sum.golang.org`, download mode only); `evaluate --env` sets `off`. Offline mode always uses `GOSUMDB=off` |
| R42 | Record shapes for imports, embeds and implements were not defined | Master plan C5 "Other per-file record lists" defines them; `entities.json` wrapper adds `helper_revision` |
| R43 | A syntax error in the receiver type's package can swallow a method, so a deeper promoted method wrongly wins (B2 review, probe `brk/`) | Trap 3 (G3) also refuses a promoted selection when any package declaring a type on the embedding path — the receiver's own package included — has syntax errors (the parser drops declarations after an error, so a method can vanish); same for iface candidates and `implements` pairs through such types. Type errors alone (e.g. failed imports offline) do not trigger it: go/types keeps a method whose signature has invalid types, and an embedded field of invalid type is already trap 3 (refined 2026-10-14 to keep offline recall) |
| R44 | A syntax error can swallow a later reassignment, invalidating the single-binding proof | `convention:func-alias` is refused when the site's file has any syntax error (`unresolved`, `syntax-error`) |
| R45 | NEG-004: the download fill (`-mod=mod`) added a missing requirement, so an import the developer's build rejects resolved | After the fill, the control go.mod's requires and replaces are restored to the repo's (go.sum fills kept); undeclared imports fail as in the developer's build; a warning names the missing requirements |
| R46 | crossref could build an edge from records of a file that changed during the load | `crossref` refuses records of files whose resolutions hash differs from the entities hash and any edge ending in such a peer file; new drop reason `stale` |
| R47 | A gate for package imports was missing (PackageImports were 0 in every run without failing evaluate) | `evaluate` gates `imports_unmapped == 0` on F1 and F3 (ambiguous-clause records reported separately) and reports PackageImports/ClassEmbeds/ClassImplements row counts; the trial reports them per arm |
| R48 | Re-ingest kept stale rows (a removed call, type or file stayed in Spanner, so the served graph could hold edges the code no longer has), and every `--graph` reset the summaries Phase 10 wrote to doc comments | Phase 8 records, before it clears `graph_rows/`, the primary keys of the previous rows of every node and edge table in `graph_prune.json` (merged with a file left by an interrupted run), when `graph_meta.json` names this repo and this run's Spanner target and the repo has rows there. After the Phase 9 upserts, the keys this run did not write are deleted (edges, then nodes), with the `FileBelongsToTopic` rows of deleted files, and the file is removed. Right after the Phase 8 upserts, the summaries Phase 10 would write are re-applied (`update`, no LLM call) for files whose cached summary record has the file's SHA-256 and for packages and directories whose children hash is unchanged; the rows and `graph_rows/` keep the doc comments, because the prompts read them as doc comments. Phase 8 never writes `embedding`, so the embeddings stay with their summaries. `crossref` prunes its own previous rows the same way. `_check_spanner` stops on schema drift (missing columns, wrong types), naming `--migrate` |
| R49 | Real F1 run (2026-10-14): the Phase 4 merge answer was cut at `MAX_OUTPUT_TOKENS` (thinking used most of it) and the JSON fallback returned its first object, so F1 got 1 topic linking 4 of 75 files; Phase 2 answers keyed nodes by package-qualified names, so every node was asked again (174 calls for 74 files); and both Phase 4 answers grow with the number of files, which cannot fit at etcd scale | A cut answer (`MAX_TOKENS`) is retried once with a larger budget, else it fails into a lossless deterministic fallback (one topic per chunk, at most 15; leftovers join a same-named topic or 「その他」); JSON calls and package/directory summaries use thinking level LOW, topic pages the default; the JSON parser never returns an inner object of an unclosed array; JSON calls get a bounded thinking budget; a topic chunk above a file cap is split by subdirectory; the merge answers with chunk-topic numbers and Python unions their files; Phase 2 accepts package-qualified keys; `PROMPT_VERSION` is bumped. The approach (fan-out per directory, then one merge) stays, since it is what codedoc_go_leiden compares against |
| R50 | B5/B6 review (2026-10-14): scoring and trial gates could pass wrong or partial results (distractors unchecked on the external and alternate paths; an offline in-repo answer against a definite other download answer counted as "resolved only offline"; empty or degraded arms, partial loads and empty denominators passed; only case targets were checked; interface upgrades scored as alternates) | `evaluate`: distractors are checked first on every path; external expectations check candidates and `open_world`; a resolved record to a runtime target is wrong when the expected target is an interface method (no alternates on interface cases, §5.4); `PossiblyCalls` rows count as edges, so a candidate outside the expected set, or candidates where none are expected, are wrong; an external function record at a conversion site is wrong; every group must end the load `ok` on F1 and F3 (F2: its discovery status unless `status_after_load` says otherwise); `join_misses` must be 0; rate denominators come from entity sites minus conversions, notcalls and dropped range candidates, and an empty denominator fails; goracle judges every resolved F1 and F3 record (0 WRONG-* required), every `via=types` record whose site has a name must end in that name, and every record's caller must match goracle's site table; F1 pins its expected `ClassImplements` and `ClassEmbeds` sets. `trial`: a site whose download record is a definite other answer (another target, external, builtin, dynamic, ambiguous) is a changed answer; a group not `ok` after the load, 0 edges, or a verdict count different from the record count fails; the implicit no-packages skip passes only with `--skip-oracle`; goracle gets the candidates and answers IFACE-OTHER for an observed in-repo callee outside them, except a method of a generic type (never a candidate, §5.4; allowed like open world and reported) and any same-named method behind a `via_embedded_iface` candidate (second dispatch); the candidates key is sent (empty lists included) only for a closed-world edge (`open_world` false) whose record carries no `candidates_partial` (`suppressed` when G4 refuses the whole search, which includes an in-repo type whose method facts are unknown, or `via_embedded_external` when an out-of-repo `via_embedded_iface` candidate was dropped; a definite non-implementer is simply not a candidate); every other interface edge gets the name check only, because on an open-world interface an out-of-repo implementer can forward to any in-repo method (e.g. `io.NopCloser(r)`), so a callee outside the candidates does not prove the edge wrong (goracle review, 2026-10-14); offline ⊆ download is checked for candidates, imports, embeds and implements; unscored verdicts (NO-TARGET, NO-SITE) are reported and fail outside goracle's documented skip classes; the untouched check follows a `.git` file and compares modes and ctimes; goracle builds with `GO_BUILD_TAGS` |
| R51 | User request (2026-10-14): "a neat visual explorer for the knowledge graph … in webapp … for both" | The webapp gains a graph explorer page. The backend reads the repo's `graph_rows/*.jsonl` (written by every `--graph`, also with `--no-spanner`; no Spanner or Gemini call), generically over the tables present, with read-only, capped JSON endpoints: stats, search, node details with summary, neighbourhood expansion per edge table and direction, and overview maps (package imports, directory tree, topics). The frontend draws them with an interactive graph view: colour and shape by node kind, edge style by table, toggles per edge table, hide tests and generated code, expand on click, a details panel linking to the wiki pages, a legend, fit and PNG export. codedoc_go_leiden gets it by the sync; its topic map uses topic levels, `TopicContainsTopic` and weighted `TopicDependsOn` |
| R52 | Prometheus trial (2026-10-14): offline, `maxTimestamp` (embeds `prometheus.Gauge` from a module missing offline, has `func (m *maxTimestamp) Get() float64`) was recorded as implementing `ReadyScrapeManager` (`Get() (*scrape.Manager, error)`), a wrong `ClassImplements` edge; go/types does not report missing methods for a type with an invalid embedded field | The helper checks every needed method itself on the method set of T or *T instead of trusting `types.Implements`: the winning method must exist, be real, have no invalid types, not be hidden by a shallower unknown or lost member (G3/R43), and have the interface method's exact signature. A winning method with another signature means "not an implementer" (no pair, no candidate), since a type's own methods win over anything an invalid embedded field could add; any other doubt makes the pair `incomplete` (counted, never edged) or suppresses the candidate search (G4). F1 pins the shape (IFC-018, class edges) |
| R53 | Prometheus trial: conversions whose callee is a type literal (`[]byte(s)`, `(*[N]byte)(p)`) in build-excluded files stayed as `unresolved` sites, because the helper only classifies conversions in loaded files | Extraction drops a call whose callee, after parentheses, is a type literal (array, slice, map, chan, func, interface or struct type, or a pointer to one): it is a conversion by syntax, counted with the conversions, in every file |
| R54 | goracle re-check (2026-10-14): the candidate index holds only package-level, non-generic named types, so an in-repo implementer that is a function-local type, an anonymous struct type (embedding gives it methods) or an instance of a generic type was neither a candidate nor opened the set; a record could say `open_world` false with an incomplete set (probe: `struct{committer; rollbacker}` passed as `Store`, the promoted `committer.Commit` ran, scored IFACE-OTHER) | `open_world` is true when the candidates do not cover every type that satisfies the interface: a type outside the repo, or an in-repo type that cannot be a candidate (a function-local named type, an anonymous struct type, an instance of a generic type), each checked method by method (R52; an unknown answer also opens the set); and at a site in a module whose in-repo directory another group puts in its build by a local `replace` (go.mod or go.work), because that group's types are in no load of the site's group and can be passed in (F1's admin-console layout; probe p17), so the check runs in Python after all groups load, from discovery's replace facts. The candidates stay the package-level, non-generic named types, and `candidates_partial` keeps its two values. Consumers need no change: an open-world edge already gets the name check only (R50), and its `PossiblyCalls` rows carry `open_world` true |
