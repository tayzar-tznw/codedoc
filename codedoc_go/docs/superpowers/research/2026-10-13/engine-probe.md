# engine-probe: what gopls v0.23.0 answers per Go call shape, and what an LSP client must know

**Date:** 2026-10-13 (JST), about 2 h 40 min of probing.
**Engine:** gopls v0.23.0, built with Go 1.27.2 linux/amd64. Its `serverInfo.version` reads `{"GoVersion":"go1.27.2",…,"Main":{"Path":"golang.org/x/tools/gopls","Version":"v0.23.0",…}}`.
**Evidence root:** `$W = /home/admin/scratch/codedoc-go-research/engine-probe`. Every path below is relative to `$W` unless it is absolute.

**How each claim was established:**
- **[probe]** a hands-on run; the output file is cited.
- **[src]** gopls v0.23.0 source at `$GOMODCACHE/golang.org/x/tools/gopls@v0.23.0`, cited as `file:line`.
- **[doc]** the docs shipped with that tag.
- **[web]** a URL, accessed 2026-10-13.

**Version check [web]:** v0.23.0 is the newest stable release, tagged 2026-07-07T18:53:34Z; v0.24.0-pre.2 is the newest pre-release. Sources: https://proxy.golang.org/golang.org/x/tools/gopls/@v/list and https://proxy.golang.org/golang.org/x/tools/gopls/@v/v0.23.0.info.

---

## 0. Findings that matter for the design

1. **Direct static calls are always right.** `definition` returns the runtime target's name position for every direct call shape tried:
   - package functions, value and pointer methods, methods on non-struct types;
   - promoted methods at any depth, including correct shadowing;
   - method expressions;
   - generic functions and methods, generic aliases, Go 1.27 generic methods;
   - `defer f()` / `go f()`;
   - calls inside `init` and inside package-level `var` initializers;
   - test files and generated files.

   The runtime trace confirms every one of them that executes (§1); external targets rest on the spec. There are only two exceptions:
   - a `//go:linkname` pull stub (2 sites), where the answer is the local body-less stub;
   - the offline hazard in point 3.
2. **gopls never resolves dynamic dispatch.**
   - Interface and constraint method calls return the interface method (14 sites, one of them an anonymous `interface{…}` literal).
   - Calls through func values return the variable, field or parameter declaration (19 sites).
   - Immediately invoked func literals, and conversions to a func-type literal, return `[]` (5 sites).
3. **Offline hazard (a) is real and gives WRONG answers.** With `logr` missing, `s.Info()` resolves to the deeper in-repo `Deep.Info` instead of the shallower embedded `logr.Logger.Info` (3 of 3 sites). The hover even says `// through Base, Deep`, and `outgoingCalls` repeats the wrong target.
   - **Hazard (b).** `implementation` drops methods whose parameter types are valid but different. It does *match every method whose parameter type comes from any missing module*, because all of them collapse to `invalid type`.
   - **Hazard (b').** When an interface embeds a missing interface, `implementation` is over-inclusive.
4. **`implementation` is a structural search over the workspace, every dependency and the test variants. It is not a points-to set.**
   - An in-repo `Logger` with one in-repo implementer also lists `log.(*Logger).Printf`. At that site the runtime target *is* the stdlib method, so "one in-repo implementer" would have produced a wrong edge.
   - It lists test-only fakes, 37 locations for `io.Writer.Write` and 101 for a constraint's `String`.
   - The doc says local types in other packages may be missing.
5. **Views.**
   - Files that are not opened are answered in the default build.
   - A file excluded by GOOS/GOARCH (`_windows.go`) is answered only after `didOpen`, which creates one more view (GOOS=windows) per port.
   - A file with a custom build tag is never answered without `buildFlags`.
   - Files the go command ignores (`testdata/`, `_x/`, `.x/`) *are* answered, through ad hoc packages.
   - A folder holding several modules but no go.work gets an AdHoc view, and cross-module answers come back **empty** unless each module has its own view.
6. **`GOWORK=/abs/path/generated.go.work` outside the repo works**, set either in the process environment or in the `env` setting, with absolute or relative `use` paths. There are four caveats:
   - GOWORK must itself be absolute; a relative value gives "no views".
   - Module `vendor/` directories are ignored, so offline answers become empty.
   - It overrides a module's requirement on a *published* version of a same-path module, which changes both `go run` output and the answers.
   - Files of modules left out of it are answered differently depending on whether they are open.

   The safe use is a **relocated copy of the repo's own go.work** with absolute paths. It reproduced the answers on 244 of 244 Prometheus sites and moved the `go.work.sum` write out of the repo.
7. **Protocol.**
   - There is no `positionEncoding` negotiation: positions are UTF-16 only, and UTF-8 columns give empty answers.
   - Readiness is the `$/progress` "Setting up workspace" pair, one per view. Its end message can read "Finished loading packages." *even when the load failed*, so a client must also watch `window/logMessage` type 1.
   - Errors use JSON-RPC code **0**.
8. **Side effects to contain.** gopls writes, or downloads, all of the following:
   - a telemetry sidecar process and counter files under `os.UserConfigDir`;
   - a file cache (259 MB here) and a goimports index (84 MB) under `os.UserCacheDir`;
   - modules, through `go list`, whenever GOPROXY is on (initial load only);
   - in workspace mode, `go.work.sum` next to the go.work file, which put a 240-line file into the shared Prometheus clone during this study (the lead was told).

---

## 1. Probe corpus and method

| Item | Content |
|---|---|
| Main tree `probe/` | `go.work` (go 1.27; `use ./app ./lib`). Modules `example.com/app` (package main plus internal packages) and `example.com/lib` (packages lib, embed, iface, gen, testy, dotpkg, blankpkg, hidden, trace). 134 sites in the sidecar `probe/probes.py`. Each site is found by its `//@ID` marker plus a callee token, and carries the runtime-correct target(s), alternatives in other builds, and the oracle type |
| Satellite trees | `probe-replace/`: 3 modules, no go.work, `replace => ../rlib`.<br>`probe-vendor/`: `vendor/`.<br>`probe-offline/`: logr v1.4.4 and uuid v1.6.0, for the hazards.<br>`probe-fork/`: a same-path module.<br>`probe-newgo/`: `go 1.28`.<br>`probe-link`: a symlink to `probe/` |
| Runtime oracle | `probe/lib/trace/trace.go`. Every target calls `trace.Hit()`, which records (call-site file:line → callee) with `runtime.Callers`/`CallersFrames` and skips wrapper frames. Builds use `-gcflags=all=-l`, because inlined frames report a wrong callee entry. It runs on the program (`go run`, `out/trace_main.jsonl`) and on the test binary (`go test ./testy/`, `out/trace_test.jsonl`) |
| Oracle result | `out/oracle_check.txt`: **87 confirmed** and 5 explained partials:<br>- FV08/FV09: deferred calls are attributed to the function's closing line;<br>- TS05/TS06/TS11: the test binary runs a different target from production.<br>Also:<br>- 2 non-call sites show no call, as expected;<br>- 1 checker artefact (ME14b: the line's only call is `apply`);<br>- 39 sites are spec-only (builtins, conversions, stdlib or third-party, cgo/asm/runtime, windows or integration files) |
| Driver | `client/lsp_client.py` is a copy of `codedoc_python/graph_generator/lsp_client.py` with three additions: a full frame log, pluggable answers to server requests, and stderr capture.<br>`client/probe_sites.py` sends, per site: `definition`, `typeDefinition`, `hover`, `implementation` at the token, at the call's `(`, and at the answer's declaration; `references`; and `prepareCallHierarchy` + `outgoingCalls` of the enclosing func |
| Naming answers | `tools/declidx` is a go/ast declaration index, the stand-in for CodeDoc's extractor (`out/declidx_*.jsonl`). The tables are produced by `client/summarize.py` and `client/table.py` |
| Final runs | `out/final_none.json` (no `didOpen`; the recommended mode) and `out/final_sitefiles.json` (site files opened). Tables are in `out/final_*_summary.txt` and `out/final_*_table.txt`. The definitions are identical in both runs except at BC02 and BC11 (§5) |

**Verdicts:**
- **correct**: the location is the declaration name of the runtime target.
- **abstract**: an interface or constraint method.
- **value**: a var, field, parameter or local of func type.
- **conversion** / **builtin**: not a call.
- **empty**: `[]`.
- **error**: an error reply (code 0).
- **WRONG**: a concrete function that the runtime does not run.

In the brief's correct / wrong / empty / partial scale, **abstract** and **value** count as *partial*: they are a static reference, not the runtime target. **error** counts as *empty*.

The per-site `typeDefinition`, `hover`, `implementation`, `outgoingCalls` and `references` answers are in `out/final_none_table.txt`.

---

## 2. Per-shape `definition` answers (`out/final_none_summary.txt`)

Locations are `file:line:col` (1-based, UTF-16 column) of the target name.

### 2.1 Package-level calls and methods

| Site | Shape | Answer | Verdict | Notes |
|---|---|---|---|---|
| PK01 | same-package func | `main.localFunc` app/pkgcalls.go:12:6 | correct | |
| PK02 | qualified, other module via go.work `use` | `lib.Hello` lib/lib.go:5:6 | correct | |
| PK03 | import alias `L.Hello` | `lib.Hello` | correct | hover prints `func L.Hello()`: the qualifier follows the file's imports |
| PK04 | dot import `Dotted()` | `dotpkg.Dotted` | correct | |
| PK05 | other package, same module | `helper.Help` | correct | |
| PK06 | blank import path | `package blankpkg` blank.go:1:1 | not a call | runtime: `blankpkg.init.0` is called by `runtime.doInit1` |
| (import) | `definition` on any import path | **one location per compiled file**, at the package clause: uuid gives 15. From an external test, the import of `testy` gives 3, because it resolves to the test variant and so includes `export_test.go` and `testy_test.go` | not a call | collapse the locations to the package directory (`out/import_def.txt`; first reported by code-seams) |
| IN04 | call inside `func init()` | `lib.Hello` | correct | runtime caller name is `blankpkg.init.0` |
| RP01/RP02 | across `replace => ../rlib`, no go.work | `rlib.Do`, `rlib.Svc.Run` | correct / **empty** | correct only when rmain has its own view; empty in the folder's AdHoc view (§5) |
| ME01–ME05 | value/pointer receivers on value and on pointer; `NewCounter()` | `Counter.Value` lib.go:17:18, `(*Counter).Inc` lib.go:22:19, `NewCounter` | correct | |
| ME06/ME07 | methods on `float64`- and `[]string`-based types | `Celsius.F`, `Names.Len` | correct | |
| ME08–ME10 | method expressions `T.M`, `(*T).M`, `(*T).M` of a value method | `Counter.Value`, `(*Counter).Inc` | correct | |
| ME11 / ME14b | method value `f := c.Value`; bound method passed as an argument | the method | reference, not a call | `outgoingCalls` omits both |
| ME12/ME13 | `f()` / `g()` after `f := c.Value` / `g := p.Inc` | the local var | value | runtime: `Counter.Value`, `(*Counter).Inc` |
| ME14/ME15 | `apply(p.Inc)`; `fn()` inside `apply` | `main.apply`; parameter `fn` | correct; value | |

### 2.2 Embedding and interfaces

| Site | Shape | Answer | Verdict | Notes |
|---|---|---|---|---|
| EM01/EM02 | promoted, depth 1 / depth 2 | `Base.Hello` embed.go:7:13 | correct | hover `// through embed.Base` / `// through embed.Mid, embed.Base` |
| EM03 | depth-1 `Mid.Shadowed` shadows depth-2 `Base.Shadowed` | `Mid.Shadowed` embed.go:23:12 | correct | |
| EM04/EM05 | `*Base` embedded: pointer / value method | `(*Base).PtrM`, `Base.Hello` | correct | hover `// through *embed.Base` |
| EM09/EM10 | explicit paths `t.Mid.Base.Hello`, `t.Base.Shadowed` | `Base.Hello`, `Base.Shadowed` | correct | |
| GE19 | promoted from embedded generic `Wrapped[T]{List[T]}` | `(*List).Push` | correct | |
| EM06 | struct embeds interface `Greeter` | `Greeter.Greet` embed.go:34:25 | abstract | runtime `English.Greet`; `implementation` = {English.Greet} |
| EM07/EM08 | interface embeds interface (`Reader` in `ReadCloser`) | `Reader.Read` (the *embedded* interface's method), `ReadCloser.Close` | abstract | |
| IF01 | in-repo interface, 0 implementers | `Zero.Nothing` | abstract | `implementation` = [] |
| IF02 | 1 implementer | `Single.Only` | abstract | impl = {OnlyImpl.Only} |
| IF03 | 2 implementers | `Multi.Do` | abstract | impl = {A.Do, (*B).Do}; the trace shows both run |
| **IF04** | in-repo `Logger`; value is `*log.Logger` | `Logger.Printf` | abstract | impl = {MyLog.Printf, **log.(*Logger).Printf**}. Runtime: `*log.Logger` (recorded dynamic type). One in-repo implementer, yet the target is in stdlib |
| IF05 | same interface, in-repo value | `Logger.Printf` | abstract | runtime `MyLog.Printf` |
| IF06 | `io.Writer` on an in-repo `*Buf` | `io.Writer.Write` $GOROOT/src/io/io.go:100:2 | abstract | impl = 37 locations (stdlib plus `(*Buf).Write`) |
| IF07 | `fmt.Fprintf(w, …)` | `fmt.Fprintf` $GOROOT/src/fmt/print.go:214:6 | correct (external) | the in-repo `Write` runs from fmt/print.go:217 (trace), which is invisible at the site |
| IF08 | sealed interface (unexported method) | `Sealed.Name` | abstract | impl = {S1.Name, S2.Name} |
| IF09/IF10/IF12 | type-switch case / assertion to a concrete type | `S1.Name`, `S2.Name` | correct | |
| IF11 | assertion to an interface | `Sealed.Name` | abstract | |
| IF13 | method expression on an interface, `Multi.Do(x)` | `Multi.Do` | abstract | |
| IF14 | `md := se.Name; md()` | local `md` | value | |
| IF15 | assertion to an anonymous `interface{ Name() string }` | a position *inside the literal*, app/iface.go:46:31, with no declaration | abstract (anonymous) | hover `func (interface) Name() string`; impl = 21 methods, stdlib included |

### 2.3 Generics and function values

| Site | Shape | Answer | Verdict | Notes |
|---|---|---|---|---|
| GE01/GE02 | explicit / inferred instantiation | `gen.Identity` gen.go:18:6 | correct | hover `func gen.Identity(x int) int // func[T any](x T) T` |
| GE03/GE18 | inferred with a func argument; type-set constraint | `gen.Map`, `gen.Sum` | correct | |
| GE04/GE05 | method of a generic type; through generic alias `Alias[T] = List[T]` | `(*List).Push` gen.go:25:19 (the origin declaration) | correct | |
| GE06/GE07 | generic function with a method constraint | `gen.Describe` | correct | |
| GE13 | `v.String()` on a type parameter | `Stringer.String` (the constraint) | abstract | impl = 101 locations; runtime target depends on the instantiation (Named/Other) |
| GE10 | Go 1.27 generic method `bx.Apply(strconv.Itoa)` | `Box.Apply` gen.go:82:17 | correct | hover `func (gen.Box[int]).Apply(f func(int) string) string // func[F any](f func(int) F) F` |
| GE12 | Go 1.27 `var idf func(int) int = gen.Identity; idf(3)` | local `idf` | value | runtime `gen.Identity[int]` |
| GE08 | `for x := range gen.Count(2)` | `gen.Count` | correct | explicit call |
| GE14/GE15 | `yield(i)` inside an iterator | parameter `yield` | value | runtime target = the loop body `main.generics-range1` (generics.go:20, trace). It has no named node |
| **GE09** | `for x := range b.All` (method value as iterator) | `(*Bag).All` | correct target, **no call syntax** | the runtime calls it (trace generics.go:25); `outgoingCalls` omits it |
| GE16/GE17 | `f(x)` inside `Map` / `Apply` | parameter `f` | value | runtime `strconv.Itoa` |
| FV01/FV02/FV03 | package var / struct field / parameter | `var fnVar`, `field holder.fn`, `param f` | value | hover `var fnVar func() string`, `field fn func() string` |
| FV04/FV05 | `handlers["a"]()`, `fs[0]()` | the local map or slice | value | |
| FV06/FV12 | closure var; local bound twice (`lib.Hello`, then `localHello`) | the local var | value | runtime: the closure; `localHello` |
| FV07, FV09, FV11 | `func(){…}()`, `defer func(){…}()`, `go func(){…}()` | `[]`, both on `func` and on `(` | **empty** | |
| FV08/FV10 | `defer localDefer()`, `go goTarget()` | `main.localDefer`, `main.goTarget` | correct | |

### 2.4 Things that look like calls but are not

| Site | Shape | Answer | Verdict |
|---|---|---|---|
| NC01 | `var initVal = lib.Hello()` | `lib.Hello` | correct. The runtime caller is the package initializer `main.init`. There is no enclosing FuncDecl, and `prepareCallHierarchy` on the var fails with "initVal is not a function". `incomingCalls` names the caller `init`, kind Variable (13) (`out/misc_checks.txt`) |
| NC02/NC03 | calls in two `func init()` of one file | `lib.Hello`, `localHello` | correct. Runtime names are `main.init.0` and `main.init.1`; gopls calls both `init` |
| NC04, NC06 | `int32(x)`, `[]byte(s)` | `builtin.int32` builtin.go:50:6, `builtin.byte` builtin.go:90:6 | conversion |
| NC05, NC07, NC09, NC10, CV01 | `(*lib.Counter)(p)`, `MyFunc(f)`, `lib.Celsius(3)`, `gen.List[int](…)` | the type declaration | conversion |
| NC11 | `func() string(localHello)` | `[]` | empty |
| NC08 | `mf()` after `mf := MyFunc(localHello)` | local `mf` (hover `var mf MyFunc`) | value |
| BI01–BI09 | `len append make new min max clear recover panic` | `$GOROOT/src/builtin/builtin.go` (body-less func decl) | builtin |
| BI10 | `unsafe.Sizeof` | `$GOROOT/src/unsafe/unsafe.go:198:6` | builtin. It is listed in `outgoingCalls`, unlike `len` |

### 2.5 Stdlib, third party, build constraints, generated code, encoding

| Site | Shape | Answer | Verdict |
|---|---|---|---|
| TP01–TP03 | `uuid.New`, `uuid.Must(uuid.NewRandom())`, `u.String()` | `$GOMODCACHE/github.com/google/uuid@v1.6.0/…` | correct (external) |
| SL01/SL02 | `strings.ToUpper`, `(*strings.Builder).WriteString` | `$GOROOT/src/strings/…` | correct (external) |
| VN01/VN02 | vendored uuid, module view, offline | `vendor/github.com/google/uuid/version4.go:13:6` | correct (vendor path) (`out/vend1.json`) |
| BC01 | `platformName()` from plat.go; variants in `_linux.go`/`_windows.go`/`_darwin.go` | plat_linux.go:5:6, one location only | correct for linux/amd64. Other-build alternatives are never reported |
| BC10 | call inside plat_linux.go | `linuxOnly` | correct |
| BC02/BC11 | calls inside plat_windows.go | not opened: error "no package metadata for file"; opened: correct (GOOS=windows view) | build-dependent |
| BC03/BC03b | inside a `//go:build integration` file | error "no package metadata" (opened or not); correct with `buildFlags: ["-tags=integration"]` (`out/run3_tags.json`) | build-dependent |
| BC04 | inside a `//go:build ignore` package-main file | `lib.Hello` | correct. Loaded as a standalone package on demand; turned off by `standaloneTags: []` (`out/files_sa_none.json`) |
| BC05 | `C.cadd(1, 2)` | `func _Cfunc_cadd` in `$GOCACHE/ae/…-d:47:6` (a cgo-generated file) | external C. With CGO_ENABLED=0: error |
| BC06 | `cg.Add()` with `cgo` / `!cgo` variants | cg.go:12:6 by default; cg_nocgo.go:7:6 with `env.CGO_ENABLED=0` (`out/files_cgo0.json`) | correct for the build |
| BC07 | asm-backed `asm.Add` (body in asm_amd64.s) | asm_amd64.go:4:6, body-less | correct identity, no Go body. `definition` on `·Add` inside the `.s` file returns the Go declaration (`out/robustness.txt`) |
| **BC08** | `//go:linkname runtimeNanotime runtime.nanotime` | the local stub `main.runtimeNanotime` lowlevel.go:12:6 | **WRONG** (runtime runs `runtime.nanotime`) |
| **BC09** | `//go:linkname hiddenSecret example.com/lib/hidden.secret` | the local stub `main.hiddenSecret` | **WRONG** (trace: `hidden.secret`) |
| GC01/GC02 | inside and into a `// Code generated … DO NOT EDIT.` file | `lib.Hello`, `main.generatedHelper` | correct |
| FL01–FL03 | files under `testdata/`, `_hidden/`, `.dotdir/` | `lib.Hello` | correct, as ad hoc packages, although the go command ignores these directories (`out/files_def_none.json`) |
| EN01/EN02 | `日本語` / `😀` before the token; token `𝔊` (astral) | `main.héllo`, `main.𝔊` | correct with UTF-16 columns |

### 2.6 Tests (`lib/testy`)

| Site | Shape | Answer | Verdict |
|---|---|---|---|
| TS01–TS03, TS07–TS10 | in-package test → unexported function; calls inside `export_test.go`; external test → function declared in `export_test.go` | the function | correct |
| TS04 | external test `testy.InternalHelper()`, where `var InternalHelper = internalHelper` | `var InternalHelper` export_test.go:5:5 | value |
| TS05 | `c.Now()` in testy.go; a test-only `fakeClock` exists | `Clock.Now` | abstract. impl = {realClock.Now, **fakeClock.Now** (testy_test.go)} |
| TS06 | `nowFn()` in testy.go; a test reassigns `nowFn` | `var nowFn` | value. The test binary runs `TestIn.func1` (trace) |
| **TS11** | `o.Hook()` in testy.go promotes `inner.Hook`; testy_test.go declares `func (Outer) Hook()` | `inner.Hook` testy.go:40:14 | correct for production. **The test binary runs `Outer.Hook`** (trace), and gopls answers from the non-test variant only |

### 2.7 Offline hazards (`probe-offline/`; download = isolated `GOMODCACHE=modcache-dl`; offline = empty cache + `GOPROXY=off`)

The runtime oracle in download mode recorded `DeepHits=[]`: the in-repo `Deep.Info`/`Deep.Enabled` never run.

| Site | Shape | Download mode | Offline mode |
|---|---|---|---|
| OF01 | `type Svc struct{ logr.Logger; Base }` with `Base{Deep}` and `Deep.Info`; call `s.Info("x")` | `logr.Logger.Info` (correct) | **`Deep.Info` WRONG**. Hover `func (Deep) Info(...) // through Base, Deep`; `outgoingCalls` = Deep.Info |
| OF02 | `s.Enabled()` (same struct) | `logr.Logger.Enabled` | **`Deep.Enabled` WRONG** |
| OF03 | `*logr.Logger` embedded instead | `logr.Logger.Info` | **`Deep.Info` WRONG** |
| OF04 | `Namer{ Name(u uuid.UUID) string }`; impls `ByInt.Name(int)`, `ByString.Name(string)`, `ByUUID.Name(uuid.UUID)`, `ByOther.Name(logr.Logger)` | impl = {ByUUID} | impl = {ByUUID, **ByOther**}. ByInt and ByString are excluded. Hover `Name(u invalid type)` (`out/off2_off.json`) |
| OF05 | `Sink interface{ logr.LogSink; Flush() }`; `OnlyFlush` has only `Flush` | impl = {FullSink} | impl = {**OnlyFlush**, FullSink} |
| OF06/OF07 | `uuid.NewString()`, `l.Info()` on a `logr.Logger` | correct (external) | `[]` (safe) |
| OF08 | plain in-repo call in a package with missing imports | correct | correct |

Signals available offline:
- gopls publishes `could not import github.com/go-logr/logr (no required module provides package …)` for workspace files, even unopened ones (`out/off_off_none.json`).
- `go list -e -json` reports `Incomplete: true` with `DepsErrors` (§7).

**Mechanism [src]:** the method-set fingerprint prints invalid types as `invalid type` (`internal/util/fingerprint/fingerprint.go:174`), so methods with different missing types match one another.

---

## 3. The other requests per site (`out/final_none_table.txt`)

| Request | Behaviour |
|---|---|
| `typeDefinition` at a call | Returns the **result type** of the call, never the callee: `builtin.int` for `localFunc() int`, `iter.Seq` for `Count`, `uuid.UUID` for `uuid.New`. A call with no named result gives error `cannot find type name(s) from type func()`. On a func-valued var it returns the func type's result (FV01 → `builtin.string`) |
| `hover` | Markdown with a `go` code block, then doc text, then a `pkg.go.dev` link (`linksInHover`). The first line is stable per form:<br>`func L.Hello() string`<br>`func (c *lib.Counter) Inc()`<br>`func (embed.Base) Hello() string // through embed.Mid, embed.Base`<br>`func (iface.Multi) Do() string`<br>`func (interface) Name() string` (anonymous interface)<br>`func gen.Identity(x int) int // func[T any](x T) T`<br>`var fnVar func() string`<br>`field fn func() string`<br>`type Celsius float64` (followed by its methods)<br>`func len(v Type) int`<br>`package blankpkg`<br>Offline types print as `invalid type`. The package qualifier follows the querying file's import names |
| `implementation` on a call token | Always identical to `implementation` at the interface method's declaration (all sites).<br>- **Interface method:** implementers across the workspace, all dependencies, stdlib and test variants (IF04, IF06, GE13, TS05).<br>- **Concrete method:** goes "upwards" to the interfaces it satisfies (TP03 → `fmt.Stringer.String`, `runtime.stringer.String`, …).<br>- **Function:** error `X is a function, not a method (query at 'func' token to find matching signatures)`.<br>- **Var:** error `X is a var, not a type` |
| `implementation` at the `(` of a dynamic call | Signature match **within the querying package only** (v0.19 notes: "Only the local (same-package) algorithm"). FV01 lists 9 `func() string` functions of package main but not the runtime target `lib.Hello` in another package; ME13 lists 18 `func()` functions, `init` included. Over- *and* under-inclusive |
| `outgoingCalls` (enclosing FuncDecl) | Lists static call expressions, with an interface method as the target for interface calls (`out/outgoing_full.txt`). Calls inside nested func literals are attributed to the enclosing FuncDecl, and `defer`/`go` calls are included.<br>It omits calls through func values, method values, the range-over-func implicit call (GE09), IIFEs themselves, conversions and builtins (but not `unsafe.Sizeof`) |
| `incomingCalls` | **Counts references as calls**: `var fnVar = lib.Hello`, `holder{fn: lib.Hello}`, map-literal values and `op := lib.Hello` all show as callers of `lib.Hello` (`out/misc_checks.txt`). It is not a call graph |
| `references` | Deduplicated; includes `_test.go` files. For an interface method it also lists calls of the concrete methods that implement it (IF08 includes the S1/S2 call sites) |

---

## 4. Protocol and session facts

| Topic | Fact | Basis |
|---|---|---|
| `initialize` | Round trip 46–49 ms.<br>Capabilities: definition, typeDefinition, implementation, references, hover, callHierarchy, typeHierarchy, workspaceSymbol, inlayHint, codeLens, semanticTokens (advertised because the client supports `workspace/configuration`), 47 `executeCommand` commands. `textDocumentSync` = {openClose, change: 2, save}. `workspace.workspaceFolders.changeNotifications` is supported | [probe] `out/discover1.initialize.json`; [src] general.go |
| `serverInfo` | `name: "gopls"`. `version` is a **JSON string** of build info: `GoVersion`, `Path`, `Main{Path, Version: "v0.23.0", Sum}`, `Deps[]` | [probe] same file; [src] general.go:273 |
| Position encoding | The client offered `["utf-8","utf-16"]`; the server returns no `positionEncoding`, so UTF-16 applies. `héllo` at UTF-16 column 16 is found, at UTF-8 column 22 the answer is `[]` | [probe] `out/utf8_check.txt`; [src] the server never reads `positionEncodings` |
| Readiness | With `window.workDoneProgress`: `window/workDoneProgress/create` → `$/progress` begin `"Setting up workspace"` / `"Loading packages..."` → end `"Finished loading packages."`. There is **one token per view or folder** (3 folders gave 3 tokens).<br>Without the capability: `window/showMessage` type 4 `"Loading packages..."`, then type 3 `"Finished loading packages."`.<br>`window/logMessage` carries `Created View (#n)` (view_type, root_dir, build_flags, env, env_overlay, ExplicitGOWORK) and `go/packages.Load #n … packages=N duration=…`.<br>Requests also block until their view has loaded | [probe] `out/discover1.log.jsonl`, `out/rep_folders.json`, `out/noprogress.txt` |
| Readiness timings | probe 0.155–0.244 s (11 runs).<br>etcd v3.7.2: 0.70–0.81 s over 3 runs (Load #1: 390 packages, 575–680 ms).<br>Prometheus v3.15.0: 6.07 s cold, 1.0 s warm (Load #1: 311 packages, 5.84 s / 0.75 s).<br>Another agent was running heavy gopls jobs at the same time | [probe] `out/bench_*.json` |
| Load failure | It surfaces in two different ways:<br>1. **Relative GOWORK:** the progress end says `"Error loading packages: … invalid GOWORK: not an absolute path"`, plus `showMessage` type 1 `"Error loading workspace folders (expected 1, got 0)"`; every request then fails with `no views`.<br>2. **go.mod `go 1.28` under GOTOOLCHAIN=local:** the progress end still says **"Finished loading packages."**. The failure appears only as `logMessage` type 1 `"initial workspace load failed: … go.mod requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)"` and a go.mod diagnostic (source `go list`); requests return `no package metadata` | [probe] `out/gw_relenv.json(.log.jsonl)`, `out/newgo.json.log.jsonl` |
| Server→client requests seen | - `workspace/configuration`: one request per workspace folder, each with the single item `{scopeUri: <folder>, section: "gopls"}` (3 folders gave 3 requests, `out/rep_folders.json.log.jsonl`).<br>- `window/workDoneProgress/create`.<br>- `client/registerCapability` ×2: `workspace/didChangeWatchedFiles` with globs `**/*.{mod,work}` and `<module>/**/*.{go,mod,sum,work}` including `vendor/**`; `workspace/didChangeConfiguration`.<br>- `window/showMessageRequest`, **only after `didSave` of go.mod**: `"Dependencies changed in …/go.mod. Run govulncheck to check for vulnerabilities?"` with Yes/No/Always/Never; a `null` reply ends it.<br>A `null` reply to everything kept gopls healthy. Never seen: `workspace/workspaceFolders` and the `*/refresh` requests | [probe] `out/misc_checks.txt`, `out/final_*.json` (`server_requests`) |
| Possible per source | - `client/unregisterCapability` when watch patterns change (general.go:505).<br>- The telemetry prompt needs `telemetryPrompt: true` (default false), 1 % sampling and a 7-day grace period (prompt.go:33,39; general.go:323).<br>- The vulncheck prompt is triggered only by Save/Create of go.mod or go.work (text_synchronization.go:317, vulncheck_prompt.go:127).<br>- `ApplyEdit`/`ShowDocument` are called only from `executeCommand` handlers (command.go:991, 1627) | [src] |
| Errors | JSON-RPC error **code 0** for: `no package metadata for file …`, `column is beyond end of line`, `line number 999 out of range 0-25`, `can't find definitions for file type go.mod`, `no views`, `… is a var, not a type`, `not a dynamic function call`. Whitespace and comments give `[]`. None crashed it | [probe] `out/robustness.txt` |
| Files that were not opened | Answered for every file of a loaded package (test files and generated files included), and on demand through `file=` loads: `//go:build ignore` standalone files, `testdata/`, `_x/`, `.x/`, even a file outside the folder.<br>Not answered: GOOS/GOARCH-excluded files and custom-tag files | [probe] `out/final_none.json` vs `out/final_sitefiles.json`; `out/files_def_none.json` |
| Cost of opening | Probe: RSS 289 MB unopened vs 416 MB with site files opened.<br>Per request on unopened files: about 3–7 ms (implementation, outgoingCalls, typeDefinition) vs 0.2–1 ms on opened ones. `definition` costs about the same either way (sum 652 vs 670 ms over 134 sites).<br>etcd, 375 definitions: unopened p50 26 ms / p95 40–44 ms; opened p50 6.4 ms / p95 84 ms with RSS 1.5 GB | [probe] `out/final_*.json.log.jsonl`, `out/bench_etcd_*.json` |
| Memory (RSS after the sweep / high-water mark) | probe 248–342 MB.<br>etcd unopened 559–799 / 672–1009 MB.<br>Prometheus 993–1993 / 1447–2659 MB (warm vs cold file cache).<br>Noisy: GC timing and concurrent jobs | [probe] `out/bench_*.json` |
| Crash and exit | After SIGKILL the client sees EOF within 3 ms, and pending requests fail. Restart to ready takes 0.17 s on the probe.<br>Exit code 0 after `shutdown` + `exit`, 1 after `exit` without `shutdown`, 0 on stdin EOF | [probe] `out/robustness.txt` |
| Side effects | - Telemetry mode is read from `$XDG_CONFIG_HOME/go/telemetry/mode`. When it is not `off`, gopls starts a sidecar process `gopls ** telemetry **` and writes counter files; with mode `off` there is no child.<br>- The file cache lives in `$GOPLSCACHE` or `$XDG_CACHE_HOME/gopls` (259 MB after etcd + Prometheus).<br>- The goimports index lives in `$XDG_CACHE_HOME/goimports` (84 MB) and has no other override.<br>- `go list`, run by gopls, **downloads missing modules when GOPROXY is on** (`out/off_autodl.json` filled an empty cache). Only the initial workspace load may use the network (view.go:704 passes `NetworkOK`); later loads append `GOPROXY=off` (load.go:374).<br>- **In workspace mode the go command writes `<go.work>.sum` next to the go.work file.** **Write caused by this study:** a Prometheus session with the repo's own go.work (GOPROXY=off, GOSUMDB=off) created `repos/prometheus/go.work.sum` in the shared clone (240 lines, 23,233 bytes, 20:19:19 JST). The repo gitignores it. It was left in place at the lead's instruction. This was the only write this track made in either clone; `find -newermt` over both clones found nothing else of mine. The same session with GOWORK pointing to a relocated copy wrote it next to the copy instead (`out/compare_prom_genwork.json`). My earlier Prometheus sessions with GOSUMDB at its default did not create the file.<br>- gopls's own loads ran **no `git`** in a git repo. A plain `go list -json ./...` ran 7 git commands, including `git status --porcelain`, which rewrites `.git/index`; `GOFLAGS=-buildvcs=false` stops them (`out/git_invocations.txt`) | [probe] `out/children.txt`, `du`; [src] main.go:31, filecache.go:412, x/tools modindex/index.go:114, x/telemetry start.go:151,189 |

**Settings that matter** (defaults from `internal/settings/default.go`; all names accepted by `settings.go`):

| Setting | Default | Probed effect |
|---|---|---|
| `buildFlags` | `[]` | `-tags=integration` makes `integ.go` answerable (`out/run3_tags.json`) |
| `env` | `{}` | Takes precedence over the process environment for GOWORK (view.go:1064–1066). Probed: GOWORK (must be absolute), CGO_ENABLED=0 (BC06 moves to cg_nocgo.go), GOPROXY, GOMODCACHE. GOOS/GOARCH are used for views |
| `directoryFilters` | `["-**/node_modules"]` | Excluding `app/testdata` makes FL01 an error. Excluding `lib/testy` still answers TS05, because the package is imported and so loaded as a dependency (`out/files_df_none.json`) |
| `standaloneTags` | `["ignore"]` | `[]` makes BC04 an error |
| `expandWorkspaceToModule` | `true` | No visible effect in a GoWork view (`out/expand_*.json`) |
| `includeReplaceInWorkspace` | `false` | Not probed: it would add `replace`d directories to a GoMod view's workspace (view.go:921–933) |
| `vulncheck` | `"Prompt"` | Set `"Off"` |
| `hoverKind`, `linksInHover` | `FullDocumentation`, `pkg.go.dev` | `linksInHover:false` drops the link section. `hoverKind:"SingleLine"` returns the bare signature line with no code fence, for example `func (embed.Base) Hello() string // through embed.Mid, embed.Base`. `"NoDocumentation"` returns the code block only (`out/hover_settings_{1,2,3}.json`) |
| `telemetryPrompt`, `zeroConfig` | `false`, `true` | |
| `initializationOptions` | | Same keys, honoured. A `workspace/configuration` answer of `{}` or `null` does not reset them (`out/files_io1.json`, `io2`) |

---

## 5. Views and workspaces in v0.23.0

**View choice [src view.go:836–997]:**
1. GOPACKAGESDRIVER, if set.
2. Otherwise a go.work: the explicit `GOWORK` (`env` setting or process environment) wins over a go.work found by searching upward.
3. Otherwise go.mod.
4. Otherwise GOPATH.
5. Otherwise ad hoc.

If the open file's module is not in the go.work, gopls uses a GoMod view with `GOWORK=off` (view.go:951–960).

**A file's view (session.go:472–499, 606):** the longest-root GoWork view whose module set contains the file's module, else a GoMod view, else GOPATH, else AdHoc. Among those, the view whose port matches the file's build constraints. If none matches, gopls still uses the first relevant view.

| Case | Result | Basis |
|---|---|---|
| go.work at the folder root | One GoWork view; the load query is every `use`d module plus `builtin` | [probe] `out/discover1.log.jsonl` |
| Several modules, no go.work, no root go.mod (`probe-replace`) | An **AdHoc** view at the folder (`query=[./ builtin]`). Unopened files load ad hoc, so cross-module imports fail: RP01/RP02 are **empty**, while same-package RP03 works.<br>`didOpen` creates one GoMod view per module, and then all are correct.<br>**One workspace folder per module** gives one GoMod view each, all correct without `didOpen` | [probe] `out/rep_none.json`, `rep_sitefiles.json`, `rep_folders.json` |
| `GOWORK=/abs/genwork/abs/go.work` (absolute `use`) or `…/rel/go.work` (`use ../../probe-replace/rmain`), set through the process environment or through `{"env":{"GOWORK":…}}` | One GoWork view, root = the workspace folder, `ExplicitGOWORK` = the path; all three modules load; RP01–RP03 are correct | [probe] `out/gw_env_abs.json`, `gw_set_abs.json`, `gw_set_rel.json` |
| GOWORK value relative (`../genwork/abs/go.work`) | `go: invalid GOWORK: not an absolute path` → `no views` | [probe] `out/gw_relenv.json` |
| GOWORK pointing to a **relocated copy of the repo's own go.work**, with its `use` paths made absolute (Prometheus) | `definition` answers are **identical on 244 of 244** sampled sites (232 non-empty in both sessions). `go.work.sum` is written next to the copy, not into the repo | [probe] `out/compare_prom_genwork.json`, `genwork/prom/go.work` |
| Generated GOWORK and a module with `vendor/` | **vendor is ignored**: answers point into GOMODCACHE. With an empty cache and GOPROXY=off they are **empty**, with diagnostic `could not import … (no required module provides package …)`. Without GOWORK, the GoMod view uses `vendor/…` even offline | [probe] `out/vend1.json`, `vend2.json`, `vend3.json` |
| Generated GOWORK that leaves out the `app` module | Unopened app files load ad hoc in the GoWork view; imports of workspace modules resolve, so PK02 is correct. Opened app files get a GoMod view with `GOWORK=off`, and PK02/ME01 are **empty**. **The answers depend on `didOpen`** | [probe] `out/gw_partial_none.json`, `gw_partial_open.json` |
| Generated GOWORK and a module that requires the *published* `github.com/google/uuid v1.6.0` while the repo also holds a module with that path | Without GOWORK: the answer is in `$GOMODCACHE/…uuid@v1.6.0/version4.go:21:6` and `go run` prints a UUID. With GOWORK: `probe-fork/uuidfork/uuid.go:4:6` and `go run` prints `in-repo-fork`. **A generated go.work can change the program** | [probe] `out/fork_plain.json`, `fork_genwork.json` |
| `_windows.go` opened on linux | A new GoWork view with `env_overlay=[GOARCH=amd64 GOOS=windows]`, and answers there (BC02/BC11 correct).<br>`_darwin.go` gets darwin/amd64; `//go:build !amd64` gets darwin/arm64, the first match in `preferredPorts` (port.go:31).<br>Per the doc, such views set CGO_ENABLED=0 implicitly. Not opened: `no package metadata` | [probe] `out/views_probe.txt`; [doc] workspace.md |
| Custom tag, `ignore`, or `!cgo` file opened | Creates **one redundant view with no overlay**, which reloads the whole workspace. The custom-tag file is still unanswered | [probe] `out/views_probe.txt` |
| etcd: excluded files opened (windows, darwin, wasm, solaris, openbsd) | 5 files opened → 6 views, and all 4 queried files answered. RSS by case, over two runs:<br>- 5 files opened: 610–612 MB;<br>- 1 windows file opened: 593–652 MB;<br>- no files opened: 474–531 MB.<br>Each extra view runs its own metadata load | [probe] `out/view_cost.txt` |
| Folder opened through a symlink | Answer URIs are the real paths | [probe] `out/symlink.json` |
| etcd tags seen | 24 files `!cluster_proxy` vs 3 `cluster_proxy`; `tools`, `libs`, `integration`; 7 windows, 3 darwin, 2 solaris, 1 openbsd, 1 wasm | [probe] grep in the shared clone |

---

## 6. Test variants

| Question | Answer | Basis |
|---|---|---|
| Duplicated locations? | None: 0 duplicate locations in any `definition`, `implementation`, `references` or `typeDefinition` list over 134 sites | [probe] `out/final_none.json` |
| Which variant answers `definition`/`hover`/`typeDefinition`/call hierarchy? | The **narrowest** package for the file, which is the non-test variant for a non-test file (definition.go:33, implementation.go:72, call_hierarchy.go:31) | [src] |
| Does `implementation` see test-only types? | **Yes.** Local search type-checks every variant of the declaring package (implementation.go:146); global search uses AllMetadata minus intermediate test variants (implementation.go:197–201). TS05 returns `fakeClock.Now` from testy_test.go | [src] + [probe] |
| Can answers differ between `p` and `p [p.test]`? | **Yes**, and gopls reports only `p`:<br>- TS11: a method added in `_test.go` shadows a promoted method in the test binary;<br>- TS06: a test reassigns a package func var | [probe] trace in `out/oracle_check.txt` |
| `references` | Include `_test.go` files, deduplicated | [probe] |

---

## 7. Implications for a Go resolver

### 7.1 Answers that can be trusted as they are (candidate `MethodCalls`)

An answer qualifies only when all of these hold:
- the site is a call expression;
- `definition` returns exactly one location;
- that location is the name of an in-repo FuncDecl that has a body;
- no gate in §7.3 fires.

This covers:
- package functions;
- value and pointer methods;
- promoted methods (any depth, with shadowing);
- method expressions;
- generic functions, generic methods and calls through generic aliases;
- `defer f()` / `go f()`;
- calls in `init` and in var initializers;
- test files and generated files.

Locations under `$GOROOT/src`, `$GOMODCACHE` or `vendor/` are `external`. Classify them after `realpath`, because answers already come back as real paths.

### 7.2 Shapes that need a rule (the target is static, but gopls does not give it)

| Shape | gopls answer | Rule |
|---|---|---|
| IIFE: `func(){}()`, `defer func(){}()`, `go func(){}()` | `[]` | The callee is a FuncLit, so the target is the FuncLit node (syntactic and certain) |
| Local var bound once to a FuncLit, a function or a method value (FV06, ME12, ME13, NC08) | the var | The extractor proves a single binding, no reassignment and no address taken (like Python's H5/R5); the target is the bound value. Package-level vars must include `_test.go` in the scan (TS06), so they usually end as `dynamic` |
| Conversion (`T(x)`, `(*T)(p)`, `[]byte(s)`, `MyFunc(f)`, `List[int](x)`) | type declaration (`builtin.go` for basic types) | Target kind is a type → no edge. `func() string(f)` returns `[]` and has no identifier, so decide it syntactically |
| Builtins | `builtin/builtin.go`, `unsafe/unsafe.go` | `builtin` status |
| `//go:linkname local target` on a body-less FuncDecl | the local stub (**wrong**) | Retarget to `target`: in-repo → that declaration (BC09), otherwise external (BC08); or gate. A body-less decl with no linkname and a `.s` file in the package is an asm function: keep it and mark it |
| `for x := range f` / `range obj.Method` (GE09) | `definition` on the ident is the method or func | The extractor must emit an implicit-call site; it has no call syntax and `outgoingCalls` misses it. The `yield(...)` calls inside iterators target anonymous loop bodies, so they are `dynamic` |
| `C.fn()` | `_Cfunc_fn` in a `$GOCACHE` cgo file | `external` (C). Never classify it as in-repo |

### 7.3 Gates (never `MethodCalls`)

| Gate | Trigger | Why |
|---|---|---|
| Interface dispatch | The answer is an interface method, an anonymous interface literal (IF15) or a constraint method (GE13) | `implementation` is structural. It includes stdlib and dependency types (IF04's runtime target is stdlib), test-only types and, offline, false positives (OF04/OF05). Per the doc it misses local types in other packages, and it cannot see reverse dependents of exported interfaces. Use `PossiblyCalls` to the in-repo candidates only. **Do not port U1 ("one implementer → edge")** |
| Func values | The answer is a var, field, param or local | `dynamic`. The signature search at `(` is same-package and over-inclusive (FV01, ME13) |
| Missing dependencies | The package, or any type on the receiver's embedding path, has unresolved imports. Signals: a `could not import` diagnostic, `go list -e` `Incomplete`/`DepsErrors`, or `invalid type` in hover | OF01–OF03 are WRONG offline. Conservative choice: no `MethodCalls` from selector calls in such packages, and no `implementation`-based candidates |
| Build-dependent targets | The target name has alternative declarations under mutually exclusive constraints: BC01; etcd's `cluster_proxy`; cgo/!cgo (BC06) | The answer is right only for the session's GOOS/GOARCH/tags/CGO_ENABLED. Record that build in `resolutions.json` (the analogue of Python G3's decided branch), or emit `PossiblyCalls` to the alternatives |
| Test-variant divergence | The receiver type, or a type it embeds, gains methods in a same-package `_test.go`; or a package func var is reassigned in tests | Production and the test binary run different code (TS11, TS06). The real-repo trial's test-run oracle will observe the test-binary target |
| Excluded file | The error is `no package metadata for file` | `unresolved` (reason `excluded-by-build`), unless a dedicated session (GOOS env or `-tags`) covers the file |
| Directories the go command skips | `testdata/`, `_*`, `.*` | gopls answers them anyway, so the scan must exclude them to match the go command |

### 7.4 Session recipe

Each line is backed by a probe above unless it is marked *(go-modules)*. Those lines come from the go-modules report (`reports/go-modules.md` §7.4) and were not re-probed here.

- **Start:** `gopls serve`, with cwd = the repo, and these environment settings:
  - `GOTOOLCHAIN=local`;
  - leave `-mod` unset, so that the go command picks `vendor/` when `vendor/modules.txt` is present. An explicit `GOFLAGS=-mod=readonly` turns vendoring off: offline, VN01/VN02 come back `[]` (`out/vend_modreadonly.json`);
  - **offline:** `GOPROXY=off`, plus `GOSUMDB=off`, because GOPROXY=off alone still lets the go command call sum.golang.org when go.sum entries are missing *(go-modules)*. Note that GOSUMDB=off is the setting under which the go command wrote `go.work.sum` here, so pair it with the relocated go.work below;
  - **download mode:** a proxy plus an isolated `GOMODCACHE`, pre-filled with `go mod download`. gopls downloads only during its initial load (view.go:704);
  - `GOVCS='*:off'`, and do not pass `GOPRIVATE`/`GONOPROXY` through, otherwise the go command can run `git ls-remote` even with GOPROXY=off *(go-modules)*;
  - an explicit `CGO_ENABLED`. With cgo on, loads run gcc and pkg-config on cgo preambles *(go-modules)*. With `0`, the build picks other files (BC05, BC06), so record the choice;
  - `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` under `OUTPUT_DIR` (or `HOME`, plus `GOPLSCACHE`), and write `off` to `$XDG_CONFIG_HOME/go/telemetry/mode`;
  - `GOFLAGS=-buildvcs=false` for any `go list` that CodeDoc runs itself, so it never touches the repo's `.git`. A `-json=` field list without `Stale` also runs no git *(go-modules)*. Any `git status` CodeDoc runs should use `--no-optional-locks`.
- **Settings:** send `{"buildFlags":[…], "env":{…}, "vulncheck":"Off", "standaloneTags":["ignore"], "directoryFilters":[…], "hoverKind":"SingleLine", "linksInHover":false}`, both in `initializationOptions` and as the `workspace/configuration` answer. `SingleLine` gives one parseable line per hover.
- **Workspace:**
  - **The repo has a go.work.** Copy it to `OUTPUT_DIR`, make its `use` (and `replace`) paths absolute, copy any `go.work.sum` beside it, and set the absolute `GOWORK`. This reproduced the answers on 244 of 244 sites and keeps `.sum` writes out of the repo.
  - **The repo has no go.work.** Pass **one workspace folder per module** rather than generating a go.work: this keeps `vendor/` and published-version semantics. A generated go.work is safe only when every cross-module requirement is already a local `replace`.
- **Readiness:** wait until every begun `"Setting up workspace"` token has ended. Fail when the end message starts with `Error loading packages`, or when a `logMessage` type 1 says `initial workspace load failed`.
- **During resolution:**
  - do not `didOpen` files: unopened answers are complete for the default build and cost less memory;
  - opening is only worth it for excluded-port files, and creates one view per port;
  - treat a code-0 error as `unresolved` with its reason;
  - convert columns to UTF-16 at the boundary;
  - read the version from `JSON.parse(serverInfo.version).Main.Version`.

---

## 8. Open risks and what was not verified

- **No points-to analysis from gopls.** Interface and func-value sites stay `PossiblyCalls`/`dynamic`. The edge rate will depend on how much dispatch the repo uses. A go/types RTA/VTA helper (the sibling track) would be the only way to upgrade them.
- **Hazard (a) was shown with one module (`logr`).** Two other routes probably behave the same way but were not probed: a missing *interface* embedded in a struct next to a deeper in-repo method, and in-repo generated packages that are absent from the repo.
- **Performance figures are noisy.** Another agent ran heavy gopls jobs at the same time; GC and filecache warmth also vary. Memory ranges come from 2–3 runs each. Concurrent or pipelined requests were not probed.
- **Not probed:**
  - GOPATH views;
  - `includeReplaceInWorkspace`;
  - `go work vendor`;
  - GOOS views for ports other than windows/darwin/js-wasm/solaris/openbsd;
  - cgo inside a non-native GOOS view (the doc says CGO_ENABLED=0 there).
- **The redundant no-overlay view** for custom-tag, ignore and !cgo files was observed only on the probe tree.
- **One workspace folder per module** means one view, and one dependency load, per module. Memory for a repo with many modules and no go.work was not measured.
- **Go 1.27 shapes do not occur in etcd or Prometheus** (both `go 1.26`). Only one generic-method shape and one typed-var inference shape were probed.
- **The telemetry prompt was not triggered.** Its conditions are cited from source; it is off by default.
- **The relocated-go.work check covered Prometheus only**, on `definition` answers for 244 sampled sites. Its go.work has no `replace` lines, so rewriting relative `replace` paths was not exercised.
- **The sumdb, GOVCS and cgo-compiler facts in §7.4 come from go-modules** and were not re-probed here.

## Sources (accessed 2026-10-13)

- gopls versions: https://proxy.golang.org/golang.org/x/tools/gopls/@v/list and https://proxy.golang.org/golang.org/x/tools/gopls/@v/v0.23.0.info
- Workspace builds, GOOS views, CGO_ENABLED and custom tags: https://github.com/golang/tools/blob/gopls/v0.23.0/gopls/doc/workspace.md
- Implementation semantics, cross-package local types and signature matching: https://github.com/golang/tools/blob/gopls/v0.23.0/gopls/doc/features/navigation.md
- Release notes shipped in the module:
  - `gopls/doc/release/v0.17.0.md`: asm definition;
  - `v0.19.0.md`: signature implementations, same-package only;
  - `v0.22.0.md`: vulncheck prompt;
  - `v0.23.0.md`.
- Go 1.27 generic methods: `$GOROOT/doc/go_spec.html`, "Method declarations" and "Go 1.27".
