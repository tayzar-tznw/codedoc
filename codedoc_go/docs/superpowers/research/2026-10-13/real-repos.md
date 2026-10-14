# Track `real-repos`: gopls (A) vs a go/types helper (B) on etcd v3.7.2 and Prometheus v3.15.0

**Date:** 2026-10-13. **Raw data:** `$W` = `/home/admin/scratch/codedoc-go-research/real-repos` (index in §10).
**Evidence tags:** [probe] = measured here, path given; [doc] = cited source, accessed 2026-10-13; [inference] = reasoning from the two.

---

## 0. Verdict

**Use B: one `go/packages` + `go/types` load per repo. Do not put gopls in the pipeline.**

- **Same answers.** A and B were compared site by site on all 187,431 call sites (72,623 etcd, 114,808 Prometheus). Wherever both name a target, they agree. In no site is A empty while B has a target, and in no site do they differ. The 422 sites where only A answers are all callee-token artifacts of the probe (§5.3) [probe].
- **10–20× faster.** One full B run takes 2.3 s (etcd) and 5.7 s (Prometheus); that includes the interface-implementer search. gopls needs 40–45 s and 60–65 s for `definition` alone, sent sequentially as the siblings do [probe].
- **Less memory.** B peaks at 1.5–1.7 GB (etcd) and 4.7–5.3 GB (Prometheus). gopls peaks at 4.7–5.5 GB and 4.9–7.2 GB, and reaches 12 GB after 400 `implementation` requests [probe].
- **Type facts.** B exposes what the zero-wrong-edge gates need: the receiver's static type, the embedding path and depth, `Var` kinds, instantiations, and invalid types. A returns only positions.
- **Offline risk, and why it favours B.** Offline, answers can be wrong in both engines, because the cause is shared (go/packages and go/types). Neither repo produced a changed answer, but a 20-line probe produced a wrong in-repo edge in both engines. Only B can detect and gate these cases (§6).

---

## 1. Setup

| Item | Value | Evidence |
|---|---|---|
| Go toolchain | go1.27.2 linux/amd64 (shared, GOTOOLCHAIN=local) | `go version` [probe] |
| gopls | v0.23.0, the latest release (2026-07-07). Only pre-releases exist after it (v0.24.0-pre.1/.2). It is built on x/tools v0.47.1-0.20260707181000-a299dadba899 | https://proxy.golang.org/golang.org/x/tools/gopls/@latest, `.../@v/list` [doc]; `go version -m gopls` [probe] |
| golang.org/x/tools for B | v0.52.0 (2026-10-10T19:29:17Z), the latest | https://proxy.golang.org/golang.org/x/tools/@latest [doc]; `$W/helper/go.mod` |
| Min Go to build x/tools v0.52.0 or gopls v0.23.0 | 1.26.0 (`go` directive) | `go.mod` of both in the module cache [probe] |
| Repos | Copies of the shared clones (without `.git`) in `$W/repos`, made at 19:36. `go.sum` was never changed.<br>etcd ships its `go.work.sum`; it stayed unchanged.<br>Prometheus v3.15.0 ships **no** `go.work.sum` (gitignored, `.gitignore:29`). In my copy, `go mod download` created an 873-line one at 19:40, before any other go command ran there. All later runs used that copy (§3).<br>**I wrote nothing into the shared clones.** There I ran only reads: `git log -1` at 19:35; `git ls-files` and `git check-ignore` at 20:30; `ls`, `find`, `grep`, `tar -c` and `diff`. All go commands and gopls ran in copies without `.git`, and no parent directory is a git repo. The clones' `.git/index` files were rewritten at 19:57:21 and 19:57:36, by someone else. The `go.work.sum` in the Prometheus clone dated 20:19 also came from another agent's session | `sha256sum -c $W/logs/*-sums-before.txt`, `diff -rq` against the shared clones, `git ls-files`, `stat` [probe] |
| Host | 32 cores, shared with other agents. Every timing below was taken at least twice | |

---

## 2. Corpus facts

| | etcd v3.7.2 | Prometheus v3.15.0 |
|---|---|---|
| `.go` files | 1,098 regular files and 11 symlinks | 731 |
| Loaded (linux/amd64, `CGO_ENABLED=1`, tests included) | 1,080, of which 415 `_test.go`. The 11 symlinks are loaded at their link paths | 694, of which 274 `_test.go` |
| Excluded by the build configuration | 29 files with 221 call sites. With `CGO_ENABLED=0`: 33 | 37 files with 727 call sites |
| Workspace modules (`go.work`) | 13, all with packages | 5. 4 have packages; `internal/tools` holds only a `tools`-tagged file |
| Modules in the load closure | 104 | 254 |
| `go list work` packages | 183 | 121 |
| In-repo package paths, then with test variants | 193, then 279 | 126, then 223 |
| Packages loaded, deps and test variants included | 1,067 | 1,885 |
| Call sites (`*ast.CallExpr` in loaded in-repo files) | **72,623**, of which 39,541 in tests | **114,808**, of which 74,990 in tests |

Sources: `$W/out/{etcd,prom}-dl-f4/summary.json` and `$W/buildcfg/*` [probe].

---

## 3. Modules

| | etcd | Prometheus |
|---|---|---|
| `go mod download` at the workspace root | 741 zips, 717 MB of zips, 3.5 GB extracted. 19.6 / 12.6 / 16.1 s | 746 zips, 542 MB, 2.7 GB. 20.8 / 10.1 / 16.8 s |
| **`go list -e -deps -test work`** (lazy) | **95 zips, 62 MB, 293 MB. 3.6 / 4.5 / 3.1 s** | **257 zips, 291 MB, 1.7 GB. 9.1 / 12.3 / 10.0 s** |
| Offline check of the lazy cache (GOPROXY=off) | All 1,067 packages load with no errors | All 1,885 packages load with no errors, but only because my copy already had a `go.work.sum` (see below) |

- **The right command.** `go list -buildvcs=false -e -deps -test work`, run with the download cache's `GOMODCACHE`, fetches exactly what the load needs [probe].
  - `go mod download` takes every module that any workspace go.mod requires: 8× more zips for etcd, because of `tools/mod`. The local help text describes this: "the modules explicitly required by the main module" (`go help mod download`, Go 1.27.2) [doc].
  - Whatever command is used, packages.Load itself downloads lazily when GOPROXY is on.
- **Why `-buildvcs=false`.** In a copy that includes `.git` (`$W/vcsprobe`), some go commands rewrite `.git/index`, because they run `git status` to stamp VCS info [probe]. The copy was freshly extracted, so any `git status` refresh rewrites the index. In a long-lived checkout, git rewrites it only when stat data is stale.

  | Command | `.git/index` |
  |---|---|
  | `go list -e -deps -test work` | Rewritten |
  | `go list -e -json work` | Rewritten |
  | `go build ./etcdctl` | Rewritten |
  | `go list -buildvcs=false -e -deps -test work` | Untouched |
  | `go list` with a field-limited `-json=…`, as go/packages calls it | Untouched |
  | The helper (packages.Load), source mode and export-data mode | Untouched |
  | A gopls session with `definition` requests | Untouched |

- **The lazy `go list` and `go mod download` both write into the repo.** In workspace mode they write `go.work.sum` next to `go.work`, even with `-buildvcs=false`.
  - In a pristine Prometheus copy, the networked lazy `go list` created a 240-line `go.work.sum`, and `go mod download` an 873-line one (`$W/wsum2`) [probe].
  - This confirms the go-modules track's report. My first test of this (`$W/wsum`) was confounded by a stray `go.work.sum` in the shared clone.
- **`GOPROXY=off` alone is not offline.** Test: a pristine Prometheus copy with the full module cache.
  - With network reachable, go still asked sum.golang.org for missing checksums and wrote `go.work.sum`.
  - With the network blocked (an unreachable `HTTPS_PROXY`), **518 of 1,885 packages failed**. The missing checksums belong to modules that only a sub-module requires, for example `compliance` → `client_golang/exp@v0.0.0-20260820…`.
  - **Cause, found by the go-modules track (`reports/go-modules.md` §7.3).** Workspace mode needs a `/go.mod` hash for every version in the combined graph. `compliance/go.sum:16` holds only that version's zip hash, with no `/go.mod` line, and `go.work.sum` is gitignored [probe: grep].
  - `GOSUMDB=off` fixes the load. `-mod=mod` is rejected in workspace mode [probe].
- **An offline setup that leaves the repo untouched.** `GOWORK` points at a generated `go.work` outside the repo, with absolute `use` paths, plus `GOSUMDB=off GOPROXY=off`, network blocked. All 1,885 packages loaded with 0 errors. The helper's 114,808 answers were identical to the download-mode run (0 differing). `go.work.sum` was written next to the generated `go.work` (`$W/wsum2/genwork`, `$W/wsum2/out-p6`) [probe].
- **My offline runs hold up with the network blocked.** I re-ran B's offline runs (empty cache) with the network blocked and `GOSUMDB=off`; `sites.jsonl` came out byte-identical (`$W/out/*-off-netblocked`).
- **Raw logs.** `$W/logs/download-run2.txt` and `download-run3.txt`. Run 1 was not logged to disk. The caches are in `$W/dl/*`.
- **Caveat on the Prometheus timings.** All Prometheus download timings except `go mod download` run 1 ran in the copy that already had a `go.work.sum`; run 1 is the one that created it. On a pristine checkout, the lazy command also fetches checksums from sum.golang.org and writes a 240-line `go.work.sum`. Measured there it took 17.4 / 9.7 s; afterwards all 1,885 packages loaded offline (`$W/logs/download-pristine.txt`).
- **Shared cache.** The shared cache was filled by the same lazy command: 5.9 s for etcd and 8.9 s for Prometheus.
- **Offline mode.** `GOMODCACHE=$W/gomodcache-empty GOPROXY=off`.
  - go list does not fail. Every third-party package becomes an error stub: 322 of 740 packages for etcd and 416 of 776 for Prometheus.
  - Stdlib and all workspace packages load [probe].

---

## 4. Approach B: the helper (`$W/helper`, about 1,200 lines of Go)

**Load.**
- **Call.** `packages.Load` with `NeedName|NeedFiles|NeedSyntax|NeedTypes|NeedTypesInfo|NeedDeps|NeedImports|NeedModule`, `Tests: true`, and the pattern **`work`**.
- **What `work` is.** The pattern matches "the set of workspace modules in workspace mode" (Go 1.25 release notes, https://go.dev/doc/go1.25; also `go help packages`) [doc].
- **Why not `./...` from the root.** It lists only the root module: 13 of the 183 packages [probe].
- **Why not `./...` per module.** Thirteen loads gave the identical answer set (72,623 sites, 0 differing). They took 13.6 s instead of 2.2 s and type-checked shared in-repo packages up to 13 times (161,859 site visits) [probe].

**Per site the helper records:**
- the file, line, byte column, UTF-16 column and offset of the callee token;
- the shape and class;
- the receiver kind, the embedding depth and generic instantiation;
- `go` or `defer`;
- the target object and its position.

It removes duplicates across package variants, keyed by `(file, Lparen offset)`. No variant ever gave a different answer for a site: 0 conflicts [probe].

### 4.1 Time and memory, wall clock of the whole run (`$W/logs/B-timings.txt`)

| Mode | etcd | Prometheus |
|---|---|---|
| Download mode, warm `GOCACHE` | 2.33 / 2.26 s (load 1.5 / 1.4 s); 1.55 / 1.54 GB | 5.74 / 5.66 s (load 4.1 / 4.1 s); 4.77 / 5.17 GB |
| Download mode, empty `GOCACHE` | 2.82 / 2.83 s; 1.66 / 1.57 GB | 7.15 / 6.97 s; 5.18 / 5.30 GB |
| Offline (empty module cache) | 1.62 / 1.61 s; 0.77 / 0.76 GB | 2.16 / 2.19 s; 1.05 / 0.99 GB |
| Variant without `NeedDeps` (deps from export data), first run | 6.9 s. Compiles deps; 104 CPU-s | 52.4 s. 902 CPU-s; the `go build` tree peaked at 6.3 GB |
| Same variant, warm build cache | 1.55 s; 0.46 GB | 2.81 s; 0.94 GB |

**What the export-data variant gives up.**
- **Unchanged:** answers for every in-repo target, and every in-repo implementer set.
- **Coarser:** external targets carry only a line (column 1). Stdlib paths come back as `$GOROOT/src/...`.
- **Lost:** unexported implementers in dependencies are invisible. For 4 interfaces (etcd) and 16 (Prometheus), the non-repo implementer count falls to 0, for example `go.etcd.io/raft/v3.Node` and `prometheus.Counter`.
- **Consequence:** the open-world check of §4.3 needs the `NeedDeps` mode [probe, `$W/out/*-exp-1`].

**Other measured runs.** Earlier repeats ran at 2.10–2.36 s and 5.58–5.9 s, consistent with the table (`$W/out/*-dl-f*`). Peak RSS is the largest process in the run (getrusage), which is the helper. `go list -deps -test work` alone peaked at 88–158 MB in §3.

### 4.2 Call sites by class, download mode (`$W/out/*-dl-f4/summary.json`)

| Class | etcd | Prometheus |
|---|---|---|
| Builtin (`len`, `append`, `unsafe.Sizeof`, …) | 5,804 | 8,966 |
| Conversion to a named type (same module / other repo module / stdlib / third-party) | 927 (150 / 294 / 148 / 335) | 4,801 (606 / 0 / 311 / 3,884) |
| Conversion to a universe type / to a type literal (`[]byte(x)`, `(*T)(x)`) | 2,300 / 2,428 | 5,253 / 633 |
| Package-level func, same module | 11,193 | 21,037 |
| Package-level func, other repo module | 2,216 | 5 |
| Package-level func, stdlib / third-party | 7,947 / 9,071 | 11,133 / 18,867 |
| Concrete method (same module / other repo module / stdlib / third-party) | 6,395 / 1,850 / 8,150 / 4,367 | 16,756 / 15 / 8,584 / 4,564 |
| Concrete method: pointer / value receiver | 18,697 / 2,065 | 24,924 / 4,995 |
| Concrete method: promoted at depth 1 / 2 / 3 | 4,892 / 22 / 3 | 2,366 / 171 / 0 |
| Interface method on an in-repo interface (same module / other module) | 3,865 / 2,124 | 6,999 / 0 |
| Interface method on a stdlib / third-party interface | 892 / 795 | 981 / 1,081 |
| Interface method on `error` / on an anonymous interface | 252 / 8 | 219 / 1 |
| Interface method reached through an embedded interface field of a struct | 1,076 | 47 |
| Method on a type parameter (through its constraint) | 0 | 4 |
| Method expression called (`T.m(x)`) | 0 | 0 |
| Func-typed local / param / package var / field (dynamic) | 849 / 223 / 162 / 159 | 3,550 / 289 / 2 / 505 |
| Callee is a call result / an index expression | 2 / 6 | 3 / 2 |
| Func literal called directly | 638 | 558 |
| Generic: explicit instantiation / implicit instantiation / method of a generic type (these overlap the rows above) | 8 / 81 / 78 | 162 / 364 / 581 |
| **Unknown (type error)** | **0** | **0** |
| `go` statements, non-test / test | 202 / 246 | 87 / 180 |
| `defer` statements, non-test / test | 861 / 1,709 | 537 / 656 |
| Functions / method values used as values, not called (callbacks) | 535 / 39 | 423 / 386 |

Only 2 sites, both in Prometheus, have a UTF-16 column different from the byte column [probe].

### 4.3 Candidate implementers at interface-method sites (`$W/out/*-dl-f4/iface_stats.json`, `ifaces.jsonl`)

**Method.**
- **Candidates.** All package-level named types of the loaded program, removed when they duplicate across variants. They are pre-filtered by method-name index, then checked with `types.Implements` on both T and *T.
- **Universe alignment for test variants.** A signature-string fallback exists for this case; it was never needed (0 uses).
- **Dispatch type.** The candidates come from the **receiver's static type**. Where the method comes through an embedded field, the embedded field's interface is used.
- **Generic types.** Uninstantiated generic types are counted as undecided: 5 interfaces in etcd, 15 in Prometheus.
- **Cost.** 0.1 s (etcd) and 0.45 s (Prometheus) over 8,150 and 45,146 named types.

| Per site | etcd (7,936 sites, 250 interfaces) | Prometheus (9,281 sites, 230 interfaces) |
|---|---|---|
| In-repo implementers, test types included: 0 / 1 / 2–5 / >5 | 843 / 1,367 / 3,963 / 1,763 | 924 / 731 / 1,960 / 5,666 |
| In-repo implementers, test-only types excluded: 0 / 1 / 2–5 / >5 | 855 / 2,034 / 3,756 / 1,291 | 935 / 1,247 / 1,599 / 5,500 |
| Stdlib implementers: 0 / 1 / 2–5 / >5 | 6,643 / 28 / 506 / 759 | 8,037 / 35 / 581 / 628 |
| Third-party implementers: 0 / 1 / 2–5 / >5 | 6,317 / 516 / 613 / 490 | 7,190 / 254 / 757 / 1,080 |
| Sites whose interface some non-repo type satisfies | 1,829 | 2,357 |
| **In-repo interfaces with sites**: exactly one in-repo implementer | 38 of 156 | 20 of 145 |
| … of which a non-repo type also satisfies the interface | **2** (3 counting non-test types only) | **4** (9 counting non-test types only) |
| … whose only in-repo implementers are test-only | 5 | 3 |

**Example.** `etcd/client/pkg/v3/testutil.TB` has one in-repo implementer and 49 call sites. Four stdlib types also satisfy it, including `*testing.T`, which is the runtime type in tests. A closed-world "single implementer" upgrade (the Python spec's U1) would produce a wrong edge here.

### 4.4 Optional: CHA and VTA (etcd, all non-test workspace packages; `$W/cg`, `$W/cmp/etcd-cg-compare.json`)

**Cost.** Load 1.3 s, SSA 0.44 s, CHA 0.84–0.90 s, VTA 3.4–3.5 s. 6.5–6.6 s in total, peak 2.4–2.6 GB, over two runs.

| Sites | In-repo callees: 0 / 1 / 2–5 / >5 |
|---|---|
| helper (static `types.Implements`, non-test declarations) | 842 / 1,219 / 1,393 / 51 |
| CHA | 831 / 1,079 / 1,448 / 147 |
| VTA | 1,259 / 1,570 / 671 / 5 |

- **Sample.** 3,505 invoke sites matched between the SSA and the helper.
- **Order between the analyses.**
  - VTA ≤ CHA on every site.
  - VTA < helper on 1,154 sites.
  - VTA has no callee at all on 200 sites. These are values whose flow VTA does not see, so it is unsound for those entry points.
- **The 76 sites with VTA > helper.** CHA and VTA count the promotion wrapper of a type that embeds an interface, for example `etcdserver.hasherAdapter{mvcc.HashStorage}`. The helper counts that as a second dispatch instead.
- **Use.** VTA could prune `PossiblyCalls` candidates. It cannot justify a `MethodCalls` edge.

---

## 5. Approach A: gopls v0.23.0 (`$W/gopls/driver.py`, forked from `codedoc_python/graph_generator/lsp_client.py`)

**Session.**
- `initialize` with the repo root as `rootUri` and as the workspace folder, `window.workDoneProgress`, and `positionEncodings: ["utf-16"]`. gopls returned no `positionEncoding`, so UTF-16 applies.
- **Readiness.** The `$/progress` end of "Setting up workspace", whose message is "Finished loading packages.". This is a metadata load only; packages are type-checked on first use.
- **Per file.** `didOpen`, then all `definition` requests (one in flight, as the siblings do), then `didClose`.
- **Cache.** gopls keeps a persistent cache at `~/.cache/gopls` unless `GOPLSCACHE` is set (`gopls@v0.23.0/internal/filecache/filecache.go:412`) [probe: source].
  - The directory already existed, created at 19:28 before any run of mine.
  - These runs used it, so they also wrote into it, which breaks the "write only under `$W`" rule: the probe, `*-dl-full-1/2`, `etcd-dl-full-c8`, `*-off-full-1` and `*-excluded`.
  - The cold and warm series, `*-off-full-2` and the shadow probe set `GOPLSCACHE` under `$W`.

### 5.1 Time and memory (`$W/gout/*/run.json`, `$W/logs/*.log`)

| Run | etcd (72,623 requests) | Prometheus (114,808 requests) |
|---|---|---|
| Shared cache, run 1 / run 2: readiness | 0.70 / 0.72 s | 0.94 / 1.04 s |
| Shared cache, run 1 / run 2: definitions | 39.7 / 43.7 s (1,831 / 1,662 req/s) | 63.6 / 63.0 s (1,805 / 1,824 req/s) |
| Private empty caches, run 1 / run 2: readiness and definitions | 1.49 / 1.28 s; 43.7 / 41.1 s | 2.35 / 2.31 s; 62.9 / 62.8 s |
| Same caches, now warm: readiness and definitions | 0.72 / 0.69 s; 40.5 / 39.9 s | 0.93 / 0.91 s; 59.5 / 59.9 s |
| Peak RSS (VmHWM) after the definitions | 4.7–5.5 GB (run 1 sampled at file 950 of 986) | 4.9–7.2 GB |
| Concurrency 8 (etcd only) | 30.0 s (2,421 req/s), 5.3 GB | — |
| Latency p50 / p90 / p99 / max | 0.2 / 0.3 / 14.7 / 175 ms | 0.2 / 0.3 / 0.9 / 2,224 ms |
| Timeouts, errors | 0, 0 in every run | 0, 0 in every run |

**Determinism.** Answers were identical across runs, cache states and concurrency levels: 0 differing sites [probe].

### 5.2 Notes on the protocol

- **Builtins and universe types.** `definition` returns a location in `$GOROOT/src/builtin/builtin.go` or `unsafe/unsafe.go`.
- **`err.Error()`.** Returns the `Error() string` line of builtin.go.
- **Interface methods.** Returns the abstract method.
- **Variables.** Returns the variable's declaration.
- **No multi-location answers.** 0 multi-location answers in 187,431 requests.

### 5.3 A against B, per site, download mode (`$W/cmp/{etcd,prom}-dl/compare.json`)

| Category | etcd | Prometheus |
|---|---|---|
| Same target | 69,549 | 113,612 |
| Both without a target (func literals; type-literal conversions such as `[]byte(x)`) | 2,810 | 1,038 |
| **A empty while B has a target** | **0** | **0** |
| **Different target** | **0** | **0** |
| A answers, B has no target | 264 | 158 |

Every class row agrees 100 %: functions, methods, interface methods, type-parameter methods, conversions, builtins and dynamic variables. All 422 "A answers" sites were read (`disagreements.jsonl`). None is a call target. They are artifacts of where the probe placed the callee token for shapes to which B deliberately gives no target:

| Shape | etcd | Prometheus | What gopls returned |
|---|---|---|---|
| `(*T)(x)` conversion; the token is `*` | 256 | 153 | T's type declaration |
| `fs[i](x)` (index callee) | 6 | 2 | The slice or map variable |
| `f(a)(b)` (call-result callee) | 2 | 3 | The import spec or the inner function |

**Conclusion [probe]:** on these repos, gopls adds nothing to go/types and loses nothing.

### 5.4 `implementation` against `types.Implements` (400 random interface-method sites per repo; `$W/cmp/*-dl/impl_compare.json`)

The comparison is by concrete method declaration (file:line:col) for the called method.

| | etcd | Prometheus |
|---|---|---|
| Time for 400 requests | 7.7 s (p50 19 ms, p90 39 ms, max 633 ms) | 15.8 s (p50 3.5 ms, p90 71 ms, max 3.8 s) |
| gopls RSS before → after the requests | 4.7 → 6.75 GB | 6.7 → 12.1 GB |
| In-repo declaration sets equal | 388 | 304 |
| gopls has extra in-repo declarations | 12 | 96 |
| Helper has extra in-repo declarations | **0** | **0** |
| gopls also returns abstract methods of other interfaces | 175 locations at 144 sites | 462 locations at 259 sites |
| Test-file implementations included | Yes: 127 sites (the same 127 in the helper) | Yes: 229 sites (the same 229) |
| Stdlib / third-party declarations (gopls vs helper) | 1,481 vs 1,456 / 1,158 vs 1,119 | 1,942 vs 1,778 / 20,602 vs 19,947 |
| Results in build-excluded (other-GOOS) files | 0 of 4,056 locations | 0 of 27,093 |

**gopls's extras.** gopls uses the interface that *declares* the method, not the receiver's static type. For example, `Commit` at a `storage.Appender` receiver is declared in the embedded `storage.AppenderTransaction`, so gopls also returns the `AppenderV2` implementers. That is an over-approximation.

**The helper's extra stdlib declarations** are all `internal/fuzz` types, which gopls omits.

**Documentation versus behaviour.** The gopls doc says that across packages "only exported package-level types … are reported" (https://go.dev/gopls/features/navigation) [doc]. gopls nevertheless returned unexported types from other packages, for example `(*schema.authBatchTx).UnsafeGetUser` [probe]. The doc also says that type parameters "are treated like wildcards … occasional spurious matches".

**Other GOOS.** gopls creates builds for other GOOS values only when a file of that GOOS is opened (§7). In these sessions no such file was open, and no result pointed into one.

---

## 6. Offline mode (`GOMODCACHE` empty, `GOPROXY=off`)

### 6.1 Recall loss per download-mode class (B: `$W/cmp/*-off/offline_diff.json`; A: `gopls_offline_diff.json`)

| Class | etcd sites | etcd B lost | etcd A lost | Prom sites | Prom B lost | Prom A lost |
|---|---|---|---|---|---|---|
| Func, same module | 11,193 | 133 (1.2 %) | 122 (1.1 %) | 21,037 | 63 (0.3 %) | 218 (1.0 %) |
| Func, other repo module | 2,216 | 37 (1.7 %) | 180 (8.1 %) | 5 | 0 | 0 |
| Func, stdlib | 7,947 | 0 | 460 (5.8 %) | 11,133 | 3 | 682 (6.1 %) |
| Func, third-party | 9,071 | 100 % | 100 % | 18,867 | 100 % | 100 % |
| Method, same module | 6,395 | 45 (0.7 %) | 89 (1.4 %) | 16,756 | 51 (0.3 %) | 373 (2.2 %) |
| Method, other repo module | 1,850 | 68 (3.7 %) | 235 (12.7 %) | 15 | 0 | 0 |
| Method, stdlib | 8,150 | 44 (0.5 %) | 360 (4.4 %) | 8,584 | 182 (2.1 %) | 636 (7.4 %) |
| Method, third-party | 4,367 | 100 % | 100 % | 4,564 | 100 % | 100 % |
| Interface method, in-repo (same and other module) | 5,989 | 48 (0.8 %) | 160 (2.7 %) | 6,999 | 325 (4.6 %) | 117 (1.7 %) |
| Interface method, stdlib / `error` | 1,144 | 38 | 119 | 1,200 | 15 | 124 |
| Interface method, third-party | 795 | 100 % | 100 % | 1,081 | 100 % | 100 % |
| Conversion | 5,655 | 344 (6.1 %) | 361 (6.4 %) | 10,687 | 3,969 (37 %) | 3,922 (37 %) |
| Dynamic (variable callee) | 1,401 | 165 (11.8 %) | 10 (0.7 %) | 4,351 | 4 | 0 |
| **All sites** | 72,623 | **15,157 (20.9 %)** | **16,335 (22.5 %)** | 114,808 | **29,130 (25.4 %)** | **30,590 (26.6 %)** |
| **Changed answers** | | **0** | **0** | | **0** | **0** |

**Reading the table.**
- **Third-party targets.** All are lost in both engines, as expected.
- **Collateral loss** on in-repo and stdlib targets stays under 5 % for B, and is mostly higher for A.
- **Offline gopls returns nothing for whole stretches of some files.**
  - Prometheus: all 1,981 sites where A is empty but B answers lie in files that go/packages attached to stubs in B's load.
  - etcd: 741 of 1,571 such sites lie in attached files.
  - Of the other 830, 622 lie in files that import a stub whose name collides with another import (mechanism 2 in §6.2), for example `server/embed/config.go` and `client/v3/client.go`. There, even `fmt.Errorf`, `os.Exit`, `errors.New` and `wg.Done()` get no answer.
  - gopls's exact cause was not traced (§9).
- **B loses some that A keeps:**
  - in-repo interfaces of `tsdb/chunks` (325 sites);
  - variable callees whose type is invalid (B marks them unknown; A still points to the variable).
- **A against B offline.** Neither ever names a different target: 0 differences (`$W/cmp/*-off-AB/compare.json`).
- **Time and memory offline.** B: 1.6 s and 0.8 GB (etcd), 2.2 s and 1.0 GB (Prometheus). A: 31.7–32.6 s and 2.8–3.1 GB (etcd), 36.8–37.8 s and 2.9–3.0 GB (Prometheus).

### 6.2 How offline answers go wrong: none in these repos, three mechanisms found

1. **go/packages attaches in-repo files to stub packages.**
   - When an import fails, go/packages parses the file name out of the go list error position and adds that file to the *failing* package. This is the "Temporary work-around for golang/go#39986" in `x/tools@v0.52.0/go/packages/golist.go:656-690`, condition at `:726-747`; see https://github.com/golang/go/issues/39986 [doc].
   - Offline: 30 attachments in etcd and 58 in Prometheus. For example, `go.uber.org/zap ← client/pkg/logutil/zap.go` [probe].
   - **Effect without a filter.** Those files are type-checked a second time inside the stub. Before the filter was added, the helper's first offline run had 1,270 sites answered differently by the stub variant (`$W/out/etcd-off-1/summary.json`). For example, `mergePaths` became `go.uber.org/zap.mergePaths`.
   - **Fix in B:** accept a (file, package) pair only if the package's `Module.Main` is true.
2. **Imports collide on a stub's name.**
   - The stub takes the package name of the attached file. `go.uber.org/zap` turned into a package named `logutil`, which collides with the real `logutil` import in the same file.
   - `logutil.CreateDefaultZapLogger` then resolves to an object in the stub. These are 30 sites in etcd and 218 in Prometheus.
   - In all 248 the position was right by accident: the stub holds the very file that declares the target.
   - When two different packages share that name, this mechanism can point to a different declaration [inference].
   - **Fix:** reject any target whose `obj.Pkg()` is not a real workspace or stdlib package.
3. **Embedded-field shadowing changes with an invalid type.**
   - Probe `$W/probes/shadow`: `type Outer struct{ Mid; suite.Suite }`, where `Mid` embeds `Inner`.
   - In download mode `o.SetT` resolves to `suite.Suite.SetT`, a third-party method at depth 1.
   - Offline, `suite.Suite` has an invalid type, so the same selector resolves to the **in-repo `Inner.SetT` at depth 2**.
   - **Both B and gopls return the wrong in-repo answer** (`$W/probes/shadow/out-*`, `$W/gout/shadow-*`) [probe].
   - **Gate (B only):** in a package with import errors, reject a promoted selection if any embedded field at a lower depth of the receiver's embedding tree has an invalid type.

A position-only engine (A) cannot apply gates 1–3; B can.

---

## 7. Build configuration (`$W/buildcfg`)

| | etcd | Prometheus |
|---|---|---|
| `import "C"` (cgo) | None | None |
| Files that depend on `CGO_ENABLED` | 4 (`tests/antithesis/test-template/**`, `//go:build cgo && amd64`): loaded only with `CGO_ENABLED=1` | None. The sets are identical |
| Excluded with linux/amd64, default tags, `CGO_ENABLED=1` | 29 files, 221 call sites | 37 files, 727 call sites |
| Reasons: custom tags | `cluster_proxy` (108 sites), `e2e`, `integration`, `tools`, `libs` | `dedupelabels` and `slicelabels` (510 sites; alternative implementations of `model/labels`), `fuzzing`, `builtinassets`, `forcedirectio`, `tools` |
| Reasons: other GOOS/GOARCH | windows, darwin, solaris, plan9, openbsd, wasm/js, `!linux` | windows, darwin, solaris, aix, js, plan9, openbsd, 386 |

**gopls in excluded files ("zero-config gopls").**
- **Behaviour.** When such a file is opened, gopls answers sites in other-GOOS files and in orphaned single-file directories. Files that only custom tags exclude return the error "no package metadata for file".
- **Counts.** etcd: 93 of 221 sites answered, 128 errors. Prometheus: 208 of 727 answered, 514 errors [probe, `$W/gout/*-excluded`].
- **Documentation.** This matches the gopls v0.15.0 release notes: gopls "adding new builds and GOOS/GOARCH combinations as needed", with `CGO_ENABLED=0`, and is "currently unable to guess build flags that include arbitrary user-defined build constraints" (https://github.com/golang/tools/releases/tag/gopls%2Fv0.15.0) [doc].
- **Check against B.** The windows and darwin answers equal B's when B loads with `GOOS=windows` or `GOOS=darwin`: 64 of 64 positioned targets agree [probe].
- **Consequence.** A silently mixes build configurations. B models exactly the configured ones and can add extra loads per GOOS or tag set on purpose.

**Other loader facts that affect the graph [probe].**
- **Symlinks.** The 11 etcd test files `client/v3/example_*_test.go` are symlinks into `tests/integration/clientv3/...`. Both paths are loaded, in two packages: 533 + 550 sites. gopls reports the link path, as B does.
- **Workspace.** With `GOWORK=off`, Prometheus's `documentation/examples/remote_storage` resolves `github.com/prometheus/prometheus/prompb` to the module-cache copy **v0.308.1**. With `go.work` it resolves to the in-repo directory. Classify targets by file location, never by import path.

---

## 8. Recommendation

| Criterion | A: gopls `definition` per site | B: go/packages + go/types helper |
|---|---|---|
| Exactness (download mode) | Equal to B on 187,431 sites | Equal to A, and gives types for the gates |
| Speed per repo | 40–45 s / 60–65 s, plus 0.7–2.4 s readiness | **2.3 s / 5.7 s**, implementer search included |
| Peak memory | 4.7–5.5 GB / 4.9–7.2 GB; up to 12 GB with `implementation` | **1.5–1.7 GB / 4.7–5.3 GB**; 0.5 / 0.9 GB in export-data mode |
| Offline | Loses 22.5 % / 26.6 %. Blank files under stubs. Cannot detect stub targets or shadowing | Loses 20.9 % / 25.4 %. Can filter attached files and stub targets, and gate invalid embeddings |
| Build tags | Implicit: default build plus ad-hoc other-GOOS builds of opened files | Explicit `GOOS`, `GOARCH`, `-tags`, `CGO_ENABLED`; one load per configuration |
| Interface candidates | `implementation` uses the declaring interface (a superset) and mixes in abstract methods. p50 3.5–19 ms and mean 19–40 ms per site; heavy on memory | `types.Implements` on the static type, test-only flag, non-repo visibility: 0.1–0.5 s per repo |
| Effort | Reuse the sibling LSP client; install gopls; readiness and UTF-16 handling; hover parsing for any type fact | A Go helper of about 1,000–1,500 lines plus a JSONL contract. The Python side drops LSP session management |
| Testability with fakes | Fake LSP server, as in the siblings | Go unit tests on fixture modules; Python tested on recorded `sites.jsonl` (a fake helper is plain data) |

**Hybrid.** Not needed for answers. gopls could serve in `evaluate` as an independent cross-check, nothing more. If one were wanted for dynamic calls, gopls's signature-based `implementation` on the `(` of a dynamic call [doc] could seed `PossiblyCalls`, but this was not measured.

**Design inputs from this track.**
- Run every go command with `GOWORK` set to a generated `go.work` outside the repo, so no `go.work.sum` lands in the repo. Offline, also set `GOSUMDB=off`.
- Pass `-buildvcs=false` to every go command, and to packages.Load's `BuildFlags` as a guard. Plain `go list` rewrites `.git/index` otherwise.
- Run the helper with the user's Go toolchain (build at run time or pin it), because go/types must understand the repo's language version.
- Load once with `work`, `Tests: true`, `NeedDeps`.
- Filter (file, package) pairs to `Module.Main`.
- Reject stub-package targets.
- Gate promoted selections across invalid embedded fields.
- Draw interface candidates from the static receiver type.
- Treat an interface as open-world whenever a non-repo type satisfies it.
- Flag test-only implementers.
- Treat calls through an embedded interface field as a second dispatch.
- Decide how symlinked files are handled (realpath dedupe or link path).
- Honour `go.work`.

---

## 9. Open risks and what was not verified

- **Wider offline cases are untested.** Only an empty module cache was tested: no partial caches and no `vendor/` (neither repo vendors). Changed answers were 0 here, but mechanism 3 is real, and other repos may trigger mechanism 2 with a different position.
- **Cold-disk timings were not measured.** The OS page cache was warm and caches could not be dropped. Other agents were loading the host; spreads are shown as run 1 / run 2.
- **The cause of blank files in offline gopls is inferred.** gopls also uses go/packages and its golist driver, but its code path was not traced. 208 etcd sites that A leaves empty fit neither mechanism.
- **A rule was broken.** Some gopls runs wrote into the shared `~/.cache/gopls` (§5).
  - The default `GOTELEMETRY=local` also makes go, go vet and gopls write counter files to `~/.config/go/telemetry/local`. That setting is local-only, with no upload.
  - Nothing outside `$W` was deleted.
- **A's offline runs had the network reachable,** with `GOPROXY=off` and the default `GOSUMDB`. With an empty cache there is nothing to verify, so no checksum lookup should apply [inference]. Only B was re-run with the network blocked.
- **`implementation` was sampled, not run in full:** 400 sites per repo.
- **CHA and VTA were run on etcd only,** non-test packages.
- **Memory on larger repos is a risk.** `NeedDeps` memory grows with the dependency closure: 5 GB for Prometheus's 254 modules. Kubernetes-size repos may need the export-data mode, which loses non-repo implementer visibility (§4.1).
- **Version skew.** A helper built with an older go/types than the repo's `go` line would report type errors. This was not probed.
- **The precision check against each repo's unit tests** is the runtime-oracle track; it was not done here.

---

## 10. Sources (accessed 2026-10-13) and raw data

**Sources**
- https://proxy.golang.org/golang.org/x/tools/@latest and `/@v/list`; https://proxy.golang.org/golang.org/x/tools/gopls/@latest and `/@v/list`.
- https://go.dev/doc/go1.25 (the `work` pattern; `Var.Kind`).
- `go help packages` and `go help mod download` (Go 1.27.2, local).
- https://github.com/golang/go/issues/39986, together with `gomodcache-shared/golang.org/x/tools@v0.52.0/go/packages/golist.go`.
- https://github.com/golang/tools/releases/tag/gopls%2Fv0.15.0 (zero-config gopls).
- https://go.dev/gopls/features/navigation (Implementation).
- `gomodcache-shared/golang.org/x/tools/gopls@v0.23.0/internal/filecache/filecache.go` (`GOPLSCACHE`).

**Code** (all under `$W`)
- `helper/` (approach B);
- `gopls/driver.py`, `gopls/compare.py`, `gopls/impl_compare.py`, `gopls/lsp_client.py` (approach A and comparisons);
- `bin/measure.py`, `bin/offline_diff.py`, `bin/defs_diff.py`, `bin/iface_stats.py`, `bin/cg_compare.py`, `bin/b_timings.sh`;
- `cg/` (CHA and VTA);
- `buildcfg/` (file lists, `countcalls`, `synsites`);
- `probes/shadow`, `probes/bogus`.

**Outputs** (all under `$W`)
- `out/{etcd,prom}-dl-f4/` (B, download mode);
- `out/{etcd,prom}-off-f5/` (B, offline);
- `out/*-exp-*` (export-data mode);
- `out/etcd-permod/`;
- `gout/*-dl-full-{1,2}`, `*-cold-*`, `*-warm-*`, `etcd-dl-full-c8`, `*-off-full-{1,2}`, `*-excluded`, `shadow-*` (gopls);
- `cmp/*` (all comparisons);
- `logs/B-timings.txt`, `logs/download-run{2,3}.txt`, `logs/*.log`.
