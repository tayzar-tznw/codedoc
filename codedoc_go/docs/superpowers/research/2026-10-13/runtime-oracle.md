# Track `runtime-oracle`: a runtime ground-truth oracle for Go call edges

Research report for the `codedoc_go` spec, 2026-10-13. Toolchain: Go 1.27.2 (shared, `$R/toolchain/go`), linux/amd64.
Repos: etcd v3.7.2 (68c065e), Prometheus v3.15.0 (5241a27). `$W` = `/home/admin/scratch/codedoc-go-research/runtime-oracle`.
Every number below comes from a probe run kept under `$W` (paths in each section and in the evidence index), or from a cited source. "Source" means the Go 1.27.2 toolchain source under `$R/toolchain/go/src`.

## 0. Findings that matter for the design

1. **The mechanism is source instrumentation of a copy, stdlib only.** Every function body and func literal starts with `__goracle.E(fid)`, a hook that records the return PC of the caller's call instruction. The tool is built on `go/ast` and `go/parser`, plus a runtime package of about 450 lines (tool: about 1,450 lines). Coverage, pprof, runtime/trace and delve cannot give "which function did this call site enter" at an acceptable cost; the measurements are in §1.
2. **Line granularity is not enough.** 38.7 % of etcd's 42,195 call sites and 40.2 % of Prometheus's 114,196 share a line with another call (`$W/measure/stats-final.txt`). `runtime.Frame` has no column. The oracle therefore gives each call its own site id: it writes a `/*line FILE#SID:L:C*/` directive before the call's `(` and restores the position right after it. The runtime then reports `Frame.File = "FILE#SID"` with the **true line**.
3. **Implicit calls happen at lines that hold ordinary call sites.** Found in probes and on real code:
   - Deferred calls run at the executed `return` line or at the closing brace.
   - After a recovered panic, deferred calls run at an unpredictable line inside the deferring function. This case was found only on Prometheus.
   - A range-over-func loop calls its iterator from the `for` line.

   Tagging *every* site makes misattribution impossible by construction: an untagged hit can never land on a site. It broke 0 of 11 test packages. Its observations equal the selective mode's exactly (3,184 and 10,489 pairs).
4. **The runtime must elide wrappers, and it does.** `runtime.Callers` skips method wrappers, `-fm` method values, `deferwrap`/`gowrap` closures and reflect call stubs, so the hook sees the real call site. Goroutine entries see `runtime.goexit`; the oracle recovers the `go` statement from the "created by" line.
5. **`runtime.Callers` per call is too slow on real code: ~340 ns per hook.** Prometheus `promql` timed out at 10 minutes against 8.6 s uninstrumented. A frame-pointer fast path costs ~10 ns, and each new (function, return PC) pair is checked once against `runtime.Callers`. With it, the fast and slow paths gave identical observations on etcd (3,185 pairs, 0 differences). `promql` then runs in 47 s, against 8.4 s uninstrumented and 12.5 s with inlining off.
6. **Hooks alone cannot see executions that enter uninstrumented code (stdlib, deps).** Such a site can be falsely CONFIRMED (a mixed in-repo and stdlib dispatch) or reported as unexercised. A completion counter after each simple statement fixes this: when `exec > hits`, the site entered something uninstrumented. Real cases this caught:
   - etcd `client.go:476 cancel()`: 8 in-repo entries, 14 completions.
   - Prometheus `functions.go:1752 f(el.F)`: 1,660 in-repo entries, 207,460 completions (the rest went to `math.*` functions).

   Counters cover 76–82 % of sites. The rest (return statements, `if`/`for`/`switch` headers, `go`/`defer`) stay `CONFIRMED-NOEXEC`.
7. **Real-code result.** All 6 etcd and all 5 Prometheus test packages pass instrumented. Observations:
   - etcd: 2,884 sites with an instrumented callee, 3,026 site→callee pairs, 84 multi-target sites.
   - Prometheus: 8,844 sites, 10,401 pairs, 694 multi-target sites.

   Runtime names matched the instrumenter's prediction for all 4,034 entered functions. A naive same-package-name "resolver" run through `match` got 78 WRONG verdicts on Prometheus; all 78 are real shadowing errors.

## 1. Mechanisms surveyed

| # | Mechanism | Probe | What it gives | Site → callee? | Cost (measured) | Verdict |
|---|---|---|---|---|---|---|
| i | Callee-entry hook (`runtime.Callers` or frame pointer) in a rewritten copy | `$W/probe0..4`, `$W/oracle` | Exact callee (hook id), caller return PC → file:line; site id via line directive | Yes, all dispatch kinds | 9.8 ns/hook fast path, 340 ns slow (`$W/bench/out.txt`); etcd mvcc +8–11 %, promql ×5.6 | **Chosen** |
| ii | Call-site instrumentation (wrap each call or set a marker before it) | design analysis; used partly | Needs types for most wrappings: untyped constant args, generic functions, conversions vs calls, builtins and multi-value calls cannot be wrapped syntactically | Only with `go/types` | — | Used only for statement completion counters and site ids, both purely syntactic |
| iii | `-cover` / `GOCOVERDIR` | `$W/mech/probe3.cover` | Block counts: the interface site `lib.go:47.3,48.1` ran 2× and `(*myWriter).Write` is 100 % covered, but nothing says which implementation a site entered | No | — | Not enough: no callee identity; a block is not a call (`&&`/`||`, panics) |
| iv-a | delve `dlv trace` (v1.27.2, installed to `$W/bin`) | `$W/mech/dlv` | Tracepoint per regexp-matched function, args and returns | Only with `--stack` | **2.7 ms per traced call** (5.42 s for 2,000 calls vs 5.7 µs untraced) | Not practical: ~10⁶× slower; one breakpoint per function |
| iv-b | eBPF uprobes (incl. `dlv trace --ebpf`, "experimental") | not probed: bpftrace, bpftool and perf are absent, and installing them needs root package installs (outside this track's rules) | Entry probes could read the return address | Possible | unmeasured | Rejected: needs root and is not stdlib (unverified beyond docs) |
| v | pprof CPU sampling | `$W/mech/pprof-vs-oracle.txt` | 100 Hz samples (source: `runtime/pprof/pprof.go` `const hz = 100`) | Statistical | free | Found 86 of the oracle's 1,527 pairs on etcd mvcc (5.6 %); its 2 extra pairs are inlining artifacts. Never complete |
| v | runtime/trace | docs only | Goroutine, syscall, GC and heap events with stacks "for most events" | No | — | Not a call tracer |

## 2. Prototype

Source:
- `$W/oracle/rt/rt.go`: runtime package, module `codedoc.dev/goracle/rt`, stdlib only (`unsafe` and one linkname).
- `$W/oracle/cmd/goracle/{main,instrument,report,match}.go`: the tool, stdlib only.

Driver scripts: `$W/scripts/{env,prepare,godirs,measure}.sh`.

```
scripts/prepare.sh SRC COPY DIRS all        # cp -a, add _goracle_rt to go.work, goracle instrument
cd COPY && GORACLE_OUT=DIR go test -vet=off -gcflags=all=-l -ldflags=-checklinkname=0 ./pkg/...
goracle report -sites COPY.sites.jsonl -hits DIR -out obs.jsonl
goracle match  -obs obs.jsonl -sites COPY.sites.jsonl -edges resolver.jsonl
```

### 2.1 Rewrite rules (`instrument`; line numbers never change)

| Rule | Text inserted | Why |
|---|---|---|
| Hook | `__goracle.E(fid);` right after the `{` of every FuncDecl body and FuncLit | Callee identity is the fid, which maps to the declaration position in the site table. It does not depend on runtime names |
| Import | `; import __goracle "codedoc.dev/goracle/rt"` after the package name, on the same line | Keeps line numbers. The rt module joins the workspace through `go.work use` (no go.mod edit needed, as probe2 showed) |
| Site id | `/*line ABS#SID:L:C*/(/*line ABS:L:C+1*/` around the call's `(` | The call instruction takes the position of `(`: a directive placed before the callee token had no effect (probe1). The site id travels in the file name and the true line is kept |
| Tag scope | `all` (recommended), or `needed`: lines with 2+ calls, exit lines and all calls of functions that contain `defer`, and `for … range` lines | See §3 and §4.4 |
| Completion counter | `; __goracle.D(stmt)` after each ExprStmt, AssignStmt, IncDec, Send or `var` DeclStmt in a statement list. It covers the calls in the statement except those under the right operand of `&&`/`||` or inside func literals. Never after `panic(...)` | `exec` counts completions. A site with `exec > hits` entered uninstrumented code. The spec makes `&&`/`||` right operands conditional and `panic` a terminating statement |
| Skipped | `//go:nosplit` functions, cgo files, files that already contain line directives (all logged as `skipped:<reason>`) | None occurred in etcd or Prometheus |

### 2.2 Runtime (`rt`)

- **Fast path (amd64).** E reads its saved BP, which is the callee's frame pointer, and takes the word above that as the callee's return address. The pair (fid, PC) is looked up in a lock-free open-addressing table of 2²⁰ keys with atomic counts.
  - The offset of E's saved BP is calibrated at init by calling E from a known frame. The value differed between builds (16, then 24), so calibration is required.
  - A new pair is inserted with a *pending* bit and checked once against `runtime.Callers`. If the frame-pointer PC is not the logical caller, the pair is marked indirect and its hits always take the slow path. This happens for wrappers, reflect stubs and inlined callees.
  - Instrumented functions always save BP: amd64 omits it only for tail-calling (NOFRAME) functions and frameless leaves (source: `cmd/internal/obj/x86/obj6.go`), and every hooked function calls E.
  - Stack copying adjusts saved frame pointers, and stacks are not shrunk at async safe points (source: `runtime/stack.go`).
- **Goroutine entries.** If the caller PC is the shared return PC into `runtime.goexit`, the hook parses the "created by" lines of `runtime.Stack(buf,false)`. That gives the line of the `go` keyword.
- **Output.** At exit, through a linkname to `internal/runtime/exithook.Add`, the runtime writes `hits-<pid>.jsonl`. The linkname needs `-ldflags=-checklinkname=0` (Go 1.23 release notes). Optional `GORACLE_SNAPSHOT_MS` snapshots guard against crashes. `GORACLE_SLOW=1` forces the `runtime.Callers` path.

### 2.3 Attribution (`report`)

Each hit is attributed by the first rule that applies:

1. **Tagged file** `ABS#SID`: goes to that site.
2. **Exit line** of a function with defers: goes to that function's defer group. With one defer in the function, the group attribution is exact.
3. **Single untagged site on the line:** goes to that site. This cannot happen in mode `all`.
4. **Caller is a function with defers** (no site on the line): goes to its defer group, route `defer-resume` (deferred calls after a recover).
5. **`for … range` line:** counted as `range-implicit`, not attributed.
6. **`runtime.goexit`:** goes to the `go` statement at the created-by line.
7. **Caller outside instrumented files:** counted as an external caller (callbacks from `sort`, `testing`, bbolt, reflect, `runtime.gopanic`).

## 3. Probe results (`$W/probe0`, `probe1`, `probe2`, `probe3/src`, `probe4`, `probe5`)

| Construct | What the runtime reports (probe) | Oracle result |
|---|---|---|
| Plain call; value/pointer receiver incl. auto `&`/`*` | caller = line of `(` (probe1 `multiline`: lines 16, 17) | correct |
| Promoted method | direct call goes to `Inner.M`; through an interface the `Outer.M` wrapper is elided (probe0 l.117, 120) | correct; reports the declaring method |
| Interface dispatch to 2 types | both callees from one line (probe0 l.122) | site with 2 callees → `WRONG-MULTI` for a single-target edge |
| Generics | `main.Map[...]`, `main.(*Stack[...]).Push`, constraint call `t.String()` → `Name.String` from `Str[...]` (probe0); generic func values, method values and method expressions on generic types (probe2) | correct |
| Closures | named → `main.main.func2`; called immediately → `func3`; nested → `Nested.func1.1` (probe3) | correct |
| Method value / method expression | `-fm` wrapper elided; `(*V).ValM(p)` wrapper elided (probe0 l.132–137) | correct |
| Func-typed field, map of funcs | direct (probe3 181, 182) | correct, incl. package-level closure `lib.init.func1` |
| Interface method expression `W.Write`, `G[int].Get`; method value of a method promoted from an embedded interface (two wrappers deep) | every wrapper elided; the caller is the real site (`$W/probe5/out.txt`, lines 18, 20, 23, 25, 27) | correct |
| `defer` | at normal exit the caller is the function at the **executed `return` line** (probe1 `retCall` line 30, `bareRet` 37) or the closing brace; loop defers also report the closing brace (48); `deferwrap` elided; `defer f()` with no params/results is not wrapped (source: `typecheck/stmt.go` `normalizeGoDeferCall`) | defer group per function; exact for single-defer functions (probe3 `Deferred`, `OneLiner`) |
| Panic, then recover | while panicking the caller is `runtime.gopanic`; after the recover, the remaining defers report the next `return` line (probe4 f: 14, f2: 21) or the faulting line (f3: 28; Prometheus `engine_internal_test.go:51`) | panicking → external; after recover → `defer-resume` (6 hits on Prometheus) |
| `go` statement | entry caller = `runtime.goexit`; `gowrap` elided; "created by F in goroutine N / file:line" gives the **`go` keyword line**, untagged even when the `(` is tagged (probe1 `goTagged`) | correct (probe3: 3 go sites; etcd: 37; Prometheus: 10) |
| Package var initializer, `init()` | `var x = f()` → caller `pkg.init` at the var line; `init.0` is called by `runtime.doInit1` | correct (probe3 lib.go:16, 20) |
| reflect | caller = `reflect.Value.call` (probe0) | never attributed to the user site; that site's own callee is `reflect.Value.Call` |
| Test functions | called by `testing.tRunner`; their calls are normal sites; external test package `lib_test.helper` | correct |
| Several calls on one line | identical caller lines (probe0 l.109) | mode `none`: 58 hits ambiguous, 39 pairs lost, 7 misattributed; tags: exact (probe3 line 163: 4 calls, 4 correct) |
| range-over-func | iterator called from the `for` line without a CallExpr (probe0 `seq` from line 157); body is `F-range1` | `range-implicit` (899,778 hits on Prometheus) |
| `sort.Slice` less | caller `sort.insertionSort_func` | external |
| Inlining on (no `-l`) | identical observations on probe3 and probe2 | correct (slow path), see §6 |

Matching on probe3 (`$W/runs/probe3-v3-match.txt`; edges in `$W/runs/probe3-edges.jsonl`) gives the expected verdict for each simulated resolver answer:
- CONFIRMED: a static call, a method value, a generic method, the 3rd of 4 calls on one line.
- WRONG-MULTI: an interface call claimed as `Circle.Area`.
- WRONG-OTHER: a func field claimed as `One`.
- `Write` claimed as `(*myWriter).Write`: the hooks alone said CONFIRMED (`$W/runs/probe3-match.txt`); the completion counter changes it to WRONG-MULTI.
- `sort.Slice` claimed as an in-repo function: UNEXERCISED without the counter, WRONG-OTHER with it.
- IFACE-CONSISTENT: edges to `Shape.Area` and `io.Writer.Write`.

## 4. Real code

**Instrumented:**
- etcd: all 787 files outside `tools/` and `tests/` (8,091 functions, 42,195 sites).
- Prometheus: all 711 files outside `web/ui`, `documentation`, `internal/tools` and `compliance` (14,688 functions, 114,196 sites).

Rewrite time: ~0.9 s each, after making edit application linear; the first version took 8.8 s on Prometheus.

**Tests run:**
- etcd: `pkg/adt`, `pkg/wait`, `server/etcdserver/api/v2store`, `server/lease`, `server/storage/mvcc`, `client/v3`.
- Prometheus: `model/labels`, `model/relabel`, `tsdb/chunkenc`, `storage`, `promql`.

None of them needs network or open ports; all pass uninstrumented.

### 4.1 Overhead (final code, tag `all`; `$W/measure/{etcd,prom}-final.tsv`, `base-l-runs.txt`, `etcd.tsv`, `prom.tsv`)

Two runs each on the shared 32-core machine while other agents were working. Build times are warm-cache and not reported.

| Package | Base s | `-l` only s | Hooks only s | Hooks + counters s | Size base → inst (vs `-l` build) |
|---|---|---|---|---|---|
| etcd mvcc | 10.34 / 10.34 | 10.71 / 10.73 | 11.23 / 11.71 | 11.46 / 11.22 | 25.1 → 23.5 MB (+3.6 %) |
| etcd lease | 3.32 / 3.34 | — | 3.34 / 3.33 | 3.35 / 3.34 | 22.7 → 20.8 MB (+2.2 %) |
| etcd adt, wait, v2store, clientv3 | ≤ 0.14 | — | ≤ 0.16 | ≤ 0.22 | +0.8 to +2.5 % |
| prom promql | 8.42 / 8.46 | 12.66 / 12.42 | 35.62 / 34.93 | 47.62 / 46.66 | 37.6 → 35.6 MB (+6.3 %) |
| prom chunkenc | 0.18 / 0.18 | 0.30 / 0.30 | 0.43 / 0.43 | 0.51 / 0.50 | +4.6 % |
| prom storage, labels, relabel | ≤ 0.33 | — | ≤ 0.47 | ≤ 0.45 | +0.7 to +6.1 % |

Notes on the table:
- Instrumented binaries are 4–10 % *smaller* than the default builds, because `-l` removes inlining.
- With the `runtime.Callers`-only runtime, mvcc took 20.29 s and promql did not finish in 600 s.
- Variance between runs: ≤ 1.0 s (promql 47.62 vs 46.66 s; mvcc base 10.66 vs 11.38 s in an earlier session, `$W/measure/etcd.tsv`). Packages under 0.25 s vary up to 5× (adt 0.02 vs 0.11 s).

### 4.2 Observations (union of 2 runs per package; `$W/runs/{etcd,prom}-final.{obs.jsonl,report.txt}`)

| | etcd | Prometheus |
|---|---|---|
| Observed sites / all sites | 5,033 / 42,195 (11.9 %) | 14,994 / 114,196 (13.1 %) |
| … with an instrumented callee (of which `go`) | 2,884 (37) | 8,844 (10) |
| … executed but entering only uninstrumented code | 2,149 | 6,150 |
| Site → callee pairs; sites with >1 callee | 3,026; 84 | 10,401; 694 |
| Mixed sites (`exec > hits` and ≥1 instrumented callee) | 1 | 3 |
| Defer groups (single-defer) | 109 (60) | 71 (56) |
| Sites with a callee that carry a counter | 79.7 % | 69.7 % |
| Unattributed hits from instrumented files | 0 | 899,778 `range-implicit` |
| Runtime names = predicted | 1,147 / 1,147 | 2,887 / 2,887 |

Every multi-target site I sampled (14 of 84 in etcd, 8 of 694 in Prometheus) is genuine dynamic dispatch:
- `Comparable.Compare` across 3 types;
- `backend` vs `fakeBackend`;
- option funcs, `lexer.state` (14 targets), `Expr.Type` (6).

The mixed sites are func values reaching `math.*`, `io.Writer` with `*os.File`, and `error` values from stdlib types.

### 4.3 Failures caused by instrumentation, and fixes

| Failure | Cause | Fix |
|---|---|---|
| `go.work lists go 1.26` refuses the rt module | rt's go.mod said `go 1.27` | declare `go 1.21` |
| promql FAIL at the 10-min test timeout; its records were lost (no exit hook on a fatal panic) | 340 ns `runtime.Callers` on every function entry | frame-pointer fast path with per-pair verification |
| `missing return` in 5 etcd packages | counter placed after `panic(...)`, which breaks the terminating-statement rule | no counter after `panic` |
| Fast path disagreed on 100 % of pairs (caught by the verification, so no wrong data) | calibration ran in another function's frame | calibrate inside `E` |
| Possible lost hit while a new pair is being verified (found by code review, never observed) | first-sight race; a lost hit could fake `exec > hits` | pending bit (`$W/oracle/rt/rt.go`); probe3 observations unchanged afterwards |

No test failed because of tags. Runs: etcd 6 packages × (2 default + 1 tag-all + 2 final + 1 slow path); Prometheus 5 × (2 + 1 + 2).

### 4.4 Validation and flakiness

- **Fast vs slow path:** identical on probe3 and on etcd (3,185 pairs, 0 differences; `$W/runs/etcd-slow.obs.jsonl`).
- **Tag `all` vs `needed`:** identical pair sets on 2-run unions (etcd 3,184, Prometheus 10,489).
- **Run to run:** 1 to 6 pairs differ, all on randomized paths. For etcd `adt` this is verified (the tests use `rand.Intn`). For Prometheus `postings.go:790` I did not check.
- **Test flakiness:** none. Every uninstrumented and every final instrumented run passed.
- **Naive-resolver demo** (`$W/runs/{etcd,prom}-naive-match.txt`):
  - etcd: 876 CONFIRMED, 180 CONFIRMED-NOEXEC, 3,462 UNEXERCISED.
  - Prometheus: 1,292 CONFIRMED, 707 CONFIRMED-NOEXEC, 10,694 UNEXERCISED, 78 WRONG-OTHER. All 78 are calls to local closures `histogramSample`/`floatHistogramSample` that shadow package functions in `storage/merge_test.go`.

## 5. Output format, matching rule, names

### 5.1 Records (JSON lines)

| File | Kind | Key fields |
|---|---|---|
| `COPY.sites.jsonl` (instrument) | `file` | `file` (relative), `abs`, `pkg`, `status`, counts |
| | `func` | `fid`, `pkg`, `qname`, `rtname`, `file`, `line`/`col` (name identifier, or `func` keyword for literals), `end_line`, `lit`, `parent`, `hooked`, `has_defer`, `exit_lines`, `defers` |
| | `site` | `sid`, `file`, `line`/`col` (callee token, 1-based **byte** column), `name`, `form` (ident, selector, index, funclit, callresult), `lparen_line`/`lparen_col`, `fid`, `stmt` (call, go, defer), `go_line`, `tagged`, `hot` |
| | `hot`, `stmt` | implicit-call lines; counter id → `sites[]` |
| `hits-<pid>.jsonl` (rt) | `meta` / `call` / `go` / `done` | `call`: `fid`, `callee` (runtime name), `caller`, `file` (may carry `#sid`), `line`, `pc`, `n`; `go`: creator and `go` line; `done`: counter id, `n` |
| `obs.jsonl` (report) | `site` | site key + `via`, `exec` (−1 = no counter), `hits`, `callees[{fid, qname, rtname, file, line, n}]` |
| | `defer-group` | `fid`, `qname`, `defer_sites`, `callees` |

Example of a `call` record (etcd): `{"kind":"call","fid":3610,"callee":"go.etcd.io/etcd/pkg/v3/adt.(*intervalTree).Insert","caller":"…adt.TestIntervalTreeDelete","file":"…/pkg/adt/interval_tree_test.go#14752","line":181,…,"n":1}`.

### 5.2 Matching a resolver record (`match`)

- **Input:** `{file, line, col, name, target:{file, line} | {qname}, kind?}`.
- **Site lookup:** same file, line and name; the column breaks ties.
  - CodeDoc keeps 0-based UTF-16 columns (codedoc_python convention). Convert to 1-based byte columns at the boundary.
- **Target lookup:** by declaration position (preferred; gopls returns the name identifier) or by `qname` with `[...]` dropped.

| Verdict | Rule |
|---|---|
| WRONG-OTHER | an instrumented callee ≠ target and the target was never entered; or no instrumented callee but `exec > 0` and the target is hooked |
| WRONG-MULTI | the target was entered, and also another instrumented callee, or `exec > hits` (something uninstrumented ran too) |
| CONFIRMED | callees = {target} and `exec == hits` |
| CONFIRMED-NOEXEC | callees = {target}, but the site has no counter (return, `if`/`for`/`switch` header, `go`/`defer`): uninstrumented entries cannot be ruled out |
| UNEXERCISED | no entry and no completion; such a site says nothing |
| UNVERIFIABLE-TARGET | the target has no hook (assembly, `nosplit`, skipped file) |
| DEFER-GROUP-CONSISTENT / -OTHER | defer site in a function with ≥2 defers: only the group set is known |
| IFACE-CONSISTENT / -OTHER | target is an interface method: every instrumented callee must be a method of that name |

### 5.3 Runtime names → qualified names (probe: 4,078 functions, 0 mismatches except row `%2e`; source: `ir/func.go` `closureName`, `noder/reader.go` `-fm`)

| Runtime `Frame.Function` | Declaration | Mapping and loss |
|---|---|---|
| `p.F`, `p.T.M`, `p.(*T).M` | func, value-receiver method, pointer-receiver method | direct |
| `p.F[...]`, `p.T[...].M`, `p.(*T[...]).M` | generic declarations | drop `[...]`; **lossy**: instantiations collapse (CodeDoc nodes do not distinguish them either) |
| `p.F.func1`, `p.F.func1.2`, `p.(*T).M.func1` | closures, numbered in source order, nested ones within their parent | **lossy**: numbers shift when code changes; closures inside inlined calls get hash suffixes (only with inlining). The oracle uses fid → position instead |
| `p.init.func1` / `p.init.0` / `p.init` | package-level closure / `init` functions in file order (package files sorted, then `_test.go`) / compiler package initializer | the initializer has no declaration; package-level sites have `fid = -1` |
| `p.F-range1` | range-over-func body | no declaration; its calls attribute by line |
| `p.F.gowrap1`, `p.F.deferwrap1`, `p.(*T).M-fm`, autogenerated `(*T).M` | compiler wrappers | never reported: elided by `runtime.Callers` |
| `main.F`, `p_test.F` | package main, external test package | the import path is replaced by `main`; `_test` suffix |
| `example.com/a/v1%2ex.F` | dot in the last path element | unescape `%2e` (probe3) |

## 6. Limits

| Limit | Effect | False WRONG? | False CONFIRMED? |
|---|---|---|---|
| Inlining | With `-l` none. Without `-l`, `runtime.Callers` still reports logical frames, and the fast path flags inlined callees through verification (probe3 identical with inlining on) | No | No |
| Compiler wrappers (pointer-receiver, `-fm`, interface, `deferwrap`/`gowrap`, reflect `callN`) | Elided by `runtime.Callers` (source `runtime/traceback.go` `tracebackPCs`); attributed to the real site | No (a wrapper that is not elided would only lose a hit) | No |
| Assembly functions, cgo | No hook: `exec > hits` at the site; an edge whose target is asm is UNVERIFIABLE-TARGET | No (target check) | No |
| Runtime callbacks (`sort.Slice` less, http handlers, `t.Run`, bbolt `ForEach`) | Caller is the external function, so never attributed to the passing site | No | No |
| Deferred calls | Group-level when a function has ≥2 defers; calls during panicking come from `runtime.gopanic` and are unattributed | No | Only at group level (DEFER-GROUP-CONSISTENT is not proof of a single target) |
| Sites without counters (20–30 % of observed sites) | A site that also enters stdlib or dependency code can look single-target | No | **Yes**: reported as CONFIRMED-NOEXEC, never as CONFIRMED |
| Unknown implicit-call positions (`defer-resume` was found late) | In mode `needed` they could be attributed to an untagged site | **Yes in `needed`**; impossible in `all` | No |
| "created by" parsing, exithook linkname, frame layout | Tied to Go 1.27.2; a format change loses go-site hits; a crash, timeout or fatal signal loses the whole process unless snapshots are on (the Go coverage FAQ has the same limit) | No | No |
| Hash table (2²⁰ keys, 23-bit fid, 2²² counters) | Overflow falls back to a locked map; the instrumenter refuses larger trees | No | No |
| Fast path on non-amd64 | Falls back to the slow path, ~35× costlier per hook | No | No |
| Tests that read their own caller file names | Tags change `Frame.File` (lines stay true); 0 of 11 packages broke | — | — |

## 7. Recommendations for the `codedoc_go` spec

1. **Ship `goracle` as the runtime oracle,** for corpus ground truth and for the etcd and Prometheus precision check.
   - Stdlib-only Go: rewriter, `rt` package, report and match.
   - Run with `-gcflags=all=-l -ldflags=-checklinkname=0 -vet=off` and tag mode `all`. Pin Go 1.27.2.
2. **Precision-check verdicts.**
   - WRONG = WRONG-OTHER or WRONG-MULTI, including `exec > hits`.
   - The "0 wrong" claim should report CONFIRMED and CONFIRMED-NOEXEC separately, and keep defer groups and interface-method edges in their own rows.
3. **Corpus use (a).**
   - Map each case's snippet and token to a site key `(file, line, byte col, name)`.
   - Run the corpus demo and tests under the oracle.
   - Require `observed ⊆ targets ∪ runtime_targets`, with every `runtime_target` observed. This is the codedoc_python rule.
   - This needs a small `judge` mode next to `match`; it is not built yet.
4. **Optional later work.**
   - A type-aware rewriter (`go/types` with `go list -export` export data, still stdlib) could add counters to return statements and headers, and shrink CONFIRMED-NOEXEC.
   - Per-P sharded counters would cut promql's counter cost (35 s → 47 s).

## 8. Open risks / not verified

- **eBPF:** not tried (no tools, no root installs). The claims about Go uretprobes are not verified here.
- **Prometheus randomness:** `postings.go:790` run-to-run difference not traced.
- **Wrapper kinds:** no wrapper was ever seen as a caller in the forms probed (probe0, probe2, probe5, and 4,034 functions on real code). A wrapper kind that is *not* elided would make a site's hits go missing, and then `exec > hits` would give a false WRONG. Not every wrapper kind the compiler can emit has been enumerated.
- **Coverage of the trial:** only ~12–13 % of sites run under the chosen packages. Wider coverage needs more packages; their cost was not measured.
- **Name-ordering assumptions:** `init.N` and closure numbering in mixed-test-file packages, and fid-based identity across re-instrumentation, rest on deterministic output. That was checked for etcd (byte-identical site table on re-instrumenting), not across tool versions.

## Evidence index

- **Probes:**
  - `$W/probe0/out-noinline.txt`: frames per construct.
  - `$W/probe1/out.txt`: call position, defer exit lines, tags, created-by.
  - `$W/probe2/out-{noinline,inline}.txt`: generic wrappers, workspace import, absolute tags.
  - `$W/probe3/src`: construct corpus.
  - `$W/probe4/out.txt`: defers after recover.
  - `$W/probe5/out.txt`: interface method expressions and embedded-interface wrappers.
  - `$W/bench/out.txt`: hook cost.
- **Mechanisms:** `$W/mech/` (coverage profile, delve traces, pprof traces and comparison).
- **Runs:** `$W/runs/` (site tables, hits, obs, reports, match outputs for probe3, etcd and Prometheus in modes v1/v2/all/final/slow).
- **Measurements:** `$W/measure/*.tsv`, `stats-final.txt`, `base-l-runs.txt`.

## Sources (accessed 2026-10-13)

- runtime `Callers`, `CallersFrames`, `Frame` (no column field), `Stack`: https://pkg.go.dev/runtime
- Line directives and the `-l` flag: https://pkg.go.dev/cmd/compile
- Linkname restriction and `-checklinkname=0`; range-over-func: https://go.dev/doc/go1.23
- Terminating statements (`panic`), order of evaluation, `&&`/`||`: https://go.dev/ref/spec
- Coverage data written at exit, lost on panic: https://go.dev/doc/build-cover
- runtime/trace events: https://pkg.go.dev/runtime/trace
- `dlv trace`, `--ebpf` (experimental): https://github.com/go-delve/delve/blob/master/Documentation/usage/dlv_trace.md
- Toolchain source read hands-on (Go 1.27.2): `runtime/traceback.go`, `runtime/stack.go`, `runtime/pprof/pprof.go`, `internal/runtime/exithook/hooks.go`, `cmd/compile/internal/typecheck/stmt.go`, `cmd/compile/internal/ir/func.go`, `cmd/compile/internal/noder/reader.go`, `cmd/internal/obj/x86/obj6.go`, `cmd/link/internal/loader/loader.go`
