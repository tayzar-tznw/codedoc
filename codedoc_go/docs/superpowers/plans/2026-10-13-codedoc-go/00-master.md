# codedoc_go + codedoc_go_leiden Implementation Plan (master)

> **For agentic workers:** this plan is executed by an agent team. Each work package (WP) has one owner agent that follows TDD (`superpowers:test-driven-development`): failing test → minimal code → passing test, in small steps. Read this master plan **and** both specs before starting. Steps inside a WP are yours to sequence; the contracts (C1–C12) are binding — never change a contract on your own; send the lead a message instead.

**Goal:** Build `codedoc_go/` (CodeDoc for Go modules: go/types helper resolver, three run modes, Gemini wiki, Spanner graph, evaluate, trial) and `codedoc_go_leiden/` (the same with Leiden topics plus `compare-topics`), each with the family's docs set.

**Architecture:** Fork the Python shell of `codedoc_python`; replace extraction and resolution with a Go helper (`gohelper extract` via go/parser, `gohelper resolve` via go/packages + go/types); discovery in pure Python; graph-first pipeline with a wiki layer on top; evaluate on three Go fixtures with a runtime oracle (`goracle`). The Leiden sibling replaces LLM topic discovery with graspologic-native Leiden on the resolved graph.

**Tech Stack:** Python 3.13 (uv venv), Go 1.27.2 toolchain, golang.org/x/tools v0.52.0 (vendored), google-genai, google-cloud-spanner, google-adk, pytest + pytest-cov; graspologic-native + networkx (Leiden sibling only).

**Specs (read both):**
- `codedoc_go/docs/superpowers/specs/2026-10-13-codedoc-go-design.md` (parent)
- `codedoc_go_leiden/docs/superpowers/specs/2026-10-13-codedoc-go-leiden-design.md` (delta)
- Research evidence: `codedoc_go/docs/superpowers/research/2026-10-13/` (README + 5 reports; probe sources under `probes/`)

## Global Constraints

- Monorepo root `/home/admin/dev/code`; variants are standalone siblings; **never modify** `codedoc/`, `codedoc_csharp/`, `codedoc_java/`, `codedoc_php/`, `codedoc_python/`, `graphify/` or the root `docs/`.
- Toolchain for all work: `/home/admin/scratch/codedoc-go-research/toolchain/go/bin/go` (go1.27.2). Always run go with `GOENV=off GOTOOLCHAIN=local GOFLAGS=-modcacherw` (add `-buildvcs=false` to any go command that could touch a repo) and caches under your WP scratch dir `/home/admin/scratch/codedoc-go-build/<wp>/` (GOCACHE, GOMODCACHE when not using vendor, TMPDIR, GOTMPDIR). Never write inode-heavy scratch to `/tmp` (tmpfs, ~1M inode cap).
- Python: `codedoc_go/.venv` (uv, Python 3.13). Install with `uv pip install --python .venv/bin/python -r requirements.txt`. Never pip-install into other variants' venvs.
- Tests: `NO_COLOR=1` (the environment exports `FORCE_COLOR=3`); pytest with `-p no:cacheprovider` and `PYTHONDONTWRITEBYTECODE=1` when running in scratch copies.
- The Bash tool ignores `set -e`: chain gating commands with `&&`.
- Positions: lines 1-based; columns **1-based byte columns** (go/token, `PositionFor(p, false)`); byte `offset` 0-based. No UTF-16 anywhere.
- Default build configuration: `GOOS=linux GOARCH=amd64 CGO_ENABLED=0`, no tags.
- Defaults: `SPANNER_DATABASE=codedoc-go` (leiden: `codedoc-go-leiden`), instance `codedoc-instance` in code defaults (the user's `.env` may say otherwise), deploy service/image `codedoc-go-mcp` (leiden: `codedoc-go-leiden-mcp`), SA `codedoc-mcp-sa`, Artifact Registry repo `codedoc`.
- `GOOGLE_CLOUD_PROJECT` has no hardcoded default anywhere.
- Docs: Japanese five-file set (`README.md`, `docs/ARCHITECTURE.md`, `docs/LSP_RESOLUTION.md`, `docs/GRAPH_SCHEMA.md`, `docs/CROSS_PROJECT.md`); `CLAUDE.md` and `mcp_server/README.md` English; MECE, short sentences, tables, no marketing words (堅牢, シームレス, 包括的, 強力, 網羅的), no emoji, numbers only with a date.
- No test calls GCP or the network unless marked `network`; no test sleeps through a real cooldown (inject sleeps).
- Commit nothing; the lead commits at the end.
- Zero wrong edges is the hard guarantee: when in doubt, refuse (status `unresolved`/`dynamic`/no edge).

## Review Focus

1. A repo whose `go` line exceeds the toolchain → fails closed per group with a message naming the line and `setup go` / `GO_TOOLCHAIN` (WP-D test `test_toolchain_too_new_fails_closed`; WP-B2 test `test_group_toolchain_gate_marks_sites_unresolved`).
2. Running against a repo must leave it byte-identical, including `.git/index` and no `go.work.sum` (WP-D test `test_control_dir_never_writes_repo` runs the real go command on a temp git repo with a go.work; WP-B6 asserts it on the trial repos).
3. `--wiki` after a code edit without `--graph` → refuses with the changed-file list (WP-B1 test `test_wiki_refuses_on_changed_hashes`).
4. Offline with a partially populated module cache → in-repo answers unchanged, missing-module sites `unresolved`, never a different target (WP-H2 tests for the three traps; WP-B6 reports changed answers between modes = 0).
5. Huge file in `--wiki` (> `WIKI_FILE_MAX_CHARS`) and a batched response that omits nodes → per-node fallback and per-node retry (WP-B4 tests `test_large_file_goes_per_node`, `test_missing_nodes_retried_individually`).

---

## C1 — Layout and ownership

```
codedoc_go/
  CLAUDE.md README.md Dockerfile .env.example .dockerignore .gcloudignore .mcp.json pytest.ini requirements.txt sample_prompt.txt  [B1 creates; C2 finalizes docs]
  .claude/skills/codedoc-query/SKILL.md                                [C1]
  docs/{ARCHITECTURE,LSP_RESOLUTION,GRAPH_SCHEMA,CROSS_PROJECT}.md     [C2]
  docs/superpowers/{research,specs,plans}/                             [lead]
  gohelper/                    go.mod, vendor/, main.go                [H2 owns go.mod, vendor, main.go]
    internal/extract/          *.go, *_test.go, testdata/              [H1]
    internal/resolve/          *.go, *_test.go, testdata/              [H2]
  graph_generator/
    __init__.py __main__.py config.py prompts.py pipeline.py           [B1; later edits: B2/B3/B4 in their sections only, coordinated by lead]
    go_model.py                dataclasses (C4)                        [D]
    go_units.py                parsers, units, groups, peers           [D]
    go_toolchain.py            toolchain, helper build+cache, env, setup go  [D]
    go_modcache.py             offline sources, download mode          [D]
    go_extract.py              run extract, entities store             [B2]
    resolution.py go_classify.py go_gates.py                           [B2]
    setup_spanner_graph.py     NODE_SPEC/EDGE_SPEC (C6)                [B3]
    node_summaries.py          Phase 2                                 [B4]
    crossref.py evaluate.py                                            [B5]
    trial.py                                                           [B6]
  graph_query_agent/ mcp_server/ webapp/                               [C1]
  scripts/ doccheck.py gql_lint.py verification_table.py               [C1]
  tests/                       one test file per module; conftest.py fakes   [owner of the module]
  test_codes/
    _proxy/                    synthetic GOPROXY (file://)             [A1]
    _gt/                       gt_lib.py generate_manifest.py check_ground_truth.py run_oracle.py, oracle/ (goracle Go module)  [A1]
    iryo-kanri-system/         F1                                      [A2]
    go_layouts/                F2                                      [A3]
    multi_repo/                F3                                      [A3]
codedoc_go_leiden/             fork of codedoc_go after wave 2         [L2]
  graph_generator/topics/      graph_builder.py community.py metrics.py naming.py reports.py compare.py  [L1]
```

## C2 — gohelper protocol (version 1)

Binary `gohelper` (module path `codedoc.local/gohelper`). All output is JSON Lines on stdout (or `--out FILE`); diagnostics on stderr; exit 0 unless the invocation itself is invalid (load errors are records, not exit codes).

`gohelper version` → `{"kind":"version","protocol":1,"go":"go1.27.2","x_tools":"v0.52.0","revision":"<sha256 of embedded source list>"}`

### `gohelper extract`
Args: `--root ABS` (repo root), `--files-from FILE` (repo-relative POSIX paths, one per line), `--goos linux --goarch amd64 --tags a,b --cgo 0`.
Groups files by directory+package clause to resolve receivers across files. Emits one line per file:
```json
{"kind":"file","relpath":"libs/billing/fee.go","record":{ <C3 FileRecord> }}
```
then `{"kind":"summary","files":N,"errors":E,"seconds":S}`. A file that cannot be read/parsed at all emits `{"kind":"file","relpath":...,"error":"..."}`.

### `gohelper resolve`
Args: `--repo-root ABS`, `--dir ABS` (cwd for the load: workspace dir or module dir), `--pattern work|./...`, `--gowork ABS|off`, `--modfile ABS|""`, `--goos --goarch --tags --cgo`, `--build-id linux-amd64`, `--main-modules m1,m2` (module paths whose packages count as in-repo), `--peer-dirs ABS,ABS` (peer module dirs, for classification hints only). The process environment is provided by Python (C8 allowlist); the helper also passes `-buildvcs=false` (and `-modfile` when given) in `packages.Config.BuildFlags`.
Records (one per line; `path` fields are **real paths**):
```json
{"kind":"file","path":"/abs/x.go","sha256":"…","packages":["example.com/a [example.com/a.test]"]}
{"kind":"site","path":"/abs/x.go","offset":1234,"line":41,"col":15,"end_col":24,"name":"Calculate",
 "site_kind":"call|go|defer|range","shape":"ident|selector|index|funclit|paren|call|other",
 "class":"builtin|conversion|func|method|iface|typeparam|value|funclit|linkname|unknown",
 "target":{"path":"/abs/fee.go","line":12,"col":22,"name":"Calculate","pkg":"example.com/iryo/billing",
           "module":"example.com/iryo/billing","version":"","obj":"func|method|iface_method|var|type|builtin|closure",
           "recv":"*FeeCalculator"} ,
 "promoted_depth":0,"embed_invalid":false,"stub":false,"pkg_errors":false,
 "value_kind":null,"alias":null,"linkname":null,"asm":false,
 "iface":null,"variants":[],"build":"linux-amd64"}
{"kind":"embed","type":{"path":…,"line":…,"col":…,"name":"Server","pkg":…},"embedded":{<target>},"pointer":true}
{"kind":"implements","type":{…},"iface":{…},"pointer_receiver":true}
{"kind":"import","path":"/abs/x.go","offset":88,"line":5,"col":2,"import_path":"example.com/iryo/kernel",
 "target_dir":"/abs/libs/kernel","module":"example.com/iryo/kernel","version":"","std":false,"error":null}
{"kind":"error","pkg":"…","path":"/abs/x.go","line":3,"msg":"could not import …"}
{"kind":"summary","packages":183,"errors":0,"load_seconds":1.4,"sites":72623,"conversions":5655,"peak_rss_bytes":…}
```
Field rules:
- `offset` is the byte offset of the callee token (selector: the `Sel` ident; ident: the ident; index shape: the base ident; funclit: the `func` keyword; range: the range expression's last ident). Same rule as C3 `sites[].offset`, so `(relpath, offset)` joins extract and resolve.
- `target` is null for `builtin`, `value` without proof, `unknown`. For `func`/`method`/`iface`/`typeparam` it is the declaration's **name** position (generic origin via `Origin()`).
- `value_kind`: `local|param|field|pkgvar|index|result` for `value` sites.
- `alias`: the proven single-binding target (`func-alias` rule, spec §5.5) as a `target` object, else null.
- `iface` (for `iface`/`typeparam` and interface method values): `{"recv_type":"pkg.I","candidates":[{"path","line","col","name","pkg","type":"T","pointer":bool,"test_only":bool,"via_embedded_iface":bool}],"open_world":bool,"suppressed":bool}` — `suppressed=true` when gate G4 applies (no candidates emitted).
- **Candidate universe (ruling 2026-10-13):** candidate types are the named types *present in this load* whose declaring file is under `--repo-root` (real path; not `vendor/`, not `testdata/`) and whose package is real (`Module != nil` with its files under `Module.Dir` or `Module.Replace.Dir`) — regardless of `Module.Main`, so replaced in-repo modules of a single-module group count. Types not in the load cannot be the dynamic type of a value in that program. The `Module.Main` filter of trap 1 applies to **site emission** only.
- **Anonymous interfaces:** `iface.recv_type` is `""` when the receiver's interface is a type literal (e.g. `v.(interface{ Describe() string }).Describe()`); `target` still points at the method spec.
- **Universe `error`:** a call of `Error` on the predeclared `error` interface is class `builtin` (target in `$GOROOT/src/builtin`), with no candidates.
- **Peer relations (ruling 2026-10-13):** `implements` records cover (in-repo concrete type, interface) pairs where the interface is in-repo **or under a `--peer-dirs` module dir**; `embed` records are emitted for every embedded field of an in-repo type whatever its location (Python classifies the embedded type; peer → `CrossRepoRef embeds`).
- `variants`: when package variants of the file disagree, the list of all distinct targets (then `target` is the production variant's).
- Conversions are emitted as `{"kind":"conversion","path":…,"offset":…}` records, never as sites, and counted in `summary.conversions`. Python joins entity sites on `(relpath, offset)`: a matching `conversion` record drops the site (counted); a `site` record classifies it; no record at all means the file was not covered by any load (→ `excluded-by-build`, `no-module` or `load-error`).
- Sites in a file attached to a non-main-module package are dropped (trap 1); targets in stub packages set `stub=true` and `target=null` (trap 2); promoted selections across an invalid-typed embedded field at shallower-or-equal depth set `embed_invalid=true` and `target=null` (trap 3).

## C3 — `entities.json` FileRecord (version 1)

```json
{"sha256":"…","module":"example.com/iryo/billing","package":"example.com/iryo/billing","package_name":"billing",
 "origin":"package|command|test|xtest","generated":false,
 "build_constraint":"linux && !cgo","build_included":true,"syntax_errors":[],
 "imports":[{"path":"fmt","alias":"","kind":"normal|dot|blank","line":3,"col":2,"offset":21}],
 "types":[{"name":"FeeCalculator","qualname":"FeeCalculator","kind":"struct|interface|named|alias",
           "type_params":"","embeds":[{"text":"Base","line":9,"col":2,"pointer":false}],
           "methods":[{"name":"Total","qualname":"Totaler.Total","name_range":[14,2,7]}],
           "name_range":[8,6,19],"start_line":8,"end_line":12,"doc":"…"}],
 "funcs":[{"name":"Calculate","qualname":"FeeCalculator.Calculate","member_kind":"func|method|init|closure|var_init",
           "receiver":{"text":"*FeeCalculator","base":"FeeCalculator","pointer":true,"owner_relpath":"libs/billing/fee.go"},
           "type_params":"","signature":"func (c *FeeCalculator) Calculate(p Points) Yen","has_body":true,
           "linkname":null,"name_range":[12,22,31],"start_line":12,"end_line":30,"doc":"…","parent":null,
           "sites":[{"kind":"call|go|defer|range","name":"Round","line":15,"col":9,"end_col":14,"offset":340,
                     "shape":"selector","receiver":"math","caller":"FeeCalculator.Calculate"}]}]}
```
- `name_range = [line, col, end_col]`, 1-based byte columns, end exclusive.
- Pseudo-entries: every file has a type `{"name":"<package>","qualname":"<package>","kind":"package",…,"name_range":[pkg clause line, col, end]}`; `<init>` appears in `funcs` (member_kind `var_init`, qualname `<init>`) only when the file has sites in package-level var initializers; `init` functions are `init#1`, `init#2`… (per file, source order); blank funcs `_#1`…
- Closures: `member_kind=closure`, qualname `<parent>.funcN` (N = 1-based ordinal among func literals directly nested in that parent, pre-order), `parent` = parent qualname, `name_range` = the `func` keyword span `[line, col, col+4]`.
- `receiver.owner_relpath`: relpath of the file declaring the receiver's base type in the same package (prefer a same-file twin, else the twin with `build_included`, else the smallest relpath); null if not found.
- Declarations at/after the first syntax error line are omitted; `syntax_errors` lists error lines.
- **Spec review amendments R1–R37 (spec Appendix B) are binding.** Contract-level ones: every `target` object also carries `"qualname"` (`F`, `T.M`, `I.M`, from the types objects; R37); `-modfile` only via `BuildFlags` and the download command line, never `GOFLAGS` (R13); dispatch interface for embedded interface fields = the field's interface type, interface types are never candidates (R19); func-alias/method expressions decide by the selected method object (R36); linkname targets parse `pkg.F`, `pkg.(*T).m`, `pkg.T.m` (R36).
- **Rulings 2026-10-13 (spec review):** all positions in resolve output are unadjusted (`PositionFor(p, false)`); trap 3 walks the receiver's embedding tree for every promoted selection used as a target (calls, func-alias method values, range over promoted method values, method expressions) in any package and per variant (most conservative wins); `iface.recv_type == ""` also for function-local named interfaces and inline constraint interfaces; Python maps targets **only** by exact name range of a node of the expected kind (no enclosing-member fallback).
- **Rulings 2026-10-13 (helper-resolve):**
  - **`notcall` records.** `{"kind":"notcall","path","offset","site_kind":"range"}` (informational) for range candidates whose X is not of func type; `summary.notcalls` counts them. B2's rule is unchanged: a `range` entity site is kept only when a `site` record exists.
  - **`value_kind`** adds `other` (`x.(func())()`, `(*fp)()`, `(<-ch)()`); `result` = a named result variable.
  - **Universe `error`.** `Error` on the predeclared `error` interface stays class **`builtin`** with no candidates (earlier ruling; not `iface`).
  - **`summary.seconds`** (total wall time) is added.
  - **Changed files.** A file whose bytes differ between two reads within one load gets `sha256:"changed-during-load"` (Python → `stale`).
  - **Fatal load failure** (for example a bad go.work): one `error` record with `pkg:""` plus a summary with `packages=0`, exit 0. Only invalid flags exit 2.
  - **iface candidate fields:** `pkg` = package of the method declaration; `type` = the implementing type's name.
  - **`variants`** is non-empty iff the variants disagree, even with one entry. `target` = the production variant's answer: the variant loading the real path, then the non-test one, then the smallest ID.
  - **Trap 1** accepts a (file, package) pair only if `Module.Main` holds, the module is in `--main-modules` (when given), the real path is under `--repo-root`, and `dir(file) == pkg.Dir`.
  - **Trap 3** applies whether or not the package has import errors.
  - **Helper version floor.** `gohelper/go.mod` says `go 1.26.0`, because x/tools v0.52.0 requires it. The helper's minimum toolchain is go1.26.0.
- **Rulings 2026-10-13 (helper-extract):**
  - **One enumeration.** `extract.Sites(f *ast.File) []Site` is the single site enumerator; `resolve` calls it on the go/packages ASTs, so `(relpath, offset)` joins by construction.
  - **`range` candidates.** Extract emits a `range` site for every `RangeStmt` whose X (unparenthesised) is an ident, selector, index or funclit (token by the call rule). Resolve emits a site record only when X has func type (a funclit X → class `funclit`, site_kind `range`). B2 **drops** any `range` entity site without a site record, also in uncovered files — the "no record = file not covered" rule does not apply to `range`.
  - **Not sites:** the outer call of `f()(x)`; calls inside array lengths and `const` declarations.
  - **Paren/other shapes** (`(*T)(p)`, `[]byte(s)`, `x.(F)()`, `f()[i](x)`): the token is the call's `Lparen`, `name` `""`.
  - **`<init>`** also exists when package-level var initializers contain closures but no sites, so `<init>.func1` has a parent.
  - **Interface methods** in `types[].methods` also carry `signature`, `doc`, `start_line`, `end_line`.
  - **Invalid code only:** a qualname repeated within one file becomes `F#2`, `F#3`…, keeping (relpath, qualname) unique.
  - **Receiver owners** see only files of the same invocation: B2 passes every scanned file in one `extract` call.
  - **Imports:** `line`/`col`/`offset` are those of the import path literal (`ImportSpec.Path`), in both extract and resolve.
- `EXTRACTOR_REVISION` (an int in `go_extract.py`) and the helper revision are folded into each record's reuse stamp with the build configuration (spec §2.6).

## C4 — Python discovery model (`go_model.py`) and `units.json`

```python
@dataclass(frozen=True)
class BuildConfig: id: str; goos: str; goarch: str; tags: tuple[str, ...]; cgo: bool; primary: bool
@dataclass(frozen=True)
class Replace: old: str; old_version: str; new: str; new_version: str; local: bool   # new = abs path when local
@dataclass(frozen=True)
class GoUnit:
    key: str            # module path
    root: str           # repo-relative POSIX dir ("" = repo root)
    go: str | None; toolchain: str | None
    requires: tuple[tuple[str, str, bool], ...]   # (path, version, indirect)
    replaces: tuple[Replace, ...]
    vendor: bool        # consistent vendor/modules.txt
    in_repo_deps: tuple[str, ...]
@dataclass
class Group:
    id: str; kind: str                  # "workspace" | "module"
    dir: str                            # abs load dir
    modules: list[str]                  # unit keys
    gowork: str | None                  # abs generated go.work, or None (GOWORK=off)
    modfile: str | None                 # abs -modfile copy, or None
    vendor: bool; degraded: bool; status: str   # "ok" | "toolchain" | "error"
    pattern: str                        # "work" | "./..."
    missing_modules: list[str]; warnings: list[str]
@dataclass(frozen=True)
class Peer: name: str; root: str; units: tuple[GoUnit, ...]
@dataclass
class Discovery:
    repo: str; repo_root: str; units: list[GoUnit]; unit_of_file: dict[str, str]
    groups: list[Group]; builds: list[BuildConfig]; toolchain: str
    mode: str                           # "offline" | "download"
    module_source: str                  # "vendor" | "file-proxy" | "none" | "proxy"
    peers: list[Peer]; skipped: dict[str, int]; warnings: list[str]
```
`units.json` = spec §3.8 shape, written by `go_units.write_units_json(discovery, path)`.
Functions (D produces, B1/B2 consume):
- `go_units.discover(repo_root: str, repo: str, *, builds: list[BuildConfig], peers: list[Peer], mode: str, output_dir: str, toolchain: "Toolchain") -> Discovery`
- `go_units.scan_go_files(repo_root) -> tuple[list[str], dict[str, int]]` (relpaths; skip counts by reason) — applies the go command's skip rules and symlink dedupe.
- `go_units.parse_go_mod(text) -> dict`, `parse_go_work(text) -> dict`, `parse_modules_txt(text) -> dict`, `go_units.match_file(relpath, content_head: bytes, build: BuildConfig) -> bool`
- `go_units.prepare_control_dirs(discovery, output_dir) -> None` (writes go.work / -modfile copies; spec §3.5; group rule per spec R4: the go.work found by walking up from the module root, if it lists the module, else a single-module group; one group per module)
- `go_toolchain.find_toolchain() -> Toolchain` (`Toolchain(go: str, version: str, goroot: str)`), `go_toolchain.env_for(group, build, *, mode, output_dir, user_modcache) -> dict[str, str]` (C8), `go_toolchain.ensure_helper(toolchain, output_dir) -> str` (abs path of cached binary), `go_toolchain.setup_go(dest) -> Toolchain`, `go_toolchain.version_tuple("go1.26.8") -> (1, 26, 8)`
- `go_modcache.user_modcache(toolchain) -> str | None`, `go_modcache.download(discovery, toolchain, output_dir) -> dict` (per-group report)

## C5 — Resolution records (`resolutions.json` v1)

Spec §5.8 shape. Record fields (all present on every record):
`site{caller,kind,name,line,col}`, `status`, `via`, `target`, `static_target`, `external`, `candidates[]`, `open_world`, `build_variants[]`, `reason`, `build`.
- `target` = `{"unit","package","relpath","qualname","line","kind":"method|class"}` (+ `"repo"` for peer targets).
- `external` = `{"module","version","package","qualname","peer":bool}` or null.
- `candidates[]` = targets plus `"test_only"`, `"via_embedded_iface"`.
Other per-file record lists (ruling 2026-10-14, B2 produces; B3/B5 consume):
- `imports[]`: `{"line","col","import_path","status","target","external","reason"}`; `target` for an in-repo or peer package is `{"unit","package","dir","clause"}` (+ `"repo"` for peers).
- `embeds[]`: `{"type":{"relpath","qualname"},"embedded":<C5 target, kind "class">|null,"status","external","pointer"}`.
- `implements[]`: `{"type":{"relpath","qualname"},"iface":<C5 target, kind "class">|null,"status","external","pointer_receiver"}`.
- `entities.json` = `{"version":1,"repo":…,"extractor_revision":…,"helper_revision":…,"entities":{relpath: FileRecord}}` (as codedoc_python plus the helper revision).
**Staleness covers every record list (ruling 2026-10-14, from B3's review):** `files[rel].sha256` in resolutions.json equals the scan hash **iff every load saw those bytes**; otherwise it carries the observed hash or `"changed-during-load"`. G8 (`stale`) applies to `calls`, `imports`, `embeds` and `implements` of a changed file, and to `embeds`/`implements` whose other end lies in a changed file. B3 refuses records of such files and any edge ending in one. Import `target.clause` = the clause of the directory's build-included non-xtest files (unique), never "the first file by relpath".
Download-mode `GOSUMDB` comes from the config key `GO_SUMDB` (default `sum.golang.org`; `evaluate --env` sets `off`).
Mapping helper class → status/via (`go_classify.classify_site(rec, ctx) -> dict`):

| class | location (spec §5.3) | status | via |
|---|---|---|---|
| builtin | – | builtin | none |
| func / method | row 6 in-repo node | resolved | types |
| func / method | rows 2–5, 8 | external | none |
| func / method | row 3 peer | external (peer=true) | none |
| iface / typeparam | interface in-repo | resolved (target = interface method) + candidates | types |
| iface / typeparam | interface outside | external + in-repo candidates | none |
| iface, anonymous interface (`recv_type == ""`) | – | ambiguous (reason `implementation`) with the in-repo candidates; `dynamic` when there are none; never a `MethodCalls` edge | types |
| value + alias proven | – | as the alias target, via `convention:func-alias` | |
| value | – | dynamic | none |
| funclit | – | resolved (closure node) | convention:funclit |
| linkname | in-repo target / else | resolved / external | convention:linkname |
| range | – | as the ranged function | convention:range |
| unknown / stub / embed_invalid | – | unresolved (`missing-module` / `incomplete-type` / `load-error`) | none |
Gates G1–G10 (spec §5.6) live in `go_gates.py`: `apply_gates(record, site_ctx) -> record`.

## C6 — Graph schema (`setup_spanner_graph.py`)

`NODE_SPEC` / `EDGE_SPEC` exactly as spec §6.1/§6.2 (column order as listed there; `embedding` `vec` on every node table). Builders:
- `pipeline.build_node_rows(data) -> dict[str, list[dict]]` (table → rows as column dicts)
- `pipeline.derive_edge_rows(data, node_index) -> dict[str, list[dict]]`
- `pipeline._make_id(kind, repo, relpath, qualname)` unchanged from codedoc_python; `ID_SCHEME = 1`.
- `OUTPUT_DIR/repos/<repo>/graph_rows/<Table>.jsonl` is **always** written (spec R20); `--no-spanner` only skips the Spanner writes. `--wiki` reads graph context from these files.
Leiden additions (L1/L2): `Topics` extra columns and tables `ClassBelongsToTopic`, `TopicDependsOn`, `TopicContainsTopic` per the leiden spec §4.

## C7 — Wiki artifacts

- `summaries/nodes/<relpath>.json`: `{"sha256","mode":"batched|per-node","model","prompt_version","file":"…","nodes":{"<qualname>":"…"}}`
- `summaries/packages.json`: `{"<dir>|<name>": {"hash","summary"}}`; `summaries/dirs.json`: `{"<dir>": {"hash","summary"}}`
- `topics/topic_tree.json` (codedoc_go LLM topics): `[{"id","name","summary","files":[relpath…],"subtopics":[{"name","summary","files":[…]}]}]`; `topics/<id>.md` pages.
- `topics/stats.json` (both variants): `{"discovery":{"calls","input_tokens","output_tokens","seconds"},"pages":{…same…}}`.
- `graph_checkpoint.json`: `{"id_scheme":1,"complete":bool,"phase":"…","file_hashes":{relpath:sha},"resolutions_hash":"…","spanner":{"instance","database","graph","id_prefix"}|null,"communities_hash":null}` (leiden fills `communities_hash`).

## C8 — Config keys and the go environment

Spec §2.5 keys plus `WIKI_SUMMARIZE_GENERATED` (default `0`) and `WIKI_SUMMARY_CACHE` (default unset → `OUTPUT_DIR/repos/<repo>/summaries`; the topic comparison points both variants at one cache) (spec R11), exactly (`config.py`): `GO_TOOLCHAIN`, `GO_BUILD_GOOS`, `GO_BUILD_GOARCH`, `GO_BUILD_TAGS`, `GO_BUILD_CGO`, `GO_EXTRA_BUILDS`, `GO_MODULES_DOWNLOAD`, `GO_PROXY`, `GO_USER_MODCACHE`, `GO_HELPER_TIMEOUT`, `WIKI_FILE_MAX_CHARS`, `WIKI_FILE_MAX_NODES`, `PEER_REPOS` + family keys. `env_for()` builds the spec §3.6 table from scratch (only `HOME`, `USER` inherited).

## C9 — CLI (`__main__.py`)

Spec §2.4 exactly. `analyze` requires exactly one of `--all/--graph/--wiki` (argparse mutually exclusive, required). Exit codes: 0 ok; 2 usage; 3 preflight failure; 4 `--wiki` precondition failure; 1 other errors.

## C10 — Corpus formats (A1/A2/A3 produce; B5 consumes)

`test_codes/<fixture>/ground_truth/cases.py` (stdlib only) defines `CASES: list[dict]`, `DECL_PINS: list[dict]`, optional `FEATURES` (F1). Helpers mirror codedoc_python with Go fields:
```python
def target(qualname, module, kind, file):   # kind: "method" | "class"; qualname per C3 grammar
    return {"qualname": qualname, "module": module, "kind": kind, "file": file}
def resolved(*targets, static=None, runtime=(), candidates=(), open_world=None): ...
def ambiguous(*candidates, runtime=()): ...
def external(module): ...            # module path, "std", or "C"
def plain(status, reason=None, runtime=()): ...   # dynamic | unresolved | builtin | conversion
# `conversion` (ruling 2026-10-13): correct iff resolutions.json has NO record at the site; wrong iff any record names a target.
def case(cid, file, snippet, token, kind, expect, why, entry="reception-desk", occurrence=1, distractors=()): ...
```
`manifest.json` (from `generate_manifest.py`): per case adds `line`, `col` (1-based byte col of the token), `offset`. `DECL_PINS`: `{"qualname","module","kind","file"}`. `qa_questions.json` as in codedoc_python. F1 also ships `ground_truth/features.json`: `{"features":[{"name":"…","files":[relpath…]}]}` written from the corpus design (never from any topic output).
**F2 layout repos (ruling 2026-10-13):** each `test_codes/go_layouts/<layout>/` is analyzed as its **own repo** (repo name = layout dir); `go_layouts/_outside/` (underscore: never a layout) holds targets of out-of-repo replaces; cases stay in one fixture-level `go_layouts/ground_truth/cases.py`, and one `go_layouts/ground_truth/expected_units.json` holds the discovery facts keyed by layout (= repo name).
**Per-mode expectations (ruling 2026-10-14):** `case(..., offline=<expect>)` is optional; `expect` is the env/download-mode answer and the manifest carries `"expect_offline"` (null or an anchored expect). `evaluate` scores the offline run against `expect_offline` when it is non-null, else against `expect`. Used only where the correct answer really differs between modes (a dependency with a newer `go` line, missing go.sum lines, published-only targets); third-party externals keep the rule "offline `unresolved` = unanswered". `evaluate --env` is download mode against `file:///…/test_codes/_proxy`.
**F3 edge labels (ruling 2026-10-13):** `expected_cross_repo_edges.json` rows are `{"table","kind"|null,"source","target"}` (+ `"import_path"` for `CrossRepoImports`) with labels — files `"<repo>:<relpath>"`, packages `"<repo>:<dir>|<clause name>"`, classes and methods `"<repo>:<relpath>:<qualname>"`.
Each fixture: `README.md` (intent, no comments in fixture code), `entries.json` (`{"<entry>": {"dir": "...", "cmd": ["go","run","./cmd/x"] | "test": ["./..."]}}`) for the oracle.

## C11 — goracle (A1)

Port of `codedoc_go/docs/superpowers/research/2026-10-13/probes/runtime-oracle/oracle/`. CLI: `goracle instrument --src DIR --out COPY [--mode all]`, `goracle report --copy COPY --hits DIR`, `goracle match --obs obs.jsonl --records records.jsonl`, `goracle judge --obs obs.jsonl --manifest manifest.json`. Formats per research report §5.1; verdicts per spec §9.4. Driver `test_codes/_gt/run_oracle.py <fixture>` runs instrument → entries → report → judge and exits non-zero on any failed executed case.

## C12 — Leiden `communities.json` (L1)

```json
{"version":1,"backend":{"name":"graspologic-native","version":"…"},"profile":"strict",
 "params":{"seeds":[42,43,44,45,46],"randomness":0.01,"iterations":10,"band":[5,25],"subtopic_min_atoms":12},
 "sweep":[{"resolution":0.5,"count":11,"modularity":0.61}],"resolution":0.5,
 "trials":[{"seed":42,"modularity":0.61,"membership_hash":"…"}],"seed_ari":0.88,
 "levels":{"1":{"<topic_id>":{"members":[class_id…],"roles":{class_id:"primary|attached-file|attached-test|attached-generated"},
                               "hub":class_id,"cohesion":0.12,"conductance":0.31,"parent":null}},
           "2":{…"parent":"<level-1 topic_id>"}},
 "unassigned":[class_id…],"atoms":N,"edges":M,"projection_hash":"…","membership_hash":"…"}
```

---

## Waves and work packages

| Wave | WPs (parallel) |
|---|---|
| 1 | H1 extract · H2 resolve · D discovery/toolchain · A1 proxy+oracle+gt tools · A2 F1 corpus · A3 F2+F3 corpus · B1 fork skeleton · L1 Leiden topics core |
| 2 | B2 extraction+resolution integration · B3 schema+builders · B4 wiki track · B5 evaluate+crossref+peers · C1 surfaces |
| 3 | B6 trial · L2 leiden fork + compare-topics · C2 docs · integration fixes |
| 4 | Final verification (lead): pytest, go test, evaluate ×2, trial ×2 repos, real-Gemini comparison, reviews, commit + push |

### WP-H1 — `gohelper extract` (Go, stdlib only)
**Files:** `gohelper/internal/extract/*.go`, `*_test.go`, `testdata/`. Seed: `research/.../probes/code-seams/probes/goextract/main.go` (513 lines).
**Produces:** `extract.Run(args []string, stdout io.Writer) error` implementing C2-extract and C3.
**Acceptance tests (go test):** every site shape and kind of spec §4.3 (call/go/defer/range, funclit token = `func`, index base ident, `f()(x)` inner only; extract emits **every** call expression including conversions, because syntax cannot tell `T(x)` from `f(x)` — resolve classifies them, C2); callers (function, closure, `<init>`, `init#k`); qualnames (methods value/pointer, generic receiver without type params, interface methods, closures nested `F.func1.func1`, `<init>.func1`, `init#2`, `_#1`); `owner_relpath` across files and with build twins; `build_included` via `go/build` `Context.MatchFile` (filename suffixes, `//go:build`, `// +build`, `ignore`, `cgo` with cgo off); `generated` via `ast.IsGenerated`; `//line` directive ignored (`PositionFor(p,false)`); syntax error → declarations after first error omitted; byte columns on a line with multibyte characters; `name_range` exact; doc comments.
**Verify:** `cd gohelper && go test ./internal/extract/...`; run on etcd and Prometheus clones (`$R/repos`) — 0 errors, 1,840 records, and qualname uniqueness per file.

### WP-H2 — `gohelper resolve` (Go, x/tools v0.52.0 vendored) + `main.go`
**Files:** `gohelper/go.mod`, `go.sum`, `vendor/`, `main.go`, `internal/resolve/*.go`, `*_test.go`, `testdata/` (fixture modules). Seed: `research/.../probes/real-repos/helper/`.
**Produces:** subcommands `version`, `extract` (wires H1), `resolve` per C2. Build: `go build -mod=vendor -trimpath -o <out> .`.
**Acceptance tests:** classes of C2 on fixture modules (static func, method value/pointer receivers, promoted depth 1/2 with shadowing, method expression, generic func/method/alias, iface with 0/1/n in-repo implementers, open_world via stdlib type, test_only candidate, embedded-interface second dispatch, typeparam constraint call, func-typed local/param/field/map/pkgvar, func-alias proof positive and each negative (reassigned, address taken, assigned in closure, pkg var), IIFE/go/defer funclit, linkname pull, asm body-less decl, range over func value, conversions dropped and counted, builtins); workspace pattern `work` with two modules; single-module `./...` with `-modfile` (verify the alternate go.sum is used and the module dir is untouched); variants disagreement (`_test.go` adds a shallower method) → `variants`; symlinked file dedupe; the three offline traps (fixtures from `research/.../probes/real-repos/probes/shadow` and stubs) with an empty GOMODCACHE + GOPROXY=off; `embed`/`implements`/`import` records; `summary` counts.
**Verify:** `go test ./...` (offline: vendor + fixture modules only); `gohelper resolve` on etcd and Prometheus copies in download mode: site counts within 1 % of real-repos.md §4.2 (72,623 / 114,808 minus conversions), load time ≤ 2× the report's numbers.

### WP-D — discovery, toolchain, module sources (Python)
**Files:** `graph_generator/go_model.py`, `go_units.py`, `go_toolchain.py`, `go_modcache.py`, `tests/test_go_units*.py`, `tests/test_go_toolchain.py`, `tests/test_go_modcache.py`. Seeds: `research/.../probes/go-modules/scripts/` (`gomodparse.py`, `gobuildctx.py` — validated against the go command).
**Produces:** C4 functions, C8 `env_for`.
**Acceptance tests:** go.mod/go.work/modules.txt parsing (blocks, comments, quoted paths, retract ranges, tool, ignore, godebug); skip rules (testdata, `_x`, `.x`, vendor, nested modules, symlink dedupe); unit ownership; groups (go.work with in/out-of-repo/missing `use`, modules outside go.work → `module` groups); control dir: relocated go.work with absolute paths + go.work.sum copy + workspace vendor symlink; `-modfile` copy + go.sum copy; peer replaces only for required module paths, not for vendored groups, refused for duplicate module paths; toolchain discovery order and `version_tuple`; too-new go line → group status `toolchain`; `env_for` exact keys for offline/download/vendored; user modcache detection; `test_control_dir_never_writes_repo` (marker `go`): temp git repo with go.work + two modules, run `go list -e -deps -test work` through `env_for` → repo files and `.git/index` mtime unchanged, `go.work.sum` appears only in the control dir; `setup_go` verifies SHA-256 (download mocked).
**Verify:** `.venv/bin/python -m pytest -q tests/test_go_units*.py tests/test_go_toolchain.py tests/test_go_modcache.py --no-cov` and `-m go`.

### WP-A1 — synthetic proxy, goracle, ground-truth tools
**Files:** `test_codes/_proxy/` (GOPROXY layout: `example.com/thirdparty/{uuidlite,textfmt,yamlish}/@v/{list,vX.info,vX.mod,vX.zip}` for v1.0.0 and v1.1.0, built by `test_codes/_proxy/build_proxy.py` from sources in `test_codes/_proxy/src/`), `test_codes/_gt/{gt_lib.py,generate_manifest.py,check_ground_truth.py,run_oracle.py}`, `test_codes/_gt/oracle/` (goracle Go module, stdlib only), `test_codes/_gt/tests/`.
**Produces:** C10 tooling, C11 goracle.
**Acceptance tests:** proxy served via `GOPROXY=file://…` + `GOSUMDB=off` resolves both versions (go test using a temp module); goracle unit tests from the prototype plus `judge` mode tests; `generate_manifest.py` on a mini fixture (snippet → line/col/offset; non-unique snippet error names the case); `check_ground_truth.py` checks of spec §9.2 item 3 on a mini fixture.
**Verify:** `go test ./...` in `_gt/oracle`; `python3 -m pytest -q test_codes/_gt/tests` (stdlib + pytest via `uv run --no-project --with pytest`).

### WP-A2 — F1 `iryo-kanri-system` (Go)
**Files:** `test_codes/iryo-kanri-system/**` (≈12 modules joined by a root `go.work`, plus one module outside it with a local `replace` and one published-version requirement on `example.com/thirdparty/...`; apps `reception-desk`, `admin-console`, `patient-portal` with `cmd/` mains and demo entry points; `ground_truth/{cases.py,manifest.json,qa_questions.json,features.json}`, `entries.json`, `README.md`).
**Content:** spec §9.1 categories with ≈130 cases and ≈90 DECL_PINS; XPR byte-identical pairs; TWIN collisions; no comments in fixture code; `go 1.26` lines; every case's `expect` from Go semantics, confirmed by goracle where executable; interface cases expect the interface method plus `runtime_targets`.
**Verify:** `go build ./...` and `go vet` per module under the workspace (GOFLAGS=-mod=mod is forbidden; use the `_proxy`); `generate_manifest.py` → `check_ground_truth.py` exit 0 → `run_oracle.py` all executed cases pass.

### WP-A3 — F2 `go_layouts` and F3 `multi_repo`
**Files:** `test_codes/go_layouts/**`, `test_codes/multi_repo/{shared,web,api,…}/**` with their `ground_truth/` and `entries.json`.
**Content:** spec §9.1 F2 list (≈20 units, ≈45 cases) and F3 (repos `shared` module `example.com/iryo/shared`, `web`, `api`; exact `expected_cross_repo_edges` in F3 ground truth; negative controls: fork, undeclared dependency, vendored peer copy, peer also published in `_proxy`).
**Verify:** same chain as A2 per fixture.

### WP-B1 — fork skeleton
**Files:** create `codedoc_go/` by copying `codedoc_python/` **excluding** `.venv`, `output_docs*`, `test_codes/`, `.coverage`, `__pycache__`, `webapp/frontend/node_modules`; then delete Python-specific modules (`treesitter_parser.py`, `lsp_client.py`, `py_*.py`, `crossref.py`/`evaluate.py` stay for B5 to adapt) and their tests; adapt `config.py` (C8), `__main__.py` (C9 incl. `doctor`, `setup go`, `trial` stubs calling into D/B6), `pipeline.py` (graph track then wiki track; `--no-spanner`; wiki preconditions per spec §7.1; Phase 2–6 function shells calling B4's API; Phase 8–10 shells calling B3's API), `requirements.txt` (drop `tree-sitter*`, `pyright`, `nodejs-wheel-binaries`, `PyYAML` unless still needed; keep the rest of the freeze), `pytest.ini` (markers `go`, `network`; `--cov=graph_generator --cov-fail-under=85` stays — the lead may waive the coverage gate until wave 3), `.venv` creation, root `.gitignore` gains `codedoc_go/.tools/`.
**Produces:** importable package with stubs for every C4–C7 function (raising `NotImplementedError("WP-x")`) so other WPs can integrate.
**Acceptance tests:** CLI parsing (mutually exclusive modes, exit codes C9), config defaults (spec §2.5), wiki preconditions (`test_wiki_refuses_without_graph`, `test_wiki_refuses_on_changed_hashes`, `test_wiki_refuses_on_spanner_target_mismatch`, `test_wiki_no_spanner_rule`), `--all` ordering, fakes in `conftest.py` still work.
**Verify:** `.venv/bin/python -m pytest -q --no-cov` green.

### WP-L1 — Leiden topics core (Python, standalone package)
**Files:** `codedoc_go_leiden/graph_generator/topics/{__init__,graph_builder,community,metrics,naming,reports,compare}.py` and `codedoc_go_leiden/tests/test_topics_*.py` (a temporary venv under the WP scratch dir until L2 forks the variant). Inputs are C6 rows (dicts) — build synthetic rows in tests.
**Produces:** `graph_builder.build_projection(nodes, edges, profile) -> Projection` (atoms, weighted edges, ledger); `community.detect(projection, params) -> Communities` (C12); `metrics.{ari,ami,nmi,modularity,conductance,cohesion}`; `naming.build_naming_prompt/parse_names`; `reports.build_evidence/grounding_check`; `compare.compare_topics(llm_tree, communities, rows, features=None) -> dict`.
**Acceptance tests:** leiden spec §7 test list; modularity of Codex's worked example = 11/26 (two features), −44/169 (directories); ARI/AMI/NMI equal frozen scikit-learn values on 5 random partitions (compute once with `uv run --no-project --with scikit-learn` and freeze); determinism (same input → same membership hash, 3 runs); isolates/thin merge/split acceptance; IDs and Jaccard carry-over; backend missing → clear error.
**Verify:** `pytest -q codedoc_go_leiden/tests/test_topics_*.py`.

### WP-B2 — extraction + resolution integration (after B1, H1/H2, D)
**Files:** `graph_generator/go_extract.py`, `resolution.py`, `go_classify.py`, `go_gates.py`, tests + `tests/fixtures/helper/<case>/` recorded outputs (regenerated with `-m go`).
**Acceptance tests:** every C5 mapping row; every gate G1–G10; freshness (changed file re-extracted; stale during run → `stale`); group toolchain gate; build twins → `build_variants`; extra builds (`GO_EXTRA_BUILDS`) resolve excluded files; join on `(relpath, offset)` with symlinks; stats; artifact atomic write.
**Verify:** pytest with recorded outputs; `-m go` against F1 once A2 lands.

### WP-B3 — schema, builders, Phases 8–9, validate
**Files:** `setup_spanner_graph.py`, the builder section of `pipeline.py`, tests.
**Acceptance tests:** DDL from spec (tables, PKs, property graph), `write_columns`, node rows per kind (pseudo-types, closures, interface methods, methods outside their type's file with `class_id` of the type), edge derivation rules of spec §6.3 (incl. cap, dedupe, FileDependsOn), `--no-spanner` jsonl, checkpoint/meta (C7), `validate` from spec.
**Verify:** pytest (FakeSpannerDb).

### WP-B4 — wiki track
**Files:** `node_summaries.py`, wiki sections of `pipeline.py`, `prompts.py`, tests.
**Acceptance tests:** batching with graph context; `test_large_file_goes_per_node`; `test_missing_nodes_retried_individually`; closures not sent; cache reuse by sha256; package/dir summaries keyed by child hashes; topics link by relpath (no basename loss); `topics/stats.json`; Phase 10 update mutations and embeddings (Files, Packages, Classes, Methods excl. closures, Topics); Japanese prompts.
**Verify:** pytest (FakeGenaiClient, FakeSpannerDb).

### WP-B5 — evaluate, crossref, peers
**Files:** `evaluate.py`, `crossref.py`, tests.
**Acceptance tests:** scoring (correct/alternate/wrong/unanswered incl. distractors), gates of spec §9.5, offline vs env mode settings (`_proxy`, GOSUMDB=off, isolated cache), F3 exact edge set, crossref peer edges and drops report, module-cache targets not matched.
**Verify:** `python -m graph_generator evaluate` and `--env` exit 0 with 0 wrong once A2/A3 land.

### WP-B6 — trial
**Files:** `trial.py`, tests.
**Acceptance tests:** a tiny module end to end (graph rows, stats, oracle match with a planted wrong record → WRONG detected); repo untouched check; changed-answers report between modes.
**Verify:** `python -m graph_generator trial` on etcd and Prometheus copies (offline + download), precision check with the research report's packages: 0 WRONG-*.

### WP-C1 — surfaces; WP-C2 — docs
C1: agent instruction (spec §6.5 + leiden cookbook in L2), SKILL.md, mcp_server/webapp/deploy defaults, `gql_lint.py`, `doccheck.py` adapted. C2: the five Japanese docs + CLAUDE.md + `.env.example` + `sample_prompt.txt` + Dockerfile; docs gate green; verification table filled from real runs.

### WP-L2 — `codedoc_go_leiden` fork + compare-topics
Copy `codedoc_go` (same exclusions) once wave 2 is green; move L1's package in; Phase 4a in `--graph`, 4b/5 in `--wiki`; schema additions; `compare-topics` CLI; docs adapted (same five-file set); tests; evaluate still 0 wrong.

## Final verification (lead)
1. `codedoc_go`: pytest (≥ 85 %), `-m go`, `go test` (gohelper, oracle), `evaluate` and `evaluate --env` exit 0 with 0 wrong, docs gate.
2. Trial on etcd and Prometheus (offline + download) with oracle precision: 0 WRONG-*; numbers into `docs/LSP_RESOLUTION.md`.
3. Real Gemini + Spanner `--all` on F1 into databases `codedoc-go` / `codedoc-go-leiden` of the existing instance (never touching other databases); `--wiki --no-spanner` on etcd and Prometheus for both variants; `compare-topics` on F1, etcd, Prometheus; results into `codedoc_go_leiden/docs/ARCHITECTURE.md`.
4. Fresh-context reviews (correctness and requirement gaps), fixes, re-run gates.
5. Commit on branch `feat/codedoc-go` from the current HEAD, no Claude attribution; push.
