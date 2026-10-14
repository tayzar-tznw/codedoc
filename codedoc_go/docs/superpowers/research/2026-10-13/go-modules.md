# go-modules: module, workspace, toolchain and build rules for `codedoc_go`

Track `go-modules`, 2026-10-13. Toolchain: Go 1.27.2 linux/amd64 and gopls v0.23.0 (both shared). Machine: 32 cores, gcc 14.2.0.

Probes live under `$W=/home/admin/scratch/codedoc-go-research/go-modules`, indexed in `$W/README.txt`. The re-runnable transcripts are `$W/transcripts/*.sh` and `*.txt`.

**Evidence tags**

| Tag | Meaning | Where to look |
|---|---|---|
| **P** | hands-on probe | the path given |
| **S** | source code | Go 1.27.2: `$GR=$R/toolchain/go/src`. gopls v0.23.0: `$GP=$R/gomodcache-shared/golang.org/x/tools/gopls@v0.23.0`. go/packages: `$XT=…/golang.org/x/tools@v0.47.1-0.20260707181000-a299dadba899` |
| **D** | documentation | URL; all accessed 2026-10-13. Local copies are in `$W/docs/`, including the `go help` texts of 1.27.2 |

Anything not tagged P, S or D is marked *unverified*.

---

## 0. The rules on one page

| # | Rule codedoc_go must implement | Evidence |
|---|---|---|
| R1 | **Discovery is pure Python, reading files only.** It parses go.mod, go.work, `vendor/modules.txt` and build constraints. The Python models were checked against the go command and agree exactly. | go.mod: 3,682 files, 0 disagreements (§1). Constraints: 167,184 file decisions, 0 mismatches (§5). std list: 7 targets, exact (§9). |
| R2 | **Never run the go command or gopls against the repo's own go.work.** In workspace mode, a plain `go list` writes `go.work.sum` next to the go.work, even with GOPROXY=off. No `-mod` value prevents it. Instead, generate a go.work under OUTPUT_DIR and set `GOWORK=<abs path>`. Single-module repos can use `GOWORK=off`: readonly mode never writes go.mod or go.sum. | P: Prometheus got a new 240-line go.work.sum (`$W/p12-download/runs/prometheus-list-ws-t1/summary.txt`). **I also caused this in the shared clone by mistake: see `$W/incident/`.** S: `modload/init.go:2013-2019`, `modfetch/fetch.go:1032-1062`. |
| R3 | **Build the go/gopls environment from scratch** with the allowlist in §7.4; do not inherit it. Each of these, if inherited, runs extra programs or writes into the repo: GOFLAGS (`-mod=mod`, `-toolexec`), GOCACHEPROG, GOPRIVATE/GONOPROXY, GOAUTH, GOTOOLCHAIN, user go env files. Always pass `-buildvcs=false`: otherwise `go list` runs `git status`, which rewrites `.git/index`. | P: `$W/transcripts/p07_safety.txt`, `p07_gitindex.txt` |
| R4 | **Offline means `GOPROXY=off` *and* `GOSUMDB=off`, plus `GOVCS='*:off'` and empty GOPRIVATE/GONOPROXY/GONOSUMDB.** GOPROXY=off alone still contacts sum.golang.org directly (644 GETs). Inherited GOPRIVATE still makes the go command run `git ls-remote`. | P: `$W/p08-offline/N1x.err`, `$W/transcripts/p07_safety.txt`. S: `modfetch/sumdb.go:176-216`, `modfetch/repo.go:261-268` |
| R5 | **Pin the build context: `CGO_ENABLED=0`, GOOS/GOARCH, and explicit tags.** With cgo on, gopls's `go list -compiled=true` runs gcc (52 times on a cold cache) and runs pkg-config with arguments taken from the repo. Note that the CGO default depends on whether gcc is on PATH. | P: `p07_safety.txt` (S1–S3). S: `cmd/go/internal/cfg/cfg.go:140-184` |
| R6 | **Set `GOTOOLCHAIN=local` and pre-check go lines.** A go.mod or go.work `go` line newer than the pinned toolchain must make the unit fail closed. gopls inherits GOTOOLCHAIN, and with `auto` the go command it runs tries to download a toolchain. | P: `$W/transcripts/p03_p04_p06.txt` (p06) |
| R7 | **Download mode = `go list -e -deps -test -compiled=false -buildvcs=false` over each module dir, under the generated go.work**, into a GOMODCACHE in OUTPUT_DIR, with `GOPROXY=https://proxy.golang.org` (no `,direct`). etcd: 306 MB, 22k inodes, 3–5 s, against 3.7 GB, 127k inodes, 14–17 s for `go mod download`. Prometheus: 1.76 GB, 90k inodes, 10–13 s, against 2.8 GB, 177k inodes, 10–13 s. Afterwards gopls's own flags load everything offline with 0 errors. | P: §12 |
| R8 | **Optional: offline from the user's cache without writing to it.** Use `GOPROXY=file://<user GOMODCACHE>/cache/download` together with an isolated GOMODCACHE. Worked with a read-only source cache: 0 errors, 0 changes to the source. | P: `$W/p08-offline/fileproxy/` |
| R9 | **Workspace mode needs the complete set of `.mod` files for the module graph.** A single missing `.mod`, even one for a module that provides no packages, leaves *every* external package unresolved. In-repo packages still load. Single-module mode with go ≥ 1.17 is lazy and tolerates this. | P: §8 |
| R10 | **The mode decides the runtime truth.** Workspace mode runs MVS across all `use` modules and lets go.work replaces win. In single-module mode, a sibling module that is not replaced resolves to its *published* version (Prometheus `documentation/examples/remote_storage` → `prometheus v0.308.1`). | P: §2, §8 |
| R11 | **Do not support GOPATH mode (GO111MODULE=off); document it as a limit.** It still works in 1.27, but it needs an import-path layout CodeDoc cannot derive from files. | P/D: §10 |
| R12 | **Labels:** take package→module from go list's `Module` field. Take std from a GOROOT/src walk. Map vendor copies through modules.txt; a vendored package that modules.txt does not list has `Module=nil`. | P: §3, §9, §11 |

---

## 1. go.mod as of Go 1.27

### 1.1 Directives

The 1.27.2 parser knows exactly these verbs (S: `$GR/cmd/vendor/golang.org/x/mod/modfile/rule.go:315,349,380-544`). The 1.26 and 1.27 release notes add none (D: https://go.dev/doc/go1.26, https://go.dev/doc/go1.27#go-command).

| Directive | Added in | Form | Read from dependencies' go.mod? (lax parse) | Notes for CodeDoc |
|---|---|---|---|---|
| `module` | before 1.21 | `module path`, or a block with one entry | yes | Exactly one. A `// Deprecated:` comment is attached. A missing module line gives Path "" (P: `lex.out` no_module). |
| `go` | before 1.21 | `go 1.N[.P][rcR]` | yes | Regex `^([1-9][0-9]*)\.(0\|[1-9][0-9]*)(\.(0\|[1-9][0-9]*))?([a-z]+[0-9]+)?$` (rule.go:327). At most one. If missing, 1.16 is assumed. Mandatory minimum since 1.21. |
| `toolchain` | 1.21 | `toolchain go1.N.P[-suffix]` or `default` | no | Regex `^default$\|^go1($\|\.)`. Has no effect under GOTOOLCHAIN=local (P). |
| `godebug` | 1.23 | `godebug key=value`, or a block | no | Ignored in workspace mode, where go.work godebug wins (D: ref/mod #go-work-file-godebug). Does not affect static resolution. |
| `require` | before 1.21 | `require path vX.Y.Z [// indirect]`, or a block | yes | Versions must be canonical. `master` is rejected by go mod edit; the go command would need the network to canonicalise it (P: lex/require_noncanon). |
| `exclude` | before 1.21 | `exclude path vX.Y.Z` | no | Applies in the main module only. |
| `replace` | before 1.21 | `old [v] => new v` or `old [v] => ./dir` | no | Main modules only. A local path starts with `./`, `../`, `/`, `\` or a drive letter (rule.go:773-780). `=>` must be its own token. |
| `retract` | before 1.21 | `retract v` or `retract [lo, hi]` | yes | Does not affect resolution. |
| `tool` | 1.24 | `tool pkg`, or a block | no | D: go1.24 notes. The `tool` package pattern. |
| `ignore` | 1.25 | `ignore ./dir` (relative to the module root) or `ignore name` (at any depth) | yes | Affects pattern matching only; files stay in module zips (D: go1.25 notes). Verified (P: `p03_p04_p06.txt`, p04). |

**Strict vs lax parsing.** A main module's go.mod is parsed strictly: an unknown directive is an error. Dependency go.mod files are parsed laxly, which keeps only `go`, `module`, `retract`, `require` and `ignore` (rule.go:342-354). A go.mod that uses a directive newer than 1.27 is therefore fatal as a main module but fine as a dependency.

go.work verbs are `go`, `toolchain`, `godebug`, `use` and `replace` (S: modfile/work.go:72,697-760). Unknown verbs are an error (P: `$W/p01-modfile/work/bad.work`).

### 1.2 Lexical rules (the Python parser spec)

Source: S `modfile/read.go:504-630` (lexer) and `805-897` (statements and blocks).

| Rule | Detail | Probe in `$W/p01-modfile/lex.out` |
|---|---|---|
| Whitespace | Space, tab and CR separate tokens; `\n` is a token. | crlf → OK |
| Comments | `//` runs to end of line. A whole-line comment is skipped. A suffix comment ends the statement; it carries `// indirect` and the deprecation text. `/* */` is an error. | cblock → error |
| `//` inside a token | Ends the identifier: `example.com/m//x` is the path `example.com/m` plus a comment. | slashslash_ident |
| Punctuation | `( ) [ ] { } ,` are tokens. `=>` is an ordinary identifier, so `a=>./b` is one token. | arrow_nospace → usage error |
| Identifiers | Runs of printable non-space runes, excluding the punctuation above. | |
| Strings | `"…"` is unquoted with Go escapes (strconv.Unquote). **Backquoted raw strings are rejected** ("unquoted string cannot contain quote"), contrary to ref/mod. Bare `'`, `"` or `` ` `` inside identifiers are errors. Newline inside a string is an error. | raw, single_quote → error; escape `\x62` → "b" |
| BOM | A leading U+FEFF is an error. | bom → error |
| Blocks | `verb (` followed by end of line opens a block. Each line inside is one spec. `)` must be followed by end of line. `verb ()` is an empty block. A `(` in mid-line is just a token. `require x (` is "unknown block type". | multitok_block, paren_junk |
| Repeats | A second `module`, `go` or `toolchain` is an error. | dup_go, two_modules |

**Can Python parse it safely? Yes.**
- `$W/scripts/gomodparse.py` (~390 lines, stdlib only) gave output identical to `go mod edit -json` on 3,682 go.mod files: 3,665 identical, 17 rejected by both, **0 disagreements**. The corpus is 3,632 dependency `.mod` files from the etcd and Prometheus caches (3,312 unique module versions), the 18 repo go.mod files and 32 edge cases. Only the comment-derived `Module.Deprecated` and `Retract.Rationale` were left out of the comparison. (P: `$W/p01-modfile/corpus/result.txt`)
- go.work: identical for etcd, Prometheus and a synthetic file covering every verb (P: `$W/p01-modfile/work/`).

### 1.3 `go mod edit -json` / `go work edit -json`

| Question | Answer | Evidence |
|---|---|---|
| Works offline? | Yes; it reads only the named file. | P: `$W/p01-modfile/all.json`, run with GOPROXY=off |
| Writes anything? | No: sha256 of the file unchanged, no new files. (Like every go command, it does write telemetry counters; see §7.1.) | P: `all.sha` check |
| Go line newer than the toolchain under GOTOOLCHAIN=local? | **Works.** `go 1.28` / `toolchain go1.28.1` prints the JSON with exit 0, while `go list` fails. `go work edit -json` on a go.work with `go 1.28` also works. | P: `p03_p04_p06.txt` (p06), `p02_workspace.txt` T15 |
| Syntax error or unknown directive | Exit 1 with `file:line: message`. | P: `lex.out` |
| Field naming | go.work JSON uses `Godebug`; go.mod JSON uses `GoDebug`. | P |

**CodeDoc should** parse go.mod and go.work in Python (gomodparse.py is a validated starting point). `go mod edit -json <abs path>` is an acceptable cross-check: safe, offline, no writes. Run it with `GOWORK=off` from a neutral cwd and with the allowlist environment.

### 1.4 Where the documentation and the 1.27.2 implementation differ (P/S)

1. ref/mod #go-mod-file-lexical says raw strings are allowed, and the keyword list omits toolchain, godebug, tool and ignore. The implementation rejects raw strings and knows all ten verbs.
2. ref/mod #workspaces says GOWORK must name a file ending in `.work`. In 1.27.2 any **absolute** path works; a relative path is an error (P: G3/G4; S: `modload/init.go:366-376`).
3. `go help packages` says `./...` excludes "directories named vendor". In fact the vendor directory itself is matched as a package; only what lies below it is skipped (P: `$W/p04-packages/vend`; S: `modload/search.go:150-152,237-243`).
4. ref/mod #build-commands says vendor mode is the default when "a vendor directory is present". `vendor/modules.txt` is not required for that choice, but its absence then fails the consistency check (§3).

---

## 2. go.work and workspace mode

All probes are in `$W/transcripts/p02_workspace.sh` and `.txt`. They use the synthetic file:// proxy in `$W/proxy`.

| Rule | Evidence |
|---|---|
| **MVS runs across all `use` modules.** Module a requires dep v1.0.0 and b requires v1.2.0: from a, workspace mode gives v1.2.0 and GOWORK=off gives v1.0.0. | P T1 |
| **go.mod replaces of *every* workspace module apply workspace-wide.** a's `replace dep => ../forkA` also applies to b. Relative paths are made absolute against each go.mod's directory before comparison. | P T2, T6; S `modload/init.go:1408-1433` |
| **Conflicts:** the same key (path, or path+version) with different targets is fatal: `go: conflicting replacements for example.com/dep: … use "go work edit -replace example.com/dep=[override]" to resolve`. | P T3 |
| **A go.work replace beats every go.mod replace of that module path,** including version-specific ones. The go.mod replaces for that path are dropped entirely. | P T4, T7; S `init.go:1410` (`replacedByWorkFile`); D ref/mod #go-work-file-replace |
| **A version-specific replace applies only if MVS selects that version.** `replace dep v1.0.0 => …` does nothing once the workspace selects v1.2.0. | P T5 |
| **Specific-plus-wildcard across modules depends on order.** Case: a has `v1.2.0 => forkA` and b has wildcard `=> forkB`. With `use (a b)`, forkB wins silently. With `use (b a)`, the result is fatal: "conflicting replacements found … in workspace modules defined by …". Inside one go.mod, the specific replace wins. | P T8, T8r, T10; S `modload/modfile.go:352-377` |
| **Two `use` dirs with the same module path:** `go: module example.com/a appears multiple times in workspace`. | P T11 |
| **A `use` dir without go.mod, or a missing dir:** `go: cannot load module ../nomod listed in go.work file: open ../nomod/go.mod: no such file or directory`. | P T12 |
| **The go.work go line must be ≥ every used module's go line:** `go: module . listed in go.work file requires go >= 1.22, but go.work lists go 1.21`. A missing go line means 1.18. | P T14, T16; D doc/toolchain #config |
| **The go.work go line must be ≤ the toolchain:** `go: ../go.work requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)`. | P T15 |
| **Vendor:** module vendor dirs are ignored in workspace mode. Only a workspace vendor dir (from `go work vendor`, at `dir(go.work)/vendor`, first line `## workspace`) is used, and then by default. `-mod` may only be `readonly` or `vendor`. | P `p03_p04_p06.txt` V5/V5b/V6, `$W/p03-vendor/wv`; S `init.go:1553-1657`; D https://go.dev/doc/go1.22#go-command |
| **`GOWORK=off`** gives single-module mode and ignores any go.work. | P G6, T1 |
| **`GOWORK=/abs/path`** is honoured from any cwd, overrides a go.work found by walking up from cwd, and may live outside the repo. `use` paths may be absolute, or relative *to the go.work file's directory*. A relative GOWORK is an error: `go: invalid GOWORK: not an absolute path`. | P G1, G2, G3, G8 |
| **Writes:** checksums go to `<GOWORK>.sum` (for example `gen/go.work.sum`). Workspace modules' go.mod and go.sum are never written in workspace mode. | S `init.go:947-958`, `init.go:2013-2019`; P §12 |
| **Patterns outside the used dirs fail:** `pattern …/b/...: directory prefix … does not contain modules listed in go.work`. | P G9 |

**CodeDoc should generate `OUTPUT_DIR/repos/<repo>/go/<group>/go.work`:**
1. Copy the repo go.work's `go`, `toolchain` and `godebug` lines.
2. Write one `use <abs dir>` per in-repo use entry. **Drop out-of-repo entries and warn**; this changes MVS, so mark the group degraded.
3. Copy go.work `replace` lines, making local paths absolute.
4. Neutralise local-path replaces that point outside the repo, in go.work or in any go.mod, by adding a go.work replace (P: `p07_safety.txt` O1–O3):
   - pin to a version, `replace X => X vN`, using the require version when the module is cached; or
   - point to an empty stub module under OUTPUT_DIR, which fails closed.
5. Copy the repo's go.work.sum next to the generated go.work, if there is one.
6. If the repo has a workspace vendor dir (`<go.work dir>/vendor` whose modules.txt starts with `## workspace`), add a symlink `vendor` next to the generated go.work. The go command then uses it, and reports `Dir` through the symlink path, so apply `realpath`. Without the symlink, the module lookup fails offline (P `$W/p03-vendor/wv-gen`).

For repos **without** go.work, use `GOWORK=off` per module, which is the exact single-module semantics. A one-`use` generated go.work behaves almost the same, but it ignores vendor/ and tolerates missing go.sum lines.

---

## 3. vendor/

Probes: `$W/transcripts/p03_p04_p06.txt` (p03) and `$W/p03-vendor/`.

| Rule | Evidence |
|---|---|
| **Default `-mod=vendor`** requires all of: no explicit `-mod`; `<modroot>/vendor` (or `dir(go.work)/vendor` in workspace mode) **is a directory**; the go version of go.mod (or go.work) is ≥ 1.14; and modules.txt's first-line `## workspace` marker matches the mode. A missing modules.txt still selects vendor mode. Otherwise `-mod=readonly`. | S `modload/init.go:1553-1687`; P V1, V3 |
| **Consistency check:** every explicit go.mod requirement must appear as `# path version` plus `## explicit` in modules.txt, and every go.mod replace as `=>`. Example error: `go: inconsistent vendoring in <dir>: example.com/dep@v1.2.0: is explicitly required in go.mod, but not marked as explicit in vendor/modules.txt … To sync the vendor directory, run: go mod vendor`. | P V2, V4; S `modload/vendor.go:143` |
| **Not checked:** vendored file contents (edited files load silently), and packages present in vendor/ but missing from modules.txt (they load with `Module=nil`). | P (`$W/p03-vendor/t/v1`, see the two probes after the transcript) |
| **Offline:** vendor mode never touches the network or the module cache. It worked with GOPROXY=off and an empty GOMODCACHE. `go list -m all` fails: `go: can't compute 'all' using the vendor directory`. `go mod download` and `go mod tidy` still go to the network (D ref/mod #vendoring). | P V1 |
| **Local-path replaces are vendored too.** `example.com/loc => ./local` is copied to `vendor/example.com/loc/`, and modules.txt records `# example.com/loc v0.0.0 => ./local`. go list reports `Dir=vendor/example.com/loc`. The copy can be stale. | P V1 |

**modules.txt grammar** (P: generated by 1.27.2; D: ref/mod #vendoring):

```
## workspace                          # only for `go work vendor`; must be the first line
# <module> <version>[ => <path> [<version>]]   # module header; replacement shown after =>
## explicit[; go 1.N]                 # explicit = required directly in go.mod; go = dependency's go line
<import path>                         # one line per vendored package, belongs to the last header
# <module> => <path>                  # replacement-only record (a replace with no requirement)
```

**CodeDoc should:**
- Treat a module with `vendor/` and go ≥ 1.14 as a vendored unit: `GOWORK=off`, `GOFLAGS=-mod=vendor`, no download.
- Label vendored packages through modules.txt.
- Map definitions that land in `vendor/<local-replace module>/` back to the in-repo directory only if the file content is byte-identical. Otherwise leave the edge unresolved: the vendor copy is what actually builds.

---

## 4. Packages and directories

| Rule | Evidence |
|---|---|
| Package = directory. `./...` skips: dirs starting with `.` or `_`; `testdata`; anything *below* a dir named `vendor` (the vendor dir itself is matched); dirs containing go.mod (nested modules); go.mod `ignore` dirs; dirs with no buildable files for the target. | P `p03_p04_p06.txt` (p04); S `modload/search.go:57-152`; D `go help packages` (1.27.2) |
| `builtin` is never matched (documentation-only). `runtime/cgo` is skipped when cgo is off. | S `search.go:59,66-68` |
| Files starting with `_` or `.` are skipped silently: they are not even listed in IgnoredGoFiles. Files excluded by name, by build tags, or by being cgo files with cgo off go to IgnoredGoFiles. `package documentation` files are ignored. | P p04 (`a/_under.go`, `a/.dotfile.go`); S `go/build/build.go:940-946,1450-1454` |
| A directory can hold package `p`, an external test package `p_test` (`_test.go` files only; XTestGoFiles), and `//go:build ignore` mains (ignored). Two different non-test package names give the error `found packages one (m1.go) and two (m2.go)`. | P p04 (`a/`, `mixed/`); S `build.go:948-971` |
| `export_test.go` is an in-package test file (TestGoFiles). Its symbols exist only in the test variant `p [p.test]`, which the external test package also sees. | P p04 |
| A test-only directory is still a package (TestGoFiles only). `import "C"` in a `_test.go` file is an error. | P p04; S `build.go:990-996` |
| Explicitly named excluded dirs (`./a/testdata`, `./a/_hidden`) load normally. The exclusions apply to `...` patterns only. | P `$W/p04-packages` (first probe) |
| `internal/`: "Code in or below a directory named "internal" is importable only by code that shares the same import path above the internal directory." | D `go help importpath` (`$W/docs/help_importpath.txt`) |
| gopls loads `<moduledir>/...` per workspace module (moduleLoadScope), so it applies the same exclusions. | S `$GP/internal/cache/load.go:94-98`, `view.go:692` |

**CodeDoc should** apply exactly these rules to decide which files have a package. Files outside them (testdata, `_x`, vendor subtrees, nested-module trees seen from the parent, ignored dirs, constraint-excluded files) get no call-graph edges.

---

## 5. Build constraints

**Algorithm.** This is a port of go/build. It is implemented in `$W/scripts/gobuildctx.py` (~370 lines, stdlib only).

1. **File name.** Skip names starting with `_` or `.`. Then apply `goodOSArchFile`:
   - take the name up to its first `.`; drop everything before the first `_`; split on `_`; drop a trailing `test`;
   - if the last two parts are a known OS and a known arch, both tags must match;
   - else, if the last part is a known OS or a known arch, it must match.

   The known lists are in `internal/syslist`: 18 OS values including `nacl`, `zos` and `hurd`, and 24 arch values. **`unix` is not a known OS, so `x_unix.go` is unconstrained by name.** (S `build.go:1995-2027`, `internal/syslist/syslist.go`)
2. **Header.** Scan lines until the first non-comment text (the package clause). Track `/* */` state.
   - A line that is exactly `//go:build` followed by a space or tab (or nothing) is *the* constraint. A second one is an error.
   - If there is no `//go:build` line, use each `// +build` line in the header region that ends at the last blank line before the package clause. The lines are ANDed. (S `build.go:1570-1691`)
3. **`//go:build` grammar.** Operators are `||`, `&&` and `!`, plus parentheses.
   - Tags are runs of Unicode letters, digits, `_` and `.`. The only whitespace is space and tab.
   - `!!` is an error, and so is a trailing comment.
   - The limit is 1,000 terms. (S `go/build/constraint/expr.go:153-358`)
4. **`// +build` grammar.** Space separates OR clauses, comma separates AND terms, and a leading `!` negates. An invalid literal becomes the tag `ignore`. The limit is 100 terms. (S `expr.go:367-450`)
5. **Tag truth** (S `build.go:1947-1978`):
   - GOOS, GOARCH and `gc`;
   - `cgo` iff cgo is enabled;
   - `linux` also under android, `solaris` under illumos, `darwin` under ios;
   - `unix` iff GOOS is in the UnixOS list (12 values);
   - `boringcrypto` is read as `goexperiment.boringcrypto`;
   - plus BuildTags (`-tags`), ToolTags and ReleaseTags `go1.1`…`go1.27`.
6. **ToolTags are not constant.** On linux/amd64 they are `goexperiment.regabiwrappers`, `regabiargs`, `dwarf5`, `jsonv2`, `greenteagc`, `randomizedheapbase64`, `sizespecializedmalloc` and `amd64.v1`. They change with GOARCH, GOAMD64 (`amd64.v1..v3`) and cross-compiling (linux/arm has no regabi tags). Read them once from the pinned toolchain with `go list -f '{{context.ToolTags}}' unsafe`; this command is safe. (P `$W/transcripts/p07_safety.txt` "context")
7. **Package assembly.**
   - After the package clause, a file importing `"C"` goes to CgoFiles, or to IgnoredGoFiles when cgo is off.
   - `_test.go` files are TestGoFiles. If their package is `<name>_test` and that differs from the package name taken from the first file, they are XTestGoFiles. (S `build.go:948-1035`)
8. **CGO default** (Go ≥ 1.20): `CGO_ENABLED` if set; otherwise off when cross-compiling, and off when CC is unset *and* gcc is not on PATH (P: CgoEnabled became false with gcc removed from PATH, but stayed true with `CC=/nonexistent`). (S `cfg.go:140-184`)
9. **Go version terms.** A `//go:build go1.N` term also raises that file's language version, in modules at go ≥ 1.21 (D `go help buildconstraint`). That matters to a type checker, not to file selection.

**Validation** (P `$W/p05-constraints/run.out`, `run-promws.out`):
- I compared the Python classification with `go list -e -json` for every file of std, cmd, the 13 etcd modules and the 5 Prometheus modules.
- 20 targets: linux/amd64 with cgo 1 and 0, windows, darwin, js/wasm, android, ios, illumos, plan9, aix, freebsd/riscv64, wasip1, openbsd, solaris, linux/arm, linux/386, netbsd, dragonfly, loong64 and s390x. Plus three custom-tag runs: `cluster_proxy,integration`; `slicelabels,forcedirectio`; `dedupelabels`.
- **Result: 167,184 file classifications, 0 mismatches.**
- The only directories Python found and go list did not are the std special cases (`builtin`, `runtime/cgo` with cgo off, vendored commands under cmd), plus two Prometheus nested modules that cannot load in offline single-module mode. Those two re-ran in workspace mode on a copy with 0 mismatches (see §8).
- Both repos have **0 in-repo cgo files** and 0 `+build` lines.
- Their tags: etcd uses `cluster_proxy` (27 files) and `cgo && amd64` (4); Prometheus uses `slicelabels` and `dedupelabels`. etcd has 4 more in-repo package variants with cgo=1 than with cgo=0.

**CodeDoc should:**
- Evaluate constraints in Python for one pinned configuration, recorded in units.json: GOOS/GOARCH, CGO_ENABLED=0, ToolTags read from the toolchain, and user tags.
- Pass the same values to gopls and to the runtime oracle.
- Give files excluded under that configuration no edges.

---

## 6. Toolchain

| Rule | Evidence |
|---|---|
| `GOTOOLCHAIN` takes the forms `<name>`, `<name>+auto` and `<name>+path`. `auto` means `local+auto` and `path` means `local+path`. `local` always runs the bundled toolchain. With `+auto` or `+path`, the toolchain or go line of go.work (or of the main go.mod) can select a newer version: first an executable named `go1.N.P` from PATH, then (auto only) a download of module `golang.org/toolchain@v0.0.1-go1.N.P.GOOS-GOARCH`, always checked against sumdb. | D https://go.dev/doc/toolchain#select, #download |
| The default is `auto`, from `$GOROOT/go.env`, which is still read under GOENV=off. GOENV=off only disables the user's go env file. | P `cat $GOROOT/go.env`; `GOENV=off go env GOTOOLCHAIN` prints `auto` |
| **Exact errors under local:** <br>main module: `go: go.mod requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)` <br>newer patch: `go: go.mod requires go >= 1.27.9 (running go 1.27.2; GOTOOLCHAIN=local)` <br>go.work: `go: ../go.work requires go >= 1.28 (…)` <br>dependency: `go: example.com/newgo@v1.0.0 requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)` | P `p03_p04_p06.txt` (p06), `p02_workspace.txt` T15 |
| A newer `toolchain` line with an older `go` line is ignored under local. | P p06 |
| With `auto` and GOPROXY=off, the go command still tries: `go: downloading go1.28.1 (linux/amd64)` … `toolchain not available`. | P p06 |
| `go mod edit -json`, `go work edit -json` and `go env` still work with newer go lines; `go list`, `go list -m` and `go mod download` fail. | P `$W/p01-modfile/future` |
| **gopls inherits GOTOOLCHAIN** through `os.Environ()`. Under local, its load fails with the error above. Under auto, the go command it runs tries `downloading go1.28.0`. | P p06; S `$GP/internal/cache/view.go:362-369` |
| Version order: 1.21 < 1.21rc1 < 1.21rc2 < 1.21.0 < 1.21.1. Below 1.21, `1.N` comes after its rcs. | D doc/toolchain #version |

**CodeDoc should** set `GOTOOLCHAIN=local` for every go command and for gopls. In discovery, compare each go.mod and go.work `go` line with the pinned toolchain version using doc/toolchain ordering. A unit that needs a newer Go is reported with the go command's message and gets no edges. Dependency go lines are only visible in the module cache, so a newer dependency shows up as a load error (fail closed).

---

## 7. Safety

### 7.1 What runs and what gets written

| Command (1.27.2) | Programs run besides `go` | Writes | Network | Evidence |
|---|---|---|---|---|
| `go mod edit -json F`, `go work edit -json F` | none | none in the repo | none | P §1.3 |
| `go list` (readonly, `-buildvcs=false`, CGO_ENABLED=0) | none, unless GOCACHEPROG or `-toolexec` is set | GOCACHE (243–5,061 files); telemetry counters; **`<GOWORK>.sum` in workspace mode** | module fetches unless GOPROXY=off; **sumdb direct unless GOSUMDB=off**; VCS if `direct` or GONOPROXY | P `p07_safety.txt`, §8, §12 |
| `go list` with default `-buildvcs=auto` in a git repo with a main package, when the output can include Stale/BuildInfo (plain `-json`, any `-f` template, `-json=…,Stale`) | `git status --porcelain` and others (2–7 calls). A `-json=<field list without Stale>` makes 0 calls. | **`.git/index` rewritten**: go runs `git status` without `--no-optional-locks` | – | P `$W/transcripts/p07_gitindex.txt`; S `cmd/go/internal/vcs/vcs.go:231` |
| `go list -compiled=true` (gopls), CGO_ENABLED=1 | **gcc** (52 cold, 1 warm) and **pkg-config with the repo's `#cgo pkg-config:` args**; `-toolexec` program 8× | GOCACHE | – | P S1–S3 |
| `go mod download` | VCS tools with `direct`; also via the `,direct` fallback on proxy 404/410 | GOMODCACHE; go.sum (single-module) or go.work.sum | yes | P Prometheus `compliance/go.sum` modified (gomoddl-each); D ref/mod #private-module-privacy |
| `go mod tidy`, `go get`, `go work sync`, `go mod vendor`, `go work vendor` | VCS possible | **go.mod, go.sum, vendor/ in the repo** (work sync rewrites every used go.mod) | yes | D ref/mod #go-work-sync, #go-mod-tidy |
| `go generate` / `build` / `test` / `run` / `vet` | runs repo directives, compilers, binaries | – | – | – |
| any `go` command | – | telemetry counters in `$XDG_CONFIG_HOME/go/telemetry/local` (default `~/.config`). The `GOTELEMETRY` env var is *not* settable. | none in `local` mode | P `p07_safety.txt` telemetry; D `go help telemetry` |

### 7.2 gopls v0.23.0 → go/packages → `go list`

- **Flags.** `go list -e -json=<fields> -compiled=true -test=true -export=false -deps=true -find=false -buildvcs=false -pgo=off [buildFlags] -- <dir>/... builtin`. Mode = NeedName|Files|CompiledGoFiles|Imports|Deps|TypesSizes|Module|EmbedFiles|ForTest, plus TypecheckCgo; `Tests: true`. (S `$GP/internal/cache/load.go:349-381`; `$XT/go/packages/golist.go:862-889`)
- **Environment.** `os.Environ()` + the `env` setting + `GO111MODULE` (S `view.go:362-369`). **`GOPROXY=off` is appended for every load *except the initial workspace load*, which runs with `NetworkOK`.** So unless GOPROXY=off is already in gopls's own environment, the first load can download. (S `view.go:704`, `load.go:374-376`; the `allowImplicitNetworkAccess` setting was removed in v0.17: `$GP/doc/release/v0.17.0.md:72`)
- **Does gopls change go.mod or go.sum itself? No.** The automatic `go mod tidy` diagnostics run `go mod tidy -modfile=<tempdir>/go.mod` with GOPROXY=off and GOWORK=off (S `$GP/internal/cache/mod_tidy.go:100-115`). `go generate`, `go mod vendor`, `go get` and the dependency add/upgrade/remove edits run only as client-requested commands (S `$GP/internal/server/command.go:632,862,922`; command.go:922 is the only caller of `RunGoModUpdateCommands`, which works on a temp `-modfile` copy and returns edits). The only repo write is **`go.work.sum`**, through its loads in workspace mode (R2).
- **Other writes.** gopls has its own cache (`GOPLSCACHE`, default `os.UserCacheDir()/gopls`; S `$GP/internal/filecache/filecache.go:412`) and telemetry.

### 7.3 Checksums

- Lookup order:
  1. go.sum lines of the main module(s). In workspace mode that means every used module's go.sum plus `<GOWORK>.sum`.
  2. If missing: in single-module readonly mode, the error `missing go.sum entry for module providing package …` with no network (P `p07_safety.txt`). In workspace mode, a sumdb check, after which the line is **written to `<GOWORK>.sum`** (S `modfetch/fetch.go:863-908`).
- Sumdb is skipped for GOSUMDB=off and for paths matching GONOSUMDB/GOPRIVATE. `golang.org/toolchain` is always verified. (S `modfetch/sumdb.go:35-60`; D ref/mod #authenticating)
- **Sumdb transport.** Through the proxy if `<proxy>/sumdb/<name>/supported` exists; **directly to sum.golang.org when GOPROXY is `off` or `direct`** (S `sumdb.go:176-216`).
  - P: GOPROXY=off with 240 missing lines gave 644 direct GETs to sum.golang.org and wrote go.work.sum (`$W/p08-offline/N1x.err`).
  - In a no-network namespace, 217 of 755 listed packages failed with the misleading message "module lookup disabled by GOPROXY=off" (`N2.json`; pattern `./...` from the root). With all module patterns, real-repos measured 518 of 1,885 (`$R/real-repos/wsum2`, logs/wsum2-*).
  - Why Prometheus hits this: workspace mode needs a `/go.mod` checksum for every version in the *combined* module graph. The per-module go.sum files lack some of them. For example, `compliance/go.mod` requires `github.com/prometheus/client_golang/exp v0.0.0-20260820153414-89c60c055469`, and `compliance/go.sum` has only its zip hash, not its `/go.mod` hash. The repo also gitignores `/go.work.sum`. So a repo without a committed go.work.sum needs sumdb or `GOSUMDB=off` to load in workspace mode. real-repos confirmed that a generated go.work with `GOSUMDB=off GOPROXY=off` and the network blocked loads all 1,885 packages with 0 errors.
- Sumdb state lives in `$GOMODCACHE/cache/download/sumdb` (tiles) and **`$GOPATH/pkg/sumdb`** (the signed tree head) (S `sumdb.go:235-303`, `cfg.go:505`).
- GOINSECURE affects only direct HTTP fetches and does not disable sumdb (D `go help environment`).
- **`-modcacherw`** makes new module-cache directories writable, so `rm -rf` works. Without it, extracted modules are read-only (D ref/mod #module-cache, #build-commands).

### 7.4 Environment allowlist (for go commands and gopls)

| Variable | Offline (default) | Download (opt-in) | Why |
|---|---|---|---|
| PATH | toolchain `bin` + `/usr/bin:/bin` | same | gcc presence changes the CGO default; `go1.N` binaries on PATH are toolchain candidates |
| GOROOT, GOTOOLCHAIN | pinned, `local` | same | §6 |
| GOENV | `off` | `off` | user go env file can set GOPROXY/GOFLAGS/GOPRIVATE |
| GOFLAGS | `-mod=readonly -modcacherw -buildvcs=false` (vendored unit: `-mod=vendor …`) | same | P: inherited `-mod=mod` wrote go.sum; `-toolexec` ran 8 programs; without `-buildvcs=false`, go list runs `git status`, which rewrites `.git/index` |
| GOWORK | generated abs path, or `off` | same | R2 |
| GOPROXY | `off`, or `file://<user GOMODCACHE>/cache/download` (R8) | `https://proxy.golang.org` (no `,direct`) | the `,direct` fallback runs VCS |
| GOSUMDB | `off` | `sum.golang.org` | §7.3 |
| GOPRIVATE, GONOPROXY, GONOSUMDB, GOINSECURE | empty | empty, unless the user opts in | GONOPROXY forces direct VCS even under GOPROXY=off (P) |
| GOVCS | `*:off` | `*:off` | P: blocks git |
| GOAUTH | `off` | `off` | `git dir` and `command` modes run programs (D `go help goauth`) |
| GOCACHEPROG, GOEXPERIMENT, GODEBUG, CC, PKG_CONFIG, CGO_* | unset | unset | GOCACHEPROG executed (P) |
| CGO_ENABLED, GOOS, GOARCH, GOAMD64 | `0`, pinned | same | §5 |
| GOMODCACHE | user cache (read-only use) or OUTPUT_DIR | OUTPUT_DIR | §8, §12 |
| GOCACHE, GOPATH, TMPDIR, GOTMPDIR, GOPLSCACHE | under OUTPUT_DIR | same | build cache, sumdb tree head, temp files, gopls cache |
| XDG_CONFIG_HOME | OUTPUT_DIR dir with `go/telemetry/mode` = `off <date>` (run `go telemetry off` inside it once) | same | P: no counters written afterwards |

If CodeDoc itself runs git on the repo, it must use `GIT_OPTIONAL_LOCKS=0` or `git --no-optional-locks`. Plain `git status` rewrites `.git/index`; with optional locks off it does not (P `p07_gitindex.txt`, git 2.47.3).

With this environment, nothing is written into the repo, and the only writes outside OUTPUT_DIR are into a user GOMODCACHE when you choose to point GOMODCACHE at it:
- With a complete cache, offline `go list` wrote 0 entries, and a fully read-only cache worked (P `$W/p08-offline/R1.json`, `R2.json`, `mc-ro.before`/`mc-ro.after`).
- With missing modules, the go command creates empty `cache/download/<path>` directories. Use R8 to avoid even that.

---

## 8. Offline mode (copies of etcd and Prometheus; GOPROXY=off)

Files are in `$W/p08-offline/`, summarised by `$W/scripts/listsummary.py`. Each run used gopls's flags, except `-compiled=false`, and covered all module dirs. §12 repeats the check with `-compiled=true`.

| Case | Result |
|---|---|
| O1: etcd, workspace mode, **empty** GOMODCACHE | rc=0, 0.5 s. **391/391 in-repo import paths load with 0 errors.** All 110 reachable external packages report `module lookup disabled by GOPROXY=off`. 337 in-repo entries carry `DepsErrors` and 240 are `Incomplete`. Nothing written to the repo. |
| O3: etcd root module, GOWORK=off, empty cache | rc=0. 114/114 in-repo load; 76 external errors. stderr still prints misleading `go: downloading …` lines. |
| O0: etcd, cache from the §12 download | 1,067 entries, 0 errors. |
| O2a: zap's zip removed, `.mod` kept | Only 5 external packages fail. |
| O2b/O2c: **one `.mod` removed**, either cobra or `cloud.google.com/go/longrunning v0.4.1`, which provides no package | **Identical to an empty cache: all 110 external packages fail.** `go list -m all` fails too. |
| O4: same partial cache, GOWORK=off (root module and server module) | 0 errors. Lazy loading does not need graph-only `.mod` files. |
| Prometheus nested module `documentation/examples/remote_storage`, GOWORK=off | Its **own** packages fail offline until the zip of `github.com/prometheus/prometheus@v0.308.1` is cached. Having only the `.mod` is not enough: the go command must prove no other build-list module also provides the path ("exactly one module" rule). In workspace mode it loads. | 
| P1/P2: Prometheus workspace mode, full cache, no go.work.sum | Loads fine **and writes go.work.sum into the repo copy**. It succeeded with the cached sumdb tiles (P1; network use not traced). Without the tiles (P2), they were fetched from sum.golang.org despite GOPROXY=off (§7.3). |

Sources for the table: the nested-module rule is P `$W/p08-offline/amb/` and D ref/mod #resolve-pkg-mod; the go.work.sum row is P `P1.json`, `N1x.err`.

**Rules.**
- `go list -e` reports each unresolvable import as a package entry with `Error`.
- Importers get `DepsErrors` and `Incomplete`, but their file lists are complete.
- In-repo packages always load, except nested modules shadowed by a parent module that is not cached (Prometheus case above).
- Workspace mode needs every `.mod` of the pruned graph: etcd needed 2,330 `.mod` files but only 95 zips.

**CodeDoc should:**
1. Precheck the cache before starting gopls: for each go.work or go.mod, check that the needed `.mod` and zip files exist. One cheap way is `go list -m -e -json all` plus `go list -deps -json`, run offline; both are safe.
2. Report "external resolution disabled" rather than silently losing recall.
3. Prefer R8 for the offline mode.

---

## 9. The stdlib package list

**Algorithm** (P `$W/scripts/stdlist.py`; S `modload/search.go:57-68,165-170`; D `go help importpath`):
1. Walk `$GOROOT/src`, pruning `.x`, `_x`, `testdata` and nested go.mod dirs; `cmd/` is its own module.
2. Keep each dir with ≥ 1 included file under the §5 rules, test files included.
3. Drop `builtin`. Drop `runtime/cgo` when cgo is off.
4. The import path is the path relative to `src`.
5. `src/vendor/...` packages are std-internal (`vendor/golang.org/x/...`): 23 packages from 4 modules listed in `src/vendor/modules.txt` (x/crypto, x/net, x/sys, x/text). Std code imports them as `golang.org/x/...`.

**Result.** Identical to `go list std` on 7 targets: linux/amd64 cgo1 383, cgo0 381, windows 383, darwin/arm64 379, js/wasm 378, wasip1 377, plan9/386 377 (P `$W/p09-stdlib/`).

The target-independent superset ("any .go file") has 404 dirs; the 14 extra are experiment- or target-gated, for example `arena`, `simd/*`, `runtime/asan`, `crypto/boring`. The go command reserves every import path whose first element has no dot for std (D `go help importpath`).

**CodeDoc should** compute the target-specific list from the pinned toolchain's GOROOT. Use the 404-dir superset only to answer "is this path std?". Label definitions under `GOROOT/src/vendor` as std-internal, not as the x/ module.

---

## 10. GOPATH mode

- 1.27.2 still supports `GO111MODULE=off` for build commands. The probe resolved `example.com/gp/a`, plus a GOPATH-style `vendor/` dir, from `$GOPATH/src` (P `$W/p10-gopath/`).
- `go get` in GOPATH mode was removed in 1.22: "Other build commands … will continue to work indefinitely for legacy GOPATH programs" (D https://go.dev/doc/go1.22#go-command).
- Default module mode without a go.mod gives `go: go.mod file not found …`.
- gopls v0.23.0 still has a GOPATHView (S `$GP/internal/cache/view.go:296-325`).
- **Decision input.** Supporting it needs the repo placed at `GOPATH/src/<import path>`, and that import path is not derivable from files (no go.mod). Its semantics differ from module mode: nested vendor dirs, and no versions.

Recommendation: **unsupported, documented limit.** Repos without go.mod are reported as having no unit.

---

## 11. Dependency identity for labels

| Fact | Evidence |
|---|---|
| **Package → module.** The go command picks the module in the build list that contains the directory. "If exactly one module … provides the package, that module is used"; zero or two is an error. Build constraints are ignored for this check; one `.go` file is enough. In a successful build that is the longest module-path prefix whose tree contains the dir (nested modules are cut out of zips). | D ref/mod #resolve-pkg-mod |
| **Authoritative offline source:** `go list -deps -json` gives `Module{Path, Version, Replace, Main, Dir}` per package. | P §8, `$W/p11-identity/I3I4.txt` |
| `go list -m all` **fails offline** even when every package loads, because it needs a `.info` per module (92 `.info` vs 2,330 `.mod` cached). `go list -m -e -json all` works: 754 modules, all with Path, Version, GoMod, GoVersion and Sum; 650 carry `Error` only for the missing Time/Dir. | P `$W/p11-identity/I1*.json/.err` |
| Replaced in-repo module, GOWORK=off: `go.etcd.io/etcd/api/v3@v3.7.2`, `Replace=./api`, `Dir=<repo>/api/...`. In workspace mode the same module is `Main=true` with no Replace: workspace modules override go.mod replaces. | P `$W/p11-identity/I3I4.txt` |
| Major version: `/vN` suffix (N ≥ 2) in the module path, so it is part of the import path. `gopkg.in/x.vN` uses a dot suffix even at v0/v1. `+incompatible` marks v2+ releases without go.mod. etcd's build list contains `gopkg.in/check.v1`, `github.com/evanphx/json-patch@v4.12.0+incompatible`, and others. | D ref/mod #major-version-suffixes; P I1c |
| **Cache layout:** `$GOMODCACHE/<esc(path)>@<esc(version)>/` holds the extracted tree. `cache/download/<esc(path)>/@v/<v>.{info,mod,zip,ziphash,lock}` holds the GOPROXY-protocol files. `cache/download/sumdb/` holds sumdb tiles. In `esc`, each capital becomes `!` + lower case (`github.com/!azure/…`, `!kim!machine!gun`). | D ref/mod #module-cache; P Prometheus cache listing, `$W/proxy/example.com/!upper` |
| `golang.org/toolchain@v0.0.1-goX.Y.Z.GOOS-GOARCH` modules exist only for toolchain switching (never under local). The go command skips them when matching packages. | D doc/toolchain #download; S `modload/search.go:186` (`gover.IsToolchain`) |

**CodeDoc should** label external definitions as `module@version` (and the replacement, if any) taken from the go list `Module` of the package that contains the definition's file. For package lookup by file path, `Dir` is the key, so classify the `realpath` against:
- in-repo module roots;
- the GOMODCACHE prefix, decoding `!`;
- `GOROOT/src` (with `vendor/` = std-internal);
- `<repo>/vendor/` (via modules.txt).

---

## 12. Download mode design input

Copies of each repo were made under `$W/p12-download/runs/<repo>-<variant>-<tag>/`, each with a fresh GOMODCACHE and GOCACHE. Network was available and other agents were busy, so times are noisy: repeats differed by ±25 %. Per-run details are in `$W/p12-download/runs/*/summary.txt`.

| Repo | Variant | Time t1 / t2 (s) | GOMODCACHE bytes | inodes | zips | `.mod` | Writes into repo copy |
|---|---|---|---|---|---|---|---|
| etcd (13 modules) | **list-genwork**: generated go.work outside repo, `go list -e -deps -test -compiled=false` over 13 dirs | 3.3 | 306,256,276 | 22,137 | 95 | 2,330 | none |
| etcd | list-ws: the repo's go.work | 3.7 / 4.6 | 306,256,276 | 22,137 | 95 | 2,330 | none (etcd's go.work.sum is complete) |
| etcd | `go mod download` in workspace mode | 16.6 / 14.0 | 3,687,106,671 | 126,864 | 741 | 2,485 | none |
| etcd | go list per module, GOWORK=off | 2.7 | 248,010,982 | 15,805 | 91 | 91 | none |
| etcd | `go mod download` per module, GOWORK=off | 8.1 | 451,386,248 | 46,549 | 303 | 2,330 | none |
| Prometheus (5 modules) | **list-genwork** | 12.2 | 1,761,166,681 | 90,476 | 257 | 866 | none (240-line go.work.sum written to the generated dir) |
| Prometheus | list-ws | 13.3 / 9.8 | 1,761,181,657 | 90,483 | 257 | 866 | **new go.work.sum (gitignored)** |
| Prometheus | `go mod download` in workspace mode | 13.0 / 10.5 | 2,796,385,315 | 177,255 | 746 | 1,147 | **new go.work.sum** |
| Prometheus | go list per module, GOWORK=off | 19.4 | 2,445,662,654 | 106,804 | 313 | 341 | none |
| Prometheus | `go mod download` per module, GOWORK=off | 22.2 | 2,812,112,031 | 140,386 | 393 | 587 | **compliance/go.sum modified** |

Notes on the table:
- About 20 % of the bytes are zips; the rest is the extracted trees. etcd's list cache holds 64 MB under `cache/download`.
- Writes were detected with sha256 manifests of go.mod/go.sum/go.work/go.work.sum, plus an mtime scan of every file. `git status` misses Prometheus's change because `/go.work.sum` is gitignored.

**Afterwards, gopls's exact flags ran offline against the list-genwork caches with the generated go.work:**

| Repo | CGO_ENABLED | Entries | Errors | Time (s) | GOCACHE files written | Repo |
|---|---|---|---|---|---|---|
| etcd | 0 | 1,060 | 0 | 0.8 | 3,374 | unchanged |
| etcd | 1 | 1,068 | 0 | 1.2 | 3,445 | unchanged |
| Prometheus | 0 | 1,885 | 0 | 1.7 | 5,016 | unchanged |
| Prometheus | 1 | 1,886 | 0 | 2.1 | 5,061 | unchanged |

P: `$W/p12-download/verify/`.

**Transport, traced** with spy git/hg/svn/fossil/bzr on PATH, and the default `GOPROXY=https://proxy.golang.org,direct`:
- the etcd list-genwork download made 2,516 HTTPS GETs, all to `proxy.golang.org`, and **0 VCS invocations** (P `$W/p12-download/spy/`);
- `go mod download` for etcd's `api` module (GOWORK=off) made 50 GETs, all to `proxy.golang.org`, and 0 VCS invocations, with go.mod and go.sum unchanged (P `$W/p12-download/spy-moddl/`).

**Recommended download step:**

```
GOWORK=<OUTPUT>/…/go.work  GOMODCACHE=<OUTPUT>/gomodcache  GOPROXY=https://proxy.golang.org  GOSUMDB=sum.golang.org
GOFLAGS='-mod=readonly -modcacherw -buildvcs=false'  CGO_ENABLED=0  GOOS/GOARCH/tags = pinned  GOVCS='*:off' GOAUTH=off …
go list -e -deps -test -compiled=false -json=ImportPath,Module,Error -- <abs module dir>/... [for each unit]
```

Why this form:
- It fetches exactly the modules that gopls's own `go list -deps -test` needs, plus the `.mod` files of the module graph.
- It writes only `<generated go.work>.sum` and OUTPUT_DIR.
- `go work sync`, `go mod tidy` and per-module `go mod download` all write into the repo, and `go mod download` fetches 1.6–12× more data (Prometheus 2.8 GB vs 1.76 GB; etcd 3.7 GB vs 306 MB).
- Vendored units skip the download.
- Single-module repos can use the same command with GOWORK=off: readonly never writes go.sum. Missing go.sum lines then become per-package errors instead of being fetched; use a one-`use` generated go.work if those modules should be fetched.

---

## 13. Open risks and unverified points

1. **Workspace-vs-single-module semantics** decide which code a call really reaches (R10). The spec must pick one policy per repo and use the same one for the runtime oracle. If it maps sibling-module calls to in-repo code where the go command uses a published version, that is a wrong edge under runtime truth.
2. **Dropping out-of-repo `use` entries, or neutralising out-of-repo replaces, changes MVS.** Units affected this way should be flagged as degraded. I did not measure how common this is in the wild.
3. **Workspace vendor plus a generated go.work.** `dir(GOWORK)/vendor` is what gets used. A symlink from OUTPUT_DIR to the repo's `go work vendor` directory works (now verified, §2 step 6). Locations then come back through the symlink path.
4. **CGO_ENABLED=0 drops cgo files** (0 in both trial repos) and switches `cgo`/`!cgo` variants: etcd has 4 fewer in-repo variants. A cgo-on opt-in must be documented as running gcc and pkg-config.
5. **Partial user caches fail closed but silently lose recall** in workspace mode (one missing `.mod` takes down all externals). A precheck and a report are needed.
6. **Not verified:**
   - whether the module-index walk differs from walkPkgs for a vendor-named package inside a *cached* module;
   - gopls behaviour under the exact allowlist environment (that is the engine-probe track);
   - whether go.mod `ignore` is honoured by gopls's `file=` queries for single files.
7. **Incident.** At 19:56:27 one of my probes ran `go list` in workspace mode inside the shared Prometheus clone and created `go.work.sum`. That probe was an `-f` template, so its VCS stamping probably also ran `git status` there. I moved the file to `$W/incident/` at 19:57. My verification `git status --porcelain --ignored` then rewrote `.git/index` in both shared clones (Prometheus 19:57:21, etcd 19:57:36). That is a stat-cache refresh only; tracked content is unchanged. My other go commands in the shared clones used GOWORK=off and Stale-free `-json` field lists: no git, no writes (P `p07_gitindex.txt`). The `repos/prometheus/go.work.sum` there now (mtime 20:19:19, 240 lines) is from engine-probe's session, which they reported; I left it untouched. I notified the lead, real-repos and engine-probe.

## Appendix: main sources (accessed 2026-10-13)

- Go Modules Reference, https://go.dev/ref/mod. Sections: #go-mod-file-lexical, #go-mod-file-grammar, #go-mod-file-go … #go-mod-file-ignore, #workspaces, #go-work-file-*, #build-commands, #vendoring, #resolve-pkg-mod, #major-version-suffixes, #go-mod-download, #go-work-sync, #module-cache, #authenticating, #checksum-database, #private-module-privacy, #vcs-govcs.
- Go Toolchains, https://go.dev/doc/toolchain. Sections: #version, #config, #select, #download.
- Release notes: https://go.dev/doc/go1.21, https://go.dev/doc/go1.22#go-command, https://go.dev/doc/go1.23 (godebug directive, telemetry), https://go.dev/doc/go1.24 (tool directive, GOAUTH), https://go.dev/doc/go1.25 (ignore directive, `work` pattern), https://go.dev/doc/go1.26, https://go.dev/doc/go1.27 (bzr removed).
- `go help` texts of 1.27.2: buildconstraint, packages, importpath, environment, goauth, vcs, telemetry, private, module-auth, mod download, list, work, modules, gopath (`$W/docs/help_*.txt`).
- Go 1.27.2 source (`$GR`): `cmd/go/internal/modload/{init.go,modfile.go,search.go,vendor.go}`, `cmd/go/internal/modfetch/{fetch.go,sumdb.go,repo.go,proxy.go}`, `cmd/go/internal/cfg/cfg.go`, `go/build/build.go`, `go/build/constraint/expr.go`, `internal/syslist/syslist.go`, `cmd/vendor/golang.org/x/mod/modfile/{read.go,rule.go,work.go}`.
- gopls v0.23.0 (`$GP`): `internal/cache/{load.go,view.go,snapshot.go,mod_tidy.go}`, `internal/server/command.go`, `internal/filecache/filecache.go`, `doc/release/v0.16.0.md`, `doc/release/v0.17.0.md`. go/packages: `$XT/go/packages/{golist.go,packages.go}`.
