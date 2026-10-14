# Spec review: codedoc_go and codedoc_go_leiden

Reviewed revisions (both unchanged at 21:42):
- `codedoc_go/docs/superpowers/specs/2026-10-13-codedoc-go-design.md`, mtime 2026-10-13 21:36:55;
- `codedoc_go_leiden/docs/superpowers/specs/2026-10-13-codedoc-go-leiden-design.md`, mtime 21:17:03.

Probes live under `/home/admin/scratch/codedoc-go-research/spec-review/`. The isolated Go environment is in `env.sh` (toolchain go1.27.2, `GOTOOLCHAIN=local`, `GOENV=off`, scratch `GOMODCACHE`/`GOCACHE`, read-only `file://` proxy on the shared research cache). `bin/probe` (source `helper/main.go`) loads patterns with go/packages and prints each call site's callee object with adjusted and raw positions. Nothing outside this directory was changed.

| Probe | What it shows |
|---|---|
| `p1/` | `-modfile` with go/packages; go.sum behaviour in single-module groups |
| `p2/` | G3 misses the cross-package embedding case |
| `p3/` | targets with no node (local, anonymous, inline-constraint interfaces); blank methods; `//line` positions |
| `p6/` | newer `toolchain` line vs newer `go` line; `go env` toolchain switching |
| `p7/` | a dependency with a newer `go` line fails the whole load |
| `p8/` | a relative `file://` GOPROXY is rejected |
| `p10/` | a non-ancestor go.work changes the runtime target |
| `p11/` | GOPROXY falls back from a `file://` proxy to the next entry |
| `pyprobe/gn_api.py`, `pyprobe/gn_behaviour.py` | graspologic-native 1.3.1 API and behaviour (venv `gn-venv/`) |
| `pyprobe/gen_decls.py` | Gemini call volume for Phase 2 on etcd and Prometheus |

Line numbers refer to the revisions above.

---

## Blocker and major findings

### 1. Blocker. codedoc_go §5.6 G3 (line 547): G3 misses the cross-package case

- **Problem.** G3 fires only "in a package with import errors". Mechanism 3 also happens when the invalid embedded field sits in a type declared in another in-repo package. The caller's package then has no import errors; it is only `IllTyped` through its dependency.
  - The same gap applies to method values bound by `func-alias` (§5.5), `range` over a promoted method value, and method expressions.
  - The spec does not say how gate results combine across package variants. The test variant can have import errors while the production variant has none.
- **Evidence (probe `p2/repo/m`).** Setup:
  - Package `b` imports the missing `github.com/go-logr/logr` and declares `type S struct{ logr.Logger; Mid }`, `type Mid struct{ Inner }` and `func (Inner) Info(msg string, kv ...any)`.
  - Package `a` imports only `b`, declares `type A struct{ b.S }` and calls `x.Info("x")`.

  Results:
  - Download mode (file proxy): `x.Info` → `github.com/go-logr/logr.Logger.Info`, selection path `[0 0 4]` (depth 2, external).
  - Offline (empty cache, `GOPROXY=off`): `x.Info` → `example.com/m/b.Inner.Info`, path `[0 1 0 0]` (depth 3, in-repo).
  - Package `a` reports `errs=0 illtyped=true`. Package `b` reports `could not import github.com/go-logr/logr`.
  - So the default (offline) mode writes a wrong in-repo `MethodCalls` edge.
  - The research probe for mechanism 3 (real-repos §6.2) used one package. The trial (0 changed answers on etcd and Prometheus) does not cover this case.
- **Fix.**
  - Drop the package-error condition. For every promoted selection used as a target (call, method value bound by `func-alias`, method expression, `range` operand), walk the receiver's embedding tree down to the selection depth. Refuse (`incomplete-type`) if any embedded field type there is invalid.
  - Evaluate gates for each package variant and keep the most conservative result.
  - Add the cross-package case to F2 and to the Go tests.

### 2. Major. codedoc_go §5.3 row 6, §5.4.1, G7 (lines 506, 515, 551): targets that have no node

- **Problem.** Local interface types, anonymous interface literals and inline constraints are in-repo targets with no node. code-seams §3.2 says local types are not nodes.
  - G7 says "no node *contains* it", which reads as containment. Under containment these calls become self-edges.
  - Python's `DeclIndex.at()` returns a "value" hit for any position inside a member that is not on a name range (`codedoc_python/graph_generator/py_classify.py:99-113`). codedoc_python maps that hit to `dynamic` (`resolution.py:164`, `:184-185`).
  - The Go spec does not say how such a hit maps for an `iface` or `typeparam` site.
- **Evidence (probe `p3/repo/m/m.go`).** All three targets fall inside a function's span:
  - `n.Name()` through a local `type namer interface{ Name() string }` → target `m.go:13:24`, inside `Local` (lines 12-18).
  - `v.(interface{ Name() string }).Name()` → `m.go:17:23`, inside `Local`.
  - `x.Name()` in `func Gen[X interface{ Name() string }](x X)` → `m.go:20:23`, on `Gen`'s own declaration line.
- **Fix.**
  - Map a target only by the exact name range of a node of the expected kind (function, method, interface method).
  - Any other in-repo position is `unresolved` (for example reason `local-declaration`), with no candidates.
  - Add F1 GEN and IFC cases for all three shapes.

### 3. Major. codedoc_go §5.2 (line 491) vs §4.1 (line 380): resolve positions and `//line`

- **Problem.** Only extraction must use `PositionFor(p, false)`. The resolve contract only says "the target object's position". The research helper that will be ported uses adjusted `fset.Position` (`real-repos/helper/main.go:487-493`).
- **Evidence (probe `p3/repo/m/gen.go`).** The file has `//line gram.y:100` before `Lined` and `//line m.go:10` before `Lined2`.
  - The call `CallLined → Lined2` has adjusted target `m.go:10:0` and raw target `gen.go:7:6`.
  - Line 10 of `m.go` really holds `func (T) Name()` (name at `10:10`). A line-based lookup gives a wrong edge `CallLined → T.Name`; an exact lookup gives a silent miss.
  - The adjusted target of `Lined` is `gram.y:100:0`, which is not a repo file, so it becomes row 9 `outside-repo`.
- **Fix.**
  - State that `resolve` uses unadjusted positions for both sites and targets.
  - Add an F2 case whose `//line` directive points at another in-repo `.go` file.

### 4. Major. codedoc_go §3.2 (lines 287-292): the group rule can pick the wrong runtime truth

- **Problem.** The rule "a go.work that lists the module → one workspace group per go.work" is ambiguous in two cases:
  - The go.work is not an ancestor of the module. The go command walks up from the cwd, so a build run from the module's own directory never sees that go.work.
  - Two go.work files list the same module. That gives two groups and duplicate sites, with no rule to merge them.
- **Evidence (probe `p10/`).** Setup: `repo/api` prints "in-repo"; `repo/server` requires the published `example.com/api v1.0.0` (the proxy copy prints "published"); `repo/tools/go.work` uses `../api ../server`.
  - `go run .` from `repo/server` prints `published`.
  - With `GOWORK=repo/tools/go.work` it prints `in-repo`.
  - The spec's rule loads `server` in the tools workspace, so it writes an in-repo edge where the developer's build reaches the published version. §9.5 counts that as wrong ("another module").
- **Fix.**
  - A module's group is the go.work found by walking up from the module root, if that go.work lists the module.
  - Otherwise the module is a single-module group (`GOWORK=off`), with a warning when some other go.work lists it.
  - Each module belongs to exactly one group.

### 5. Major. codedoc_go §3.3, §5.1, §5.6 G6: a failed load for a whole group is not handled

- **Problem.** The §3.3 pre-check reads only the repo's own `go` lines. A dependency whose `go` line is newer than the toolchain fails the whole `packages.Load` of a workspace group. A helper crash, a `GO_HELPER_TIMEOUT` or running out of memory have no defined outcome either.
- **Evidence (probe `p7/`).** A workspace with module `m` requires `example.com/newgo v1.0.0`, whose go.mod says `go 1.28`, from a file proxy:
  - `LOAD ERROR: err: exit status 1: stderr: go: module example.com/newgo@v1.0.0 requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)`.
  - No packages are returned, so `m`'s purely in-repo call `H()` is lost along with everything else.
- **Fix.**
  - When the helper exits non-zero or times out, set the group to status `error`.
  - Mark all its sites `unresolved`: reason `toolchain` when the go command says "requires go >=", else `load-error`.
  - Print the message in `doctor` and in the run summary, and continue with the other groups.
  - Have `doctor` pre-check dependency `go` lines from the cached `.mod` files and from `## explicit; go 1.N` lines in `modules.txt`.

### 6. Major. codedoc_go §9.3-9.4 (lines 863, 871-876): holes in the oracle rules

- **Problem and evidence.**
  - **Missing WRONG-OTHER condition.** The table drops the research's second WRONG-OTHER condition: "no instrumented callee but `exec > 0` and the target is hooked" (`research/2026-10-13/runtime-oracle.md` §5.2, line 221).
    - Without it, a site that ran only uninstrumented code (stdlib or a dependency) while CodeDoc claimed a hooked in-repo target counts as "says nothing".
    - That is exactly the open-world failure: `testutil.TB` → `*testing.T`, and engine-probe IF04 (`*log.Logger`).
  - **The judge rule passes vacuously.** The rule as summarized (`observed ⊆ targets ∪ runtime_targets`, every `runtime_target` observed) holds when nothing instrumented was observed.
    - codedoc_python's judge also requires a resolved case to reach a target, and an external case to observe an external callee (`codedoc_python/test_codes/_gt/trace_oracle.py:453-481`).
  - **No source for the "implements" facts.** IFACE-CONSISTENT requires "a type that implements the interface". goracle is stdlib-only and works on go/ast, and the spec does not say where those facts come from.
- **Fix.**
  - Add the missing condition to both `match` and `judge`.
  - When a site executed, require the expected in-repo target to be observed.
  - For `external` expectations, require `exec > hits` and no in-repo callee.
  - Take the "implements" facts from the helper's records.

### 7. Major. codedoc_go §9.6 (lines 906-910), §3.6 (line 337), §3.7 (line 347): the trial's offline arm

- **Problem.**
  - Offline mode reads the user's module cache by default. Because one missing `.mod` file drops every external package of a workspace group (go-modules R9), the trial's offline numbers depend on whatever happens to be cached, so they are not reproducible.
  - `GOMODCACHE=OUTPUT_DIR/go/modcache` is used by both modes. After one download run, later "offline" runs see the downloaded modules: the offline loss reads 0 and gates G2-G4 are never exercised.
  - "Every changed answer must be explained or gated" is not a pass/fail rule.
  - The spec does not say which mode's records the precision check scores.
- **Evidence.**
  - `~/go/pkg/mod` exists on this host and is partial (`du -sh` gives 759M).
  - The research's offline baseline was an empty cache plus `GOPROXY=off` (real-repos §6).
- **Fix.**
  - The trial's offline arm uses an empty, dedicated `GOMODCACHE` with `GOPROXY=off`; the download arm uses another cache.
  - Fail the trial on any offline `resolved` target that differs from download mode. Investigate any answer that is `resolved` only offline.
  - Score the download-mode records with the oracle.

### 8. Major. Leiden §3.3-3.4 (lines 85-96): an empty projection crashes 4a

- **Evidence (`pyprobe/gn_behaviour.py`, graspologic-native 1.3.1).**
  - `leiden([])` raises `EmptyNetworkError`.
  - `leiden([("a","b",0.0)])` panics in Rust (`called Option::unwrap() on a None value`). It surfaces as `pyo3_runtime.PanicException`, whose MRO is `(PanicException, BaseException, object)`, so `except Exception` does not catch it.
- **Problem.** 4a runs inside `--graph`. A fixture repo (F2 units, F3 repos) or a small real repo with no eligible edges would crash `--graph` and `evaluate`. A level-2 induced subgraph with no edges hits the same path.
- **Fix.**
  - When the total weight W is 0, or a level-2 subgraph has no edges, emit singletons or a leaf without calling the backend (Codex §7.5).
  - Never pass zero or NaN weights.
  - Wrap backend calls in `except BaseException`, so a failure stops 4a with a clear message instead of the graph track.

### 9. Major. Leiden §3.3 level 2, §3.4, done #2 (lines 89, 93-96, 43): the integrity gate cannot be implemented as written

- **Problem.** The gate requires that every eligible atom has exactly one primary leaf, that each level covers every atom once, and that children's union equals the parent. The spec leaves these questions open:
  - a. Level 2 re-clusters the induced subgraph. Isolates attached by file have no edges, and graspologic takes only an edge list, so they vanish. Test and generated atoms are not in the projection at all. How are they assigned to children?
  - b. Does `SUBTOPIC_MIN_ATOMS` count attached atoms?
  - c. Is 「未分類」 a `Topics` row? Which role do its members get (the role list has `primary` and `attached-*`, nothing for unassigned atoms)? Is it named by the LLM? Does the gate's "exactly one primary leaf" count it?
  - d. Are leaves that were not split carried to level 2 (Codex §8.3 frontier rule)?
  - e. Are attached atoms part of `membership_hash`? If so, a test-file edit changes topic IDs.
  - f. What breaks ties in thin-community merges ("most shared weight"), isolate attachment ("most other atoms of their file") and hub PageRank?
  - g. A 2-atom component with no neighbour goes to 未分類, while a single isolate is attached by file. That is inconsistent.
  - h. Excluded atoms "share weight" with communities, but they are outside the projection. Which relations count?
- **Fix.**
  - Primary membership = the clustered atoms.
  - Re-attach the other atoms at every level, inside their parent, with the same rules.
  - Make 未分類 one `Topics` row per repo, with role `unassigned`, not sent to the LLM, and counted by the gate.
  - Carry leaves forward.
  - Compute `membership_hash` over primary members only.
  - Break ties by the smallest class_id or membership hash.
  - Compute excluded-atom weights with the same relations and α on a `with_tests` projection.

### 10. Major. Leiden §3.5 and §4 vs codedoc_go §1 (line 87): stale topic rows pile up in Spanner

- **Problem.** Topic IDs change with any membership change, but the family only upserts rows.
  - Each re-run with membership changes leaves the old rows in `Topics`, `ClassBelongsToTopic`, `FileBelongsToTopic`, `TopicDependsOn` and `TopicContainsTopic`.
  - Spanner then shows several primary topics per class, and the cookbook's "topic of a class" and "members of a topic" return stale answers.
  - Claude §2.2 lists this same weakness for the LLM topics.
- **Fix.** On each 4a write and each Phase 10 topic write, delete the repo's rows in the five derived topic tables, then insert. This is Claude's D5: "rewrites the derived tables for the current ID_PREFIX".

### 11. Major. Leiden done #4 (line 45) vs codedoc_go done #6 (line 96): a large billable run without the user's consent

- **Problem.** Done #4 requires real Gemini wiki runs on etcd and Prometheus for both variants. The parent spec says billable runs happen only when the user asks. "The user asked the lead to proceed on recommendations" is not consent to a large spend.
- **Evidence.** Counted from `code-seams/out/go-{etcd,prometheus}.jsonl` (types, funcs, methods, interface methods and aliases), with files over 40,000 bytes or 40 declarations going node by node:
  - etcd: about 3.8k per-node calls plus about 1.06k batched files, about 4.9k calls;
  - Prometheus: about 6.9k per-node calls plus about 0.62k batched files, about 7.5k calls per variant;
  - about 25k calls for both variants;
  - generated files are included: etcd 18 files with 1,752 declarations, Prometheus 9 files with 858.
- **Fix.**
  - Make the real run conditional on the user's go-ahead, with this estimate.
  - Share the Phase 2 cache between the variants, since content hashes and prompts are identical.
  - State the per-node volume in codedoc_go §7.2.
  - Consider deterministic summaries for generated files. Requirement 6 fixes per-node calls for large files, so this needs the user's say.

---

## Minor findings

### 12. Minor. codedoc_go §3.5 and §3.7 (lines 310, 319, 357): go.sum in single-module groups

- **Evidence (probe `p1/repo/s`, empty go.sum in `p1/ctl-s`).**
  - `go list -e -deps -test -json` with `GOWORK=off`, `-mod=readonly` and `-modfile=ctl-s/go.mod` reports `missing go.sum entry for module providing package github.com/google/uuid (imported by example.com/s)`.
  - `ctl-s/go.sum` stays at 0 bytes and nothing is downloaded.
  - The same module through a one-`use` generated go.work loads and writes `go.work.sum` (2 lines).
- **Problem.** "Single-module downloads write `go.sum`" and "verified against `go.sum` and sum.golang.org" do not hold for single-module groups. Readonly mode never writes and never asks the checksum database; missing lines fail per package in both modes. F2's "missing `go.sum` lines" case has no stated outcome.
- **Fix.** Correct the text. State the F2 expectation: either `unresolved` (`missing-module`), or the one-`use` go.work fallback in download mode so the checksum database can fill the lines.

### 13. Minor. codedoc_go §3.5 (line 319) and the §13 `-modfile` risk row: the B1 check

- **Evidence (probe `p1/`).**
  - What works:
    - `-modfile=ctl-a/go.mod` with a relative `replace example.com/b => ../b` resolves against the module root (target `p1/repo/b/b.go:3:6`);
    - `-mod=vendor -modfile=ctl-v/go.mod` puts targets in `p1/repo/v/vendor/…`;
    - nothing is written into the repo.
  - What fails: `GOFLAGS="-modcacherw -buildvcs=false -modfile=…"` without `-mod` gives `LOAD ERROR … build flag -modfile only valid when using modules`. go/packages first runs `go list -e -f {{context.ReleaseTags}} -- unsafe` with `GO111MODULE=off` (seen in the `PLOG=1` trace).
- **Fix.** Pass `-modfile` through `packages.Config.BuildFlags` and on the download `go list` command line, not through `GOFLAGS`. Keep an explicit `-mod`.

### 14. Minor. codedoc_go §2.5 (line 220): the `GO_USER_MODCACHE` default can trigger a toolchain download

- **Evidence (probe `p6/n`, a go.mod with `go 1.28`).**
  - `GOTOOLCHAIN=auto GOPROXY=off go env GOMODCACHE` prints `go: downloading go1.28.0 (linux/amd64)` and then `toolchain not available`.
  - With `GOTOOLCHAIN=local`, or from a neutral cwd, it prints the cache path.
- **Fix.** Run it from a neutral cwd with `GOTOOLCHAIN=local`, keeping only the user's `GOMODCACHE`, `GOPATH` and `HOME`.

### 15. Minor. codedoc_go §2.7 (line 245) vs §3.7 (line 348), §2.1 (line 114): what stops a run is unclear

- **Problem.**
  - "37 modules are missing offline …" is listed as a refusal, but §3.7 says missing modules fail closed and the run continues.
  - `doctor` never separates errors from warnings.
  - The precondition for `--all` is "as `--graph`", though `--all` also needs Gemini configuration.
- **Fix.**
  - List the stop conditions: no usable toolchain, every group too new, Spanner schema missing without `--no-spanner`, Gemini configuration missing for `--wiki` or `--all`.
  - List the warnings: missing modules, degraded groups, newer `toolchain` lines, low memory.

### 16. Minor. codedoc_go §4.3 (line 427) and §5.5: `range` sites cannot be found or verified as specified

- **Problem.**
  - Whether `f` in `for v := range f` is a function is a type fact that go/parser cannot give, so extraction cannot emit exactly those sites.
  - goracle attributes implicit range calls to no site (runtime-oracle §2.3, rule 5; 899,778 such hits on Prometheus), so `range` edges are never verified.
- **Fix.**
  - Extraction emits a candidate for every `range` over an identifier or selector; resolve drops operands that are not functions and counts them like conversions.
  - Give goracle an attribution rule (the single `range` site on the `for` line), or list `range` edges as unverifiable.

### 17. Minor. codedoc_go §4.2 (lines 405-416): blank methods collide

- **Evidence (probe `p3`).** `func (T) _() {}` written twice in one file builds and vets cleanly.
- **Problem.** Both get qualname `T._` in the same relpath, and `_claim_id` raises on a different owner (`codedoc_python/graph_generator/pipeline.py:1839-1840`).
- **Fix.** Number blank methods `T._#k`, as is already done for `_#k`.

### 18. Minor. codedoc_go §6.1 (line 616): owners of methods whose receiver type has build twins

- **Problem.** The owner rule is missing for methods whose receiver type has build twins (Prometheus has 128 such methods) and for receivers that are not found. code-seams §2.2 and §3.4 give it: the same-file twin, else the twin compiled by the primary build, else `<package>` with a flag. Without it, `class_id` and `ClassDefinesMethod` can point at the wrong twin, which is a wrong structural edge.
- **Fix.** Copy the rule into §6.1.

### 19. Minor. codedoc_go §5.4 (lines 516-518): the dispatch interface is not specified

- **Problem.**
  - For calls through a struct's embedded interface field (1,076 etcd sites, real-repos §4.2), the "receiver's static type" is a struct.
  - The dispatch interface should be the embedded field's interface type, as the research helper does (`real-repos/helper/main.go:711-727`).
  - Interface types should not be candidates, because `types.Implements(I2, I)` is true for any wider interface `I2`.
- **Fix.** State both rules.

### 20. Minor. codedoc_go §2.3 (line 173) and §7.2: no stated source for `--wiki` graph context

- **Problem.** `graph_rows/*.jsonl` is written only with `--no-spanner`. A separate `--wiki` run after a Spanner `--graph` has no stated source for callers, callees and candidates.
- **Fix.** Always write `graph_rows`, or say that `--wiki` re-derives the rows from `entities.json` and `resolutions.json` with the pure builders.

### 21. Minor. codedoc_go §3.1 (lines 264-272): go.mod `ignore` directories are parsed but not skipped

- **Problem.** The go command's `./...` and `work` patterns skip `ignore`d directories (go-modules §4). Those packages still load as dependencies when another package imports them, so some of their files would be resolved and others not.
- **Fix.** Treat ignored directories like `testdata` (not scanned, targets there `unscanned`), or define the rule.

### 22. Minor. codedoc_go §9.5 (line 886): the fixture proxy URL is invalid

- **Evidence (probe `p8/`).** `GOPROXY=file://test_codes/_proxy` gives `go: invalid file:// proxy URL with non-path elements: file://test_codes/_proxy`.
- **Fix.** Use an absolute `file:///…/test_codes/_proxy`.

### 23. Minor. codedoc_go: numbers that do not match the research

- §3.7 line 350, "10× smaller than `go mod download`": for Prometheus it is 1.76 GB vs 2.8 GB, about 1.6× (go-modules §12 says "1.6–12×").
- §3.4 line 306, "221 sites in etcd … with the primary configuration": that is the `CGO_ENABLED=1` figure (29 files). With the spec's `CGO_ENABLED=0` primary build, etcd excludes 33 files (real-repos §2), so more sites.
- §0 line 32, "named the same target at all 187,431 call sites": the engines agreed wherever both named a target. Neither named one at 3,848 sites, and only gopls answered at 422 (real-repos §5.3).
- §3.3 line 298 says the `toolchain` case is "checked by the F2 fixture", but F2's list (lines 804-818) has no newer-`toolchain`-line case.
- **Fix.** Correct the numbers, and add the `toolchain`-line case to F2.

### 24. Minor. Leiden §2 and §4 (lines 53, 56, 120): circular phase order

- **Problem.** 4a runs after Phase 9, yet the topic rows are "written in Phase 9", and the graph checkpoint key includes the communities hash. That is circular.
- **Fix.** Run 4a between 1.6 and 8 (it reads only the derived rows), or add a Phase 9b with its own checkpoint.

### 25. Minor. Leiden §3.3 (lines 85-88): trial selection and the resolution sweep

- **Evidence (`pyprobe/gn_behaviour.py`).** On Codex's six-type example, `leiden` at γ=1 returns quality 0.4231 (=11/26). At γ=0.5 it returns 0.6731 for the same partition, and `gn.modularity(…, resolution=0.5)` also gives 0.6731.
- **Problem.**
  - "Highest normalized modularity recomputed by our own implementation" does not say at which γ. Comparing modularity at γ=1 across trials run at another γ does not select the best trial for the objective that was optimized.
  - "Longest plateau" is undefined on a non-uniform grid (grid points or γ width).
  - There are no tie rules for equal plateaus or for "closest to the band".
  - `hierarchical_leiden` is named, but level 2 uses `leiden` on induced subgraphs.
- **Fix.**
  - Compare trials by modularity at the trial's own γ (`gn.modularity` with `resolution=γ`).
  - Plateau length = number of consecutive grid points; on a tie, take the smaller γ.
  - Drop `hierarchical_leiden`, or say where it is used.

### 26. Minor. Leiden §3.1-3.2 (lines 64-79): projection details

- **Problem.**
  - **Closures inflate counts.** `F→G` and `F.func1→G` count as two distinct pairs for one owner pair (n=2).
  - **`implements` makes hubs.** It is structural in Go, so small in-repo interfaces (one method) are satisfied by many types and become hubs at α 0.5 each. In C# and Java this relation is declared intent.
  - **Build-excluded atoms.** Atoms with `build_included=false`, such as Prometheus `model/labels` twin types, have no resolved edges and always end up isolated or in 未分類.
- **Fix.**
  - Collapse callers to their outermost declaration before counting pairs.
  - Ablate `implements` (or require two or more methods, or evidence of use) and report the effect.
  - Exclude build-excluded atoms from eligibility, or attach them with their own role.

### 27. Minor. Leiden §6 (line 146): compare-topics inputs

- **Problem.**
  - **Path.** `--llm …/repos/<repo>` plus `topics/topic_tree.json` does not match codedoc_go §2.3, whose per-repo outputs list `summaries/topics.json` and `docs/`, not `topics/topic_tree.json`. The siblings write `OUTPUT_DIR/topics/topic_tree.json`, not per repo (`codedoc_python/graph_generator/pipeline.py:506`, `:1606`).
  - **Same atom graph.** Identical file hashes do not guarantee the same atom graph: the build configuration, offline vs download mode, and `resolutions.json` can differ.
  - **Metric partition.** The structural-quality metric needs a partition, but atoms of LLM files with several topics are only "reported".
- **Fix.**
  - Fix the path in one of the two specs.
  - Also require the same build configuration, mode and `resolutions.json` hash, or recompute both partitions on one projection.
  - Exclude atoms of multi-topic files from modularity and report them.

### 28. Minor. codedoc_go §8.2: peer substitution can degrade a whole group offline

- **Problem.** Substituting a peer's source adds the peer's requirements to MVS.
  - Offline, in a workspace group, one missing `.mod` among them drops every external package of the group (go-modules R9; §8 cases O2b and O2c).
  - A peer whose `go` line is newer than the toolchain fails the whole load (finding 5).
- **Fix.** `doctor` checks the requirement closure and `go` lines of substituted peers, and the run reports per group when peers degrade external resolution.

### 29. Minor. codedoc_go §2.6: the entity reuse stamp misses module ownership

- **Problem.** Entity records carry `module`, `package` (import path) and `origin`, which depend on the owning go.mod (module path, nested go.mod added or removed) as well as on the file. The stamp covers only the file hash, extractor revision, helper hash and build configuration. A module rename or a new nested go.mod leaves stale fields in reused records while `resolutions.json` is fresh.
- **Fix.** Add the owning go.mod path and module path (or its hash) to the stamp.

### 30. Minor. codedoc_go §7.2 and §7.4: the summary cache ignores graph context

- **Problem.** Phase 2 prompts include graph context (callers, callees, candidates), but the cache key is only the file's SHA-256. A change in another file leaves stale context in the summaries of unchanged files.
- **Fix.** Include a hash of the context in the key, or state the trade-off in the docs.

### 31. Minor. codedoc_go §9.2: no stated home for pins and QA questions

- **Problem.** §9.5 gates F1 on node coverage over about 90 declaration pins and on QA ≥ 85 %, but the ground-truth chain does not say where the pins and the QA questions live. codedoc_python's `evaluate` reads `ground_truth/qa_questions.json` and fails the gate when it is missing (`codedoc_python/graph_generator/evaluate.py:705-717`).
- **Fix.** Name the files (pins in `cases.py`, `qa_questions.json`) and have `check_ground_truth.py` validate them.

### 32. Minor. codedoc_go §9.3: goracle setup for modules outside a go.work

- **Problem.** The prototype adds the `_goracle_rt` module through `go.work use` (runtime-oracle §2, `prepare.sh`). F1's module outside the go.work, and any single-module repo, need something else.
  - A go.work in the copy would redirect published-version requirements to in-repo source (probe `p10`) and so change the runtime truth.
- **Fix.** For single-module groups, add the runtime module with `require` and `replace` in the copy's go.mod, and keep `GOWORK=off`.

### 33. Minor. codedoc_go §3.3: the helper's `go` line sets the real minimum

- **Problem.** x/tools v0.52.0 needs go ≥ 1.26.0 (real-repos §1). If the helper's go.mod says `go 1.27`, as the research helper's does (`real-repos/helper/go.mod`), a go1.26 toolchain cannot build it under `GOTOOLCHAIN=local`. §3.3 says the minimum is "the higher of the helper's go line and 1.25".
- **Fix.** Pin the helper's go line to 1.26 and state that the effective minimum is go1.26.

### 34. Minor. codedoc_go §3.7: download mode fetches cached modules again

- **Evidence (probe `p11/`).** `GOPROXY=file://<empty dir>,file://<proxy>` falls back to the second proxy and fetches `example.com/api`.
- **Problem.** Download mode uses only `GO_PROXY`, so modules already in the user's cache are fetched again over the network.
- **Fix.** In download mode use `GOPROXY=file://<user modcache>/cache/download,https://proxy.golang.org`. Checksums are still verified against go.sum and the checksum database.

### 35. Minor. codedoc_go §3.1 and §3.7: no action for an inconsistent `vendor/`

- **Problem.** `modules.txt` is checked against go.mod, but the action on inconsistency is not defined.
  - With `-mod=vendor` the go command refuses the module ("inconsistent vendoring").
  - With `-mod=readonly` it silently uses the module cache, which is a build the developer cannot run.
- **Fix.** An inconsistent `vendor/` gives group status `error` with the go command's message. Alternatively mark it `degraded` and use readonly mode, and say so.

### 36. Minor. codedoc_go §5.5: `linkname` and `func-alias` details

- **Problem.**
  - a. `linkname` targets can name methods (`pkg.(*T).m`); how those are parsed is not specified.
  - b. `func-alias` accepts "a method value with a concrete receiver". But `f := s.Read` on a struct with an embedded `io.Reader` has a concrete receiver expression and an interface method.
  - c. Method expressions (`f := T.M`, `f := I.M`) are not mentioned.
- **Fix.** State the method-target parsing. Decide by the selected method object (an interface method behaves like `iface`), not by the receiver expression's type.

### 37. Minor. codedoc_go §8.3 and §5.3 row 3: qualnames of peer targets

- **Problem.** `crossref` needs `{repo, relpath, qualname}` for peer targets, but the spec does not say how the local run computes the peer qualname. codedoc_python parses the peer file on demand (`py_classify.external_symbol` / `_qualname_in`).
- **Fix.** Pick one: extract the peer's files, or have the helper emit qualnames from the types objects (`T.M`, `F`, `I.M`).

### 38. Minor. Leiden §3.5: carry-over reuses stale reports

- **Problem.** At Jaccard ≥ 0.6, up to 40 % of the members may have changed, yet the previous report is reused unchanged.
  - A page can describe members that left, and the grounding check still passes because those identifiers exist in the graph.
  - compare-topics' cost metric is skewed on re-runs.
- **Fix.** Reuse only the name and regenerate the report when membership changed, or mark reused reports with the membership diff. Compare cost on cold runs.

### 39. Minor. Leiden: partial resolution is silent

- **Problem.** When a group fails, or offline loses edges, atoms fall into 未分類 silently. Codex §8.7 says to fail the coverage gate or publish an explicitly partial report.
- **Fix.** Record resolution coverage per group in `communities.json`, and mark topics `partial` when a group failed.

### 40. Minor. Leiden §4 (line 116): `FileBelongsToTopic` role

- **Problem.** A `role` (`primary` or `secondary`) is introduced for `FileBelongsToTopic` but not listed as a column change, and ties for "most of its declarations" have no rule.
- **Fix.** Add the column. On a tie, take the smaller topic_id.

---

## Checked and found correct

- **Fixed in the 21:36 revision while I reviewed.**
  - Newer `toolchain` lines no longer fail closed. Probe `p6`: `go 1.26` plus `toolchain go1.27.9` loads under `GOTOOLCHAIN=local`; `go 1.27.9` fails with "go.mod requires go >= 1.27.9".
  - The edge-table count is now 14. That is codedoc_python's 11 `EDGE_SPEC` tables plus `ClassEmbeds`/`ClassImplements` replacing `ClassInherits` (+1), plus `PackageContainsFile` and `CrossRepoImports`.
- **`-modfile` with go/packages** works when `-mod` is explicit: relative `replace` paths resolve against the module root, and vendored targets point into `vendor/` (probe `p1`).
- **Module rules match the research (go-modules R1-R12):** the workspace vs single-module runtime difference, absolute `GOWORK`, `go.work.sum` written beside the generated go.work and never into the repo, `GOPROXY=off` plus `GOSUMDB=off` for offline, the read-only `file://` proxy, the download command and its sizes.
- **Interface design is consistent** with real-repos §4.1 and §4.3 and engine-probe IF04: a static edge plus `PossiblyCalls`, never a single-implementer upgrade, candidates from the receiver's static type, and `NeedDeps` needed for the open-world flag.
- **Loader traps** (golang/go#39986 attachments, stub names, package variants) match real-repos §6.2.
- **Qualname grammar** gives no duplicates on the trial repos (code-seams §3.4), except the blank-method case (finding 17).
- **Oracle.** The mechanism (mode `all`, completion counters, build flags, measured numbers) matches runtime-oracle, and the verdict rows other than those in finding 6 match its §5.2.
- **graspologic-native 1.3.1:**
  - The signatures match: `leiden(edges, starting_communities, resolution, randomness, iterations, use_modularity, seed, trials, max_local_moving_iterations)` returns `(quality, {node: id})`; `hierarchical_leiden` has no `trials`; `modularity(edges, communities, resolution)`.
  - Modularity is normalized: 11/26 and −44/169 reproduce Codex §8.5.
  - The returned quality is modularity at the run's own γ.
  - Runs are deterministic on identical canonical input. A shuffled edge order or flipped endpoints change the partition, so the spec's canonical input is required.
  - Duplicate edges are not summed, so the spec's one edge per pair is required.
  - Disconnected components are never merged, even at γ=0.01.
  - The Python 3.13 wheel installs (with networkx 3.7, numpy 2.5.4, scipy 1.18.1).
  - `starting_communities` did not panic on my small input; this is irrelevant, since the spec never passes it.
- **Requirements covered:** corpus sizes (F1 120-150, F2 about 45, F3 multi-repo with an exact edge set); three run modes; per-file batching with per-node calls for large files; the docs set in both specs; `evaluate` in both modes; the etcd and Prometheus trial with the oracle check.

## Verdict

**Changes required.**
- Blocking: finding 1.
- Fix before implementation: findings 2-11.
- Findings 12-40 can be folded into the plan.
