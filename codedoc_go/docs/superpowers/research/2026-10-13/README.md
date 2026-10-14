# codedoc_go research, 2026-10-13

Evidence gathered before writing the codedoc_go design spec. Five research agents ran in parallel at maximum effort; each wrote one report. The probe sources are copied under `probes/<track>/`. The raw outputs (module caches, repo copies, JSON dumps, binaries; about 42 GB) are left in place for verification under `/home/admin/scratch/codedoc-go-research/<track>/`, and the reports cite them by path (`$W` = that track directory, `$R` = `/home/admin/scratch/codedoc-go-research`).

## Environment

| Item | Value |
|---|---|
| Go toolchain | go1.27.2 linux/amd64, SHA-256 `ecbadb99…45fe5` verified against go.dev |
| gopls | v0.23.0 (2026-07-07), built with go1.27.2; v0.24.0 was pre-release only |
| golang.org/x/tools | v0.52.0 (2026-10-10) |
| Trial repo 1 | etcd v3.7.2, commit `68c065e562994b89e333e77b039ad066f933c586`, go.work with 13 modules, 1,109 `.go` files |
| Trial repo 2 | Prometheus v3.15.0, commit `5241a27fe3c6983549fccc32f6e65917408c63cd`, go.work with 5 modules, 731 `.go` files |
| Host | 32 cores, 125 GB RAM, Debian, gcc 14.2.0 |

## Reports

| Report | Question | Verdict |
|---|---|---|
| [real-repos.md](real-repos.md) | gopls `definition` per site (A) vs one go/packages + go/types load (B), on etcd and Prometheus, online and offline | B. Identical answers on all 187,431 call sites; 2.3 s / 5.7 s vs 40–65 s; less memory; only B has the type facts to gate offline wrong answers |
| [engine-probe.md](engine-probe.md) | What gopls answers per Go call shape; protocol, views, workspaces | Direct static calls always right; dynamic dispatch never resolved; offline embedding hazard gives wrong answers; `implementation` is structural and open-world; a relocated go.work (GOWORK outside the repo) reproduces answers and keeps writes out of the repo |
| [go-modules.md](go-modules.md) | go.mod / go.work / vendor / build-constraint / toolchain rules for file-only discovery; safety; offline; download mode | Pure-Python discovery matches the go command exactly; never run go against the repo's own go.work; offline = GOPROXY=off + GOSUMDB=off + GOVCS=*:off; CGO_ENABLED=0; GOTOOLCHAIN=local; lazy `go list -deps -test` download is 10× smaller than `go mod download` |
| [code-seams.md](code-seams.md) | What to fork from codedoc_python; tree-sitter-go vs go/parser | About 60 % of codedoc_python's resolver code is Python-specific and is replaced; four structural seams (methods outside their type's file, package-level import targets, per-file pseudo-class, topic linking by basename); extractor = go/parser helper (tree-sitter-go 0.25.0 fails on Go 1.26 `new(expr)` in 32 etcd files) |
| runtime-oracle.md | A Go runtime call-edge oracle for ground truth and the real-repo precision check | Pending: the agent was still running when this folder was saved |

## Findings that cross reports

- **Interfaces are open-world in Go.** A single in-repo implementer does not prove the runtime target: `testutil.TB` (etcd) is also satisfied by `*testing.T`, and in a probe the runtime target of an in-repo `Logger.Printf` call was the stdlib `log.(*Logger).Printf`. The C#/Java/Python single-implementer upgrade (`iface-impl`, U1) must not be ported as is.
- **Nothing may be written into the repo.** Workspace-mode `go list` and gopls write `go.work.sum` next to the go.work; plain `go list` runs `git status` (rewrites `.git/index`) unless `-buildvcs=false`. Both happened to the shared clones during this study (recorded in go-modules §13 and engine-probe §4); the stray `repos/prometheus/go.work.sum` is left in place on the user's request.
- **Offline answers can be wrong, not just missing.** When an embedded field's type comes from a missing module, the selector resolves to a deeper in-repo method (a wrong edge in both engines). go/packages also attaches in-repo files to stubs of failed imports. Only a go/types-based resolver can see and gate these.

## User decisions so far (2026-10-13)

| # | Question | Answer |
|---|---|---|
| 1 | What Go code, to what bar? | Plain Go modules (go.mod, go.work, vendor/); the codedoc_python bar: scored corpus, `evaluate` gate, 0 wrong edges, five Japanese docs |
| 2 | May CodeDoc download modules? | Detect + opt-in download: offline by default (vendor/ or the existing cache, proxy off); a flag downloads into an isolated cache under OUTPUT_DIR; `evaluate` runs both modes |
| 3 | Real-repo trial | etcd and Prometheus, GCP-free: graph track plus a precision check with a runtime oracle on their own unit tests |
| 4 | Corpus size | Parity with codedoc_python: F1 about 120–150 cases, F2 about 45, F3 multi-repo with an exact edge set |
| 5 | Run modes | Three commands: `--all` (graph, Gemini summaries for each node, wiki pages), `--graph` (graph only), `--wiki` (Gemini summaries and wiki pages; requires a completed `--graph` first) |
| 6 | Outputs | Leave test and research outputs in place for later verification |
| 7 | Delivery | Commit and push when done (new branch, no Claude attribution) |
| 8 | Reference variant | Raised by the user: codedoc_csharp as the state-of-the-art reference rather than codedoc_python. Under discussion |
