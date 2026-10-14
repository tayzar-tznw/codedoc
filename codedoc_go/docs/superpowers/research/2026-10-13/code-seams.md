# code-seams: fork map of codedoc_python and the codedoc_go extractor decision

- **Date:** 2026-10-13 (JST). Research track `code-seams`, one of five.
- **Inputs:** `codedoc_python` at commit e308fb7 (branch `feat/codedoc-python`); spec v3 and master plan C1–C7; etcd v3.7.2 (68c065e) and Prometheus v3.15.0 (5241a27); Go 1.27.2; gopls v0.23.0.
- **Evidence:**
  - Probe code is in `$W/probes/`, raw outputs in `$W/out/`, with `$W = /home/admin/scratch/codedoc-go-research/code-seams`.
  - Each claim carries its basis: **[src]** source read (file:line), **[probe]** hands-on run (output path), **[doc]** cited documentation (Appendix B, accessed 2026-10-13).
- **Not done here:** gopls behaviour at scale (engine-probe), module policy (go-modules), runtime oracle (runtime-oracle). No file under `/home/admin/dev/code` was changed.

---

## 0. Findings that matter for the design

1. **The fork splits roughly 60/40.**
   - In the coverage run, 62 % of graph_generator's statements (6,572 of 10,544) sit in the eleven Python-specific modules (`treesitter_parser.py` and `py_*.py` except `py_model.py`). Nine are replaced; `py_session.py` and the `DeclIndex` part of `py_classify.py` are adapted.
   - The other 38 % carries over with edits: pipeline, resolution orchestration, evaluate, crossref, CLI, LSP client, schema, model, config and prompts.
   - The codedoc_python suite passes in a scratch copy: 2037 passed, 94.44 % coverage, 30.8 s [probe `out/pytest-codedoc_python.txt`].
   - `lsp_client.py` drives gopls v0.23.0 unchanged [probe `out/gopls_smoke.txt`].
2. **Four structural seams go beyond vocabulary.** Each needs a spec decision.
   - **a. Members are assumed to live in their class's file.** Go methods often do not:
     - etcd: 105 of 4,407 methods are declared outside their type's file;
     - Prometheus: 302 of 5,026 [probe `out/analyze-*.txt`].

     The assumption is in `pipeline._node_index`, `crossref._index`, `evaluate.node_labels` and `DeclIndex._owner` (§2.2).
   - **b. Import targets are assumed to be files.** gopls answers an import path with the package clause of *every* compiled file of the package.
     - My probe: 3 locations for a 3-file package [probe `out/gopls_smoke_multifile.txt`].
     - engine-probe confirmed it: 15 locations for `github.com/google/uuid`. The answer also depends on the importing file: the same import gives 1 location from a normal file and 3 from an external test package, because that resolves to the test variant (`testy.go`, `export_test.go`, `testy_test.go`) [engine-probe `out/import_def.txt`].

     The Python safe form would turn every such import into `ambiguous`. Imports need a package-level target, so I recommend a `Packages` node table keyed by (dir, clause name), which maps every variant to one node (§3.5).
   - **c. `<module>` is assumed to equal one file.** Keep a per-file pseudo-class `<package>`, which keeps IDs and per-file freshness intact. Group by an import-path column (§3.3).
   - **d. Topics link to files by basename.** That linking drops files whose stem is not unique: 42.8 % of etcd's files (`doc.go` ×67) and 23.3 % of Prometheus's [probe; src `pipeline.py:2076-2088`].
3. **Qualname grammar.** I propose `F`, `T.M`, `I.M`, `F.func1`, `F.func1.func2`, `init#1`, `<init>` and `<package>` (§3.2).
   - Receiver pointer-ness goes in a column, not the name.
   - Type parameters are never part of a name.
   - Result: no duplicate (relpath, qualname) pair in 1,840 files. Without the `#k` suffix on `init`, 6 files collide [probe].
4. **Extractor: use go/parser + go/ast in a stdlib-only Go helper.**
   - On files tree-sitter parses cleanly, both extractors agree on 100 % of declarations (36,474) and on 99.98 % of call sites (177,535 of 177,572).
   - But tree-sitter-go 0.25.0, the newest on PyPI (2025-08-29), fails on Go 1.26 `new(expr)`. That breaks 32 etcd files, `etcdserver/server.go` among them.
   - It also fails on Go 1.27 generic methods. Upstream has had no grammar change since 2025-08-29, and its open issue #195 asks whether the project is maintained (§4.1).
   - go/parser reports 0 errors on all 1,840 files. It is about 6× faster at parsing and 10–11× faster end to end.
5. **Positions.**
   - gopls v0.23.0 has no position-encoding negotiation: columns are always UTF-16, and lines split on `\n` only [src+probe].
   - The helper must call `PositionFor(p, false)`, because `//line` directives move `Position()` [probe `out/linedir.txt`].
6. **Size estimate.**
   - Python side: about 8.2–10.7k lines, plus a 0.9–1.3k-line Go helper. The baseline is 15.7k.
   - Tests: 13–17k lines. Of the baseline's 18.6k, 2.8k are kept, 5.9k adapted and 10.0k replaced (§6).

---

## 1. Fork map

**Verdicts:**
- **keep:** copy, cosmetic edits only;
- **adapt:** structure kept, named parts change;
- **replace:** Go-specific rewrite;
- **drop:** not carried over.

**Coupling to Python:**
- **none;**
- **low:** names and strings;
- **medium:** Python vocabulary or data shapes, with neutral logic;
- **high:** the logic is Python semantics.

**Line counts:** from `wc -l` at e308fb7. Statement counts come from the coverage run. No file is dropped outright; the Python-only parts inside kept files are.

### 1.1 `graph_generator/` (15,747 lines)

| File | Lines (stmts) | What it does | Verdict | Coupling | What changes for Go |
|---|---|---|---|---|---|
| `__init__.py` | 0 | Package marker | keep | none | — |
| `__main__.py` | 623 (382) | CLI: init, analyze, generate, validate, crossref, evaluate, setup | adapt | low | Fixture names :33; phase label :45; description :525; `--provision-env` help :541-542; `evaluate --env` help :586 |
| `config.py` | 89 (40) | `.env` → settings | adapt | medium | `SOURCE_EXTENSIONS` :74. `SKIP_DIRS` :79-84 move to the go command's rules. `PY_LSP_*` :53-60, `PY_ENV_*`/`PEER_REPOS` :67-70 and the DB default :39 change |
| `prompts.py` | 147 (7) | Japanese Gemini prompts | adapt | low | `fee.py`/`billing.py` examples :79-81, :145. Ask for repo-relative paths in `linked_files` (finding 2d) |
| `py_model.py` | 197 (128) | C1 shared types and vocabulary | adapt | medium | `Unit`/`Env`/`Dist` :57-88; vocabulary tuples :14-29; `normalize_dist` (PEP 503) :39; `External.evidence` :158; `python_version` :98-99, :128 |
| `pipeline.py` | 2,499 (1,587) | Phases 1–10, freshness, node/edge builders | adapt | medium | §2. Phases 2–6 and 10 (lines 1036-1710, 2340-2451) do not change |
| `treesitter_parser.py` | 2,080 (1,490) | Python extractor, PEP 263 decoding, chunker | replace | high | Go helper plus a thin Python loader. The chunker uses top-level declaration spans from the helper |
| `py_units.py` | 774 (605) | Units, names, import roots, dependencies, module names, origin | replace | high | Module ownership from go.mod/go.work, import paths, origin |
| `py_envs.py` | 777 (550) | Interpreters, environments, distributions, typeshed lists | replace | high | GOROOT, GOMODCACHE and vendor reading; build configuration |
| `py_layout.py` | 586 (416) | Providers, duplicates, execution environments, symlink views, pyrightconfig | replace | high | gopls session groups (go.work or module roots) and build flags. No views or execution environments |
| `lsp_client.py` | 334 (229) | JSON-RPC client over stdio | keep | none | Verified against gopls (initialize, didOpen, definition, implementation, stop). Only the docstring names pyright (:3, :7, :11) |
| `py_session.py` | 225 (137) | pyright session | adapt | medium | Skeleton kept: start, stop, open/close, location parsing. Changes: settings :38-39, env removal :41-42, readiness and version regexes :45-47, `python` hover block :47, languageId :192, `positionEncodings` :26. Add `implementation`. Read the version from the `serverInfo` JSON |
| `py_classify.py` | 679 (483) | `DeclIndex` and location rows 1–10 | adapt (`DeclIndex`), replace (rows) | high | Rows become GOROOT `builtin/builtin.go`, GOROOT stdlib, `GOMODCACHE/<module>@<ver>`, vendor, replace dirs, peers, in-repo, unscanned. The `DeclIndex` name-range index is reusable. Its module (0,0) hit and `module_body` logic change |
| `py_semantics.py` | 963 (705) | Hover parsing, MRO, H1–H5 | replace | high | No MRO, decorators or dunders. Add interface method sets and embedding promotion |
| `py_rules.py` | 873 (601) | R1–R11, U1, receiver union check | replace | high | Interface dispatch upgrade (U1 analog), method values, conversions, calls of func literals |
| `py_gates.py` | 1,365 (945) | Binding index, condition evaluator, G1–G9 | replace; keep the ~100-line frame | high | Keep `apply_gates` and G1/G2/G7/G8/G9 (:968-1040). G3–G6 have no Go counterpart. Add a build-excluded gate |
| `py_importtable.py` | 398 (310) | Fallback without pyright | replace | high | Package-qualified calls resolve from syntax (§3.6) |
| `resolution.py` | 1,057 (649) | Phase 1.6 orchestration and artifact | adapt | medium | About 60 % is neutral (§2.3). The waves, class model and peer parsing change |
| `setup_spanner_graph.py` | 445 (204) | `NODE_SPEC`/`EDGE_SPEC`, DDL, migrate, verify | adapt | low | Columns :49-128; possibly a `Packages` table |
| `crossref.py` | 406 (248) | Peers and cross-repo edges | adapt | medium | §2.1 and §2.2 |
| `evaluate.py` | 784 (498) | No-GCP scoring gate | adapt | medium | §2.1. The verdict and metric code (:104-468) is neutral |
| `py_provision.py` | 446 (330) | uv wheel provisioning | replace | high | Opt-in `go mod download` into an isolated GOMODCACHE (go-modules track) |

### 1.2 Query stack, scripts, deploy

| Path | Lines | What it does | Verdict | Coupling | Notes |
|---|---|---|---|---|---|
| `graph_query_agent/agent.py` | 273 | Two ADK agents; schema instruction; GQL cookbook | adapt | medium | Rewrite the `_SCHEMA` text (:98-162) and the cookbook (:166-214). Keep the InstructionProvider (:233-236), which is harmless without braces |
| `graph_query_agent/config.py`, `__init__.py` | 31, 1 | Root `.env` settings | keep | low | DB default :27 |
| `mcp_server/` (server, runner, sessions, `__main__`, `__init__`) | 317 | FastMCP wrapper | keep | none | — |
| `mcp_server/deploy.sh`, `cloudbuild.yaml`, `Dockerfile`, `requirements.txt` | 391 | Cloud Run deploy | keep | low | Names `codedoc-python(-mcp)` at deploy.sh:15, 19, 62, 70, 105 and cloudbuild.yaml:7, 13 |
| `mcp_server/README.md` | 273 | English deploy guide | adapt | low | 29 Python-specific terms |
| `webapp/main.py`, `Dockerfile`, `requirements.txt`, `frontend/*` | 240 + 36 + about 1.2k (without `package-lock.json`) | Docs viewer and chat UI | keep | none | — |
| `Dockerfile` (root), `.dockerignore`, `.gcloudignore`, `.mcp.json` | 30, 31, 21, 7 | Webapp image and tool config | keep | none | The Python base image is the tool's own runtime |
| `scripts/doccheck.py` | 260 | Docs gate: links, CLI terms, config keys, tables, GQL, prose | keep | none | Placeholder `〔16で記入〕` :35 and doc names :30-33 |
| `scripts/gql_lint.py` | 172 | GQL pattern lint | keep | none | Its `_TOKEN` regex rejects `{}` inside property maps, so Go qualnames should avoid braces |
| `scripts/verification_table.py` | 67 | Verification rows from evaluate | keep | none | — |
| `requirements.txt`, `pytest.ini` | 129, 6 | Pins and markers | adapt | low | Drop `pyright`, `nodejs-wheel-binaries`, `tree-sitter*` and `PyYAML` (if unused). Keep the markers and the 85 % gate |
| `.env.example` | 60 | Settings template | adapt | medium | Line 2, :24, :33-44 (`PY_LSP_*`), :49-57 (`PY_ENV_*`) |
| `sample_prompt.txt` | 8 | Japanese sample questions | adapt | low | `.py` paths |

### 1.3 Docs (Japanese, MECE five-file set) and adapted English files

| File | Lines | Python-term hits¹ | Verdict | Notes |
|---|---|---|---|---|
| `README.md` | 438 | 108 | adapt (rewrite content) | Keep the headings doccheck needs (`## 設定リファレンス`) and the CLI table. Replace "パッケージマネージャー対応" and "環境のプロビジョニング" with modules, offline mode and download |
| `docs/ARCHITECTURE.md` | 196 | 50 | adapt | Section skeleton (§1–12) reusable |
| `docs/LSP_RESOLUTION.md` | 399 | 66 | replace | pyright, environments, rules and gates are Python-specific |
| `docs/GRAPH_SCHEMA.md` | 284 | 44 | adapt | Keep `## 2. ノード表` and `## 3. エッジ表`, which doccheck uses. Replace "qualname の文法" |
| `docs/CROSS_PROJECT.md` | 165 | 26 | adapt | Replace distributions with modules; replace direct_url.json with replace/go.work |
| `CLAUDE.md` | 83 | 36 | adapt | English |
| `.claude/skills/codedoc-query/SKILL.md` | 172 | 18 | adapt | Lines 81-130 describe the Python graph |

¹ `grep -c -i` for python, pyright, pep, `.py`, decorat, metaclass, `<module>`, distribution, uv, wheel, venv and typeshed (by line).

### 1.4 `tests/` and `test_codes/`

Per-file verdicts for all 99 test files are in `$W/out/test_verdicts.md`; the imports per file are in `$W/out/test_imports.txt`. Summary:

| Group (pattern) | Files | Lines | Verdict | Why |
|---|---|---|---|---|
| Fakes and infrastructure: `conftest.py`, `fake_lsp_server.py` | 2 | 590 | keep | Gemini/Spanner/vertexai/sleep/ADC fakes are neutral. Replace `SAMPLE_TREE` (`conftest.py:231-243`). The fake server needs `implementation`, `serverInfo` and gopls configuration sections (:68-69, :136-138) |
| Neutral surfaces: `test_lsp_client`, `test_setup_spanner*`, `test_validate`, `test_gql_lint`, `test_doccheck`, `test_verification_table`, `test_graph_checkpoint`, `test_pipeline_docs`, `test_prompts`, `test_config`, `test_cli`, `test_deploy_script`, `test_surface*`, `test_crossref_peers`, `test_entry_points_env`, `test_spanner_project` | 18 | 2,200 | keep (light edits) | Names and keys change; the logic tested is neutral |
| Graph, pipeline, crossref, evaluate, agent, docs, resolution orchestration, session (`test_graph_*`, `test_pipeline_graph`, `test_phase1*`, `test_scan`, `test_entities_store`, `test_crossref`, `test_evaluate_*`, `test_agent_instruction`, `test_doc_*`, `test_skill_doc`, `test_py_model`, `test_resolution*`, `test_py_session`, `test_lsp_integration`, `graph_samples.py`, `resolution_fakes.py`) | 29 | 5,884 | adapt | Same tests with Go-shaped entities, records and vocabulary. About half of `test_resolution_run.py` (1,094 lines) is runner-neutral |
| Extractor (`test_treesitter_*`) | 11 | 1,859 | replace | Helper tests move into Go (`go test` with golden files), plus Python loader tests |
| Discovery, layout, provisioning (`test_py_units`, `test_py_envs`, `test_py_layout`, `test_py_provision`) | 4 | 2,346 | replace | Go module and build-configuration discovery |
| Classification and semantics (`test_py_classify_*`, `test_py_semantics_*`, `decl_builders.py`) | 12 | 1,831 | replace | Go rows; interface and embedding model |
| Rules, gates, import table (`test_rules_*`, `test_gates_*`, `test_importtable_*`, the four `*_world*.py`/`rules_support.py`) | 21 | 2,654 | replace | Go rules and gates |
| pyright captures (`pyright_captures.py`, `test_pyright_fixtures.py`, `fixtures/pyright/1.1.414/*.json`) | 2 + 2 | 502 + 784 | replace | gopls captures (§5) |
| `test_codes/_gt/*` (generate_manifest 276, check_ground_truth 864, gt_lib 298, make_layout_envs 184, trace_oracle 559, tests 786) | 11 | 2,970 | replace; keep the snippet machinery | Snippet → line/col and manifest plumbing are text-level and reusable. `ast` anchors, PEP 3155 names, Python env layouts and the `sys.monitoring` oracle are Python-only |
| Fixtures F1/F2/F3 (`iryo-kanri-system`, `py_layouts`, `multi_repo`) | 131/90/32 | code 2,161/232/151; ground truth 6,313/1,724/1,222 | replace (port) | Port the domain and case IDs. F2 becomes a module-layout fixture (go.work, replace, vendor, nested modules, build tags) |

---

## 2. Python assumptions in shared code

### 2.1 By theme

| Theme | Where (file:line) | Go replacement |
|---|---|---|
| File extension and skipping | `config.py:74` (`{".py"}`); `config.py:79-89` (venv, env, build, dist, site-packages, `__pypackages__`, `.egg-info`); `pipeline.py:683-686` (`pyvenv.cfg`/`conda-meta` dirs); `py_units.py:747`; `py_classify.py:438-441`; `py_layout.py:26`, `:485` (`.py/.pyi/.so/.pyd`) | `{".go"}`. Use the go command's skip rules: `vendor`, `testdata`, `_`/`.` prefixes, nested go.mod, go.mod `ignore` [doc `go help packages`]. Python's `build`/`dist`/`env` would wrongly skip legal Go package dirs; none occur in etcd or Prometheus [probe] |
| Source decoding | `pipeline.py:139-143`, `treesitter_parser.py:49-100` (PEP 263, cp932, BOM stripped at :89) | Go source is UTF-8 by spec. Do not strip the BOM: both parsers count its 3 bytes in columns [probe `samples/bom.go`]. Invalid UTF-8 is an error in go/parser (2 errors), while tree-sitter is silent [probe] |
| Module and qualname grammar | `py_model.py:44-46` (`fq` = `module.qualname`); `pipeline.py:1815-1818` (`<module>`, lambda signatures); `pipeline.py:1824-1826`; `pipeline.py:1889-1912` (keys); `crossref.py:147-150` (strips the `module.` prefix); `crossref.py:191`, `:239-243` (`<module>`); `evaluate.py:105`, `:573`; `resolution.py:309-327` (metaclass pseudo-site); `treesitter_parser.py:558` (`<locals>`), `:744` (`<lambda@L>`), `:281-283` (`{get}`); `agent.py:96-97`, `:101`, `:128-131`; `gql_lint.py` `_TOKEN` (no braces) | §3.2. `fq` = import path + "." + qualname, e.g. `go.etcd.io/etcd/server/v3/etcdserver.EtcdServer.Start`. Prefix stripping still works because import paths never end in "." |
| PEP 503 distribution keys | `py_model.py:39-41` (`normalize_dist`); `py_model.py:58` (`Unit.key`); `pipeline.py:1782-1786`; `evaluate.py:29`, `:109`, `:114`, `:524`; `agent.py:102` | Module path, case-sensitive and not normalized. The module cache escapes upper case as `!x` when it maps paths |
| `units.json` schema | C3; `py_model.py:57-140` (managers, import_roots, declared_deps, requires_python, entry_points, lock_file; Env kinds venv/conda/pixi/pep582; Dist RECORD/direct_url; ExecEnv; Layout providers/duplicates/views); `pipeline.py:742-761`, `:954-955` | A modules artifact: module path, dir, go/toolchain lines, requires, replace targets (in-repo, peer, outside), go.work membership, vendor state, build configuration (GOOS, GOARCH, tags, cgo), gopls groups. Fields are owned by the go-modules track |
| Env handling and provisioning | `config.py:53-70`; `pipeline.py:925-973` (typeshed, stdlib, stub_distributions, build_layout, provision_envs; prints "pyright" :971); `pipeline.py:1015-1033`; `py_session.py:41-42` (removes `VIRTUAL_ENV`…); `evaluate.py:476-534` (pins Python 3.12, `make_empty_env`, wheels); `crossref.py:330-354` (`read_dists`, pep582); `__main__.py:281-283` | Set rather than strip: `GOFLAGS`, `GOWORK`, `GOPROXY=off` offline, `GOTOOLCHAIN=local`, `GOMODCACHE`, `GOOS`/`GOARCH`/tags. Opt-in download into an isolated cache |
| Site kinds | `py_model.py:16-18`; `resolution.py:73` (`RULE_SITE_KINDS`); `pipeline.py:2142-2165`; `evaluate.py:134-140` (`call`/`decorator`); `crossref.py:35`, `:170-176`; `agent.py:139` | `call` (ident or selector, with index or instantiation wrappers), `embed`, `import`. No `decorator`/`operator`/`subscript`/`with`/`attribute_*` (§3.6) |
| Statuses, provenance, reasons | `py_model.py:14-29`; `resolution.py:75-77` (`stub`/`installed` evidence); `agent.py:140-156`; docs | The six statuses stay. Python-only reasons: `stdlib-shadow`, `duplicate-package`, `decorated`, `dispatch`, `cls-dispatch`, `conditional-binding`, `stub-only`. Rule names are Python-only |
| Node columns | `setup_spanner_graph.py:48-70`; `pipeline.py:1789` (`_CALLABLE_KINDS`), `:1799-1821` (`_decorators`, `_signature` in Python syntax), `:1844-1854` (`[project.scripts]` entry points), `:1931-1967`; `agent.py:105-107`, `:128-133`; `py_units.py:767-774` (origin package/script/test) | §3.5 |
| Constructor and class targets | `pipeline.py:2142-2143` (no `__init__` edge); `crossref.py:249-250` (`new`); `evaluate.py:155` | Conversion `T(x)` resolves to a type: a class target with no `MethodCalls`. Composite literals are not call sites |
| Inheritance | `pipeline.py:2186-2199` (`_child_class` from `bases`); `pipeline.py:2214-2239` (`extends`); `crossref.py:127-129`, `:141-144`; `agent.py:117`, `:124` | Embedding (struct or interface): kind `embeds`. It is not inheritance (promotion only) |
| Imports | `pipeline.py:2202-2211`, `:2242-2270` (target = file; `type_checking`); `crossref.py:241-243` (`<module>` → `<module>`); `evaluate.py:153-154`, `:370-372` (`imports_of` by file) | A package target (finding 2b). No `TYPE_CHECKING` analog. Blank imports (`_`) still run `init`: 29 in etcd, 77 in Prometheus [probe] |
| Evaluate fixtures and metrics | `evaluate.py:32-38`, `:33-34` (3.12/Linux), `:625-629` (uv hints); `__main__.py:33`, `:583` | Same metrics. Pin GOOS/GOARCH, the go version and tags instead of Python 3.12 |
| Doc checks | `scripts/doccheck.py` is neutral; `check_config` parses the tool's own `config.py` (still Python) | Keep |
| Gemini topic → file linking | `prompts.py:79-81`; `pipeline.py:2076-2088` (unique stem only) | Use paths. Non-unique stems: etcd 475/1,109, Prometheus 170/731 [probe] |

### 2.2 Structural assumptions that are not vocabulary

| Assumption | Where | Go fact | Consequence |
|---|---|---|---|
| A member is listed under its class in the same file record | `pipeline.py:1891-1912`; `crossref.py:130-131`, `:246-251`; `evaluate.py:421-429`; `py_classify.py:90-92` | Methods outside their type's file: etcd 105 of 4,407, Prometheus 302 of 5,026. Twin receiver types in Prometheus: 128 methods. Of those, 114 resolve to the same-file twin and 14 to the single declaration included in the linux/amd64 build. So 0 stay unresolved [probe `out/analyze2-*.txt`] | The entity contract must let a method name a receiver type in another file. `ClassDefinesMethod` crosses files. The method ID still uses the method's own file |
| An import resolves to one file | `pipeline.py:2262-2269` (also feeds `FileDependsOn`) | gopls returns one package clause per compiled file of the package. From an external test package that file set includes the in-package `_test.go` files [probe; engine-probe `out/import_def.txt`] | Needs a package node keyed by (dir, clause name), or the gates would make every import ambiguous. All three test-variant files carry `package testy`, so the key is variant-independent |
| One pseudo-class per file covers top-level code | spec §4.1; `crossref.py:242-243` | A package spans files; package-level var initializers run in the package initializer | §3.3 |
| One file is parsed alone (two-pass `SiteFilters` aside) | `pipeline.py:854-922` | Go needs no dunder or property filter. Receiver ownership and build inclusion need the package's file set and the build configuration | Phase 1.5 keeps per-file records but adds a package pass. The build configuration must enter the reuse stamp (the `filters_hash` analog) |
| A rule wave needs base and decorator answers first | `resolution.py:6-10`, `:668-682`, `:903-924` | No MRO or decorators. Interface dispatch needs implementations, which gopls `implementation` returns, including stdlib types (`reflect.StructTag.Get`) [probe] | Wave 1 shrinks to embeds, or disappears |
| The version comes from a log line | `py_session.py:45-46`, `:173-174` | gopls sends `serverInfo.version` as a JSON string, `"Version":"v0.23.0"` [probe] | Parse the JSON |

### 2.3 Neutral parts of `resolution.py` and `pipeline.py` that carry over unchanged

- **resolution.py:**
  - `ResolutionConfig`, `StaleEntitiesError`, `lsp_command`, `write_json_atomic`, `resolutions_hash` (:80-134);
  - the safe-form record builder for G1/G2/G7/G8 (:142-216);
  - stats and heaviest-first ordering (:330-356);
  - `run_resolution` (:399-428);
  - the group runner lifecycle: start, restart once, deferred serial retry, fallback mode, five-timeout guard (:537-651);
  - the waves and stale marking (:853-901);
  - the artifact (:927-963).
- **pipeline.py:**
  - throttle, Gemini calls, checkpoints, `ensure_fresh`, the graph fingerprint (:95-667);
  - the scan core (:674-739);
  - Phases 2–6 (:1036-1710);
  - `_make_id`, `_edge_id`, `_claim_id`, `_batch_insert` (:1717-1761, :1829-1841);
  - `_EdgeSink` and `_possibly` (:2049-2125);
  - Phase 10 and the orchestrator (:2340-2499).

---

## 3. Python concepts mapped to Go

### 3.1 Units, packages and origin (measured on etcd / Prometheus)

| Python | Go | Measured fact [probe `out/analyze-*.txt`] |
|---|---|---|
| unit / distribution (PEP 503 key) | module; key = module path from go.mod | 13 / 5 modules; both repos have a `go.work` |
| unit ownership (deepest root) | The deepest go.mod above the file owns it, unless a skip rule excludes the file | — |
| import roots, `module` (dotted) | Package import path = module path + relative dir; plus a `package` column for the clause name | Clause name ≠ last path element: 9 / 2. Of these, 5 / 1 are a major-version suffix (`client/v3` → `clientv3`), the rest are other names (`httptypes`, `io_prometheus_client`) |
| `origin` = script | `command` (package main) | 18 / 9 main dirs; 18 / 9 `func main` |
| `origin` = test | `test` (`_test.go`, same package) and `xtest` (external `<pkg>_test` package; `go list` gives it the import path `<dir path>_test` [probe `out/go_list_xtest.txt`]) | 418 / 278 `_test.go` files; 20 / 5 dirs with an external test package |
| one package per dir | Several clause names per dir occur (generators behind `//go:build ignore` or tools tags) | 1 / 0 dirs (`tools/mod`: `mod`, `libs`, `tools`) |
| interpreter version (known or assumed) | go directive / toolchain; GOOS, GOARCH, tags, cgo (the pinned build configuration) | `//go:build` files: 73 / 78. Excluded under linux/amd64: 29 files with 221 call sites, and 37 files with 718 call sites |
| site-packages, typeshed | GOROOT/src (stdlib), `GOROOT/src/builtin/builtin.go` (builtins: `len`, `new`, `min`), GOMODCACHE, `vendor/` | gopls answers `new(...)` in `builtin.go:235` [probe] |
| `[project.scripts]` entry points | Command name of a `func main` (the dir name for `go install`) | — |
| generated code (not modelled in Python) | `ast.IsGenerated` | 18 / 9 files; 4,447 / 3,650 call sites |

### 3.2 Qualname grammar (proposal)

| Declaration | Qualname | From the probes | Runtime name, go1.27.2 [probe `out/runtime_names.txt`] |
|---|---|---|---|
| Function | `F` | `Map` | `pkg.F`; generic `pkg.Gen[...]` |
| Method, value receiver | `T.M` | `Pair.Key` | `pkg.T.Val` |
| Method, pointer receiver | `T.M`; column `receiver` = `*T` | `Base.Hello` | `pkg.(*T).Ptr` |
| Method of a generic type | `T.M`, no type params | `List.Push` (receiver `*List[T]`) | `pkg.(*G[...]).Ptr` |
| Generic method (Go 1.27) | `T.M` | `Rand.N` | `pkg.(*T).Gen[...]` |
| Interface method | `I.M`, member of the interface's Classes node | `Number.String` | — |
| Closure (func literal) | `F.func1`; nested `F.func1.func1`; ordinal per parent in source order | `use.func1`, `<init>.func1` | `pkg.Clos.func1`, `pkg.Clos.func2.func1`; inlined copies differ (`pkg.init.func2.1`) |
| `init` function (several legal per file) | `init#1`, `init#2`, numbered 1-based per file | `init#1` | `pkg.init.0`…`init.2`, numbered across the package's files |
| Blank function `func _()` | `_#1` | — | — (0 in both repos) |
| Package-level var initializers | `<init>`: per-file pseudo-member, the caller of `var x = f()` | 1,317 / 1,233 call sites | Closures there: `pkg.init.funcN` |
| Top-level pseudo-class | `<package>` (kind `package`), per file | holds the file's functions, `init#k` and `<init>` | — |
| Type: struct, interface, alias, named | `T` | `List`, `GAlias` (generic alias) | — |
| Local type inside a function | Not a node; it cannot have methods | 37 / 163 local types | — |

Why:
- **Receiver kind is a column.** Switching `func (t T)` to `func (t *T)` is a routine refactor and should not change the node ID. A type cannot declare both, so `T.M` is unique per package (Go spec, method sets).
- **Type parameters stay out of names.** Receiver type-parameter names differ between methods (`List[T]`, `List[E]`).
- **Per-file numbering for `init#k`.** The runtime's package-wide `init.N` depends on other files and on build constraints, which would break per-file IDs and freshness. The `#` keeps it apart from runtime names; GQL strings accept it.
- **No braces anywhere.** `scripts/gql_lint.py` rejects `{` in property maps.
- **Runtime names cannot be matched by name.** Inlining renames closures, and `init` numbering is package-wide. The oracle should map frames by file:line, as codedoc_python's tracer does with `def` lines.

### 3.3 The `<module>` pseudo-class: per file

| Option | IDs (`_make_id(kind, repo, relpath, qualname)`) | Freshness | Edges | Verdict |
|---|---|---|---|---|
| Per file: `<package>` in each file | relpath = the file; no change to `_make_id` | A file's nodes depend only on that file, apart from the owner link | `FileDefinesClass` stays file → class | **Recommended** |
| Per package: one node per (dir, clause name) | Needs a dir relpath for the pseudo-class, while members keep file relpaths: mixed inputs | One edit changes a node built from many files | No file owns it | Rejected |

"List the functions of package X" is answered through `Files.module` (import path), or through `Packages` if that table is added.

### 3.4 ID scheme and collision analysis

`_make_id` stays as it is (`pipeline.py:1717-1725`). New kind if adopted: `("package", repo, dir relpath, clause name)`. `ID_SCHEME = 1` for the new variant.

| Collision case | etcd (1,109 files) | Prometheus (731 files) | Handling |
|---|---|---|---|
| Duplicate (relpath, qualname), proposed grammar | 0 | 0 | — |
| The same, without the `#k` suffix on `init` | 2 files | 4 files (generated `.pb.go`) | `init#k` |
| Blank functions | 0 | 0 | `_#k` |
| Build-tag twins: the same qualname in two or more files of one package | 30 qualnames | 68 qualnames | Different relpaths, so different IDs. Add a `build_included` column for the pinned configuration. gopls answers only into included files |
| Twin types | 1 | 5 (`model/labels` ×3 implementations) | The same |
| Method declared outside its type's file | 105 | 302 | Owner link across files (§2.2) |
| Method whose receiver type has twins | 0 | 128 | Same-file twin, else the single included declaration. 0 left unresolved |
| Receiver type not found in the package | 0 | 0 | Fall back to the file's `<package>` and add a flag |
| Same qualname in different packages or dirs | — | — | relpath differs |
| Duplicates inside one file of an ignored or broken file | not measured (0 in the probes) | — | Keep `_claim_id` (`pipeline.py:1829-1841`), which raises on a collision. Or apply codedoc_python's merge-with-all-name-ranges rule |

### 3.5 Schema consequences (input for the spec)

| Table.column (Python) | Go proposal |
|---|---|
| `Files.origin` package/script/test | package / command / test / xtest |
| `Files.module`, `Files.distribution` | Import path; module path |
| new on `Files` | `package` (clause name), `generated`, `build_constraint`, `build_included` |
| `Classes.kind` class/protocol/abc/dataclass/enum/namedtuple/typeddict/exception/module | struct / interface / alias / named / package |
| `Classes.decorators`, `Classes.metaclass` | Drop; add `type_params` |
| `Methods.member_kind` function/method/classmethod/staticmethod/property_*/lambda/module_body | func / method / interface_method / init / closure / var_init |
| `Methods.decorators` | Drop; add `receiver` (`*T`) and `type_params` |
| `Methods.entry_points` | Command name for `func main` |
| `Methods.signature` | Go syntax, e.g. `func (s *EtcdServer) Start()` |
| `ClassInherits.kind` extends | embeds. `implements` only if it can be made certain (closed world plus stdlib types; see §2.2) |
| `FileImports` (Files → Files) | Files → `Packages` (new node table: package_id, import_path, name, dir, module, repo). Key: (dir, clause name), so an answer listing every compiled file, test variant included, maps to one node. `FileDependsOn` then comes from calls and embeds, or gets a package-level twin |
| `CrossRepoRef.kind` import/extends/new | import (package level) / embeds / new (conversion; composite literals have no site) |

### 3.6 Sites, shapes and the fallback mode (measured)

| Callee shape (go/parser) | etcd | Prometheus | Site? |
|---|---|---|---|
| selector `x.f(...)` | 52,464 | 82,796 | `call` |
| ident `f(...)` | 17,312 | 31,536 | `call` (includes builtins and conversions) |
| type literal `[]byte(x)` | 2,045 | 469 | No: a conversion with no token; counted |
| func literal `func(){..}()`, including `go`/`defer` | 638 | 558 | No token. The closure node runs here, so this is a syntactic edge candidate |
| `(*T)(x)` | 383 | 173 | No: a conversion; counted |
| call of a call `f()(x)` | 2 | 3 | Inner site only (C2's A3 rule) |
| Inside closures, as share of token calls | 11,891 (17.0 %) | 30,344 (26.5 %) | The caller is the closure node |

**Import-table mode in Go is strong for qualified calls.** `pkg.F` is unambiguous given the import path and the package's files, apart from build twins.
- **Call shares** [probe `out/analyze3-*.txt`]:
  - unqualified identifier calls: 24.8 % (etcd) and 27.6 % (Prometheus) of token call sites;
  - package-qualified selector calls: 34 % and 38 %;
  - method calls on values, which need types (gopls): 41 % and 35 %.
- **Package-qualified calls whose target package is in the repo:** 4,449 and 8,531, matched by clause name. Aliased imports add 2,077 and 881.
- **Import forms:**
  - dot imports: 0 in both repos;
  - cgo: 0 in both;
  - aliased imports: 655 and 228.

---

## 4. Extractor options

### 4.1 Versions and compatibility

| Item | Fact | Basis |
|---|---|---|
| Newest tree-sitter-go on PyPI | 0.25.0, uploaded 2025-08-29. `requires_dist: tree-sitter~=0.24` (extra `core`) | [doc] pypi.org/pypi/tree-sitter-go/json → `out/pypi-tree-sitter-go.json` |
| Compatibility with codedoc_python's tree-sitter pin | Loads in tree-sitter 0.25.2: ABI 15, inside the binding's range 13–15 | [probe] venv `$W/venv` |
| Newest tree-sitter on PyPI | 0.26.0 (2026-06-30). Not needed; the grammar is the limit | [doc] `out/pypi-tree-sitter.json` |
| Upstream maintenance | Last grammar change v0.25.0 (2025-08-29); last commit 2025-09-15 (CI). Issue #195 "Is this project maintained?" (2026-05-04). A community fork exists (alienvspredator/tree-sitter-go) | [doc] GitHub API → `out/gh-tsgo-*.json` |
| Go 1.26 `new(expr)` | Unsupported. PR #193 open since 2026-04-08; PR #199 closed unmerged 2026-10-08 | [doc] + [probe] |
| Go 1.27 generic methods | Unsupported. PR #198 open since 2026-08-17. The declaration becomes an ERROR node and the method disappears | [doc] + [probe] `samples/newsyntax.go` |
| Go 1.26/1.27 language | 1.26: `new(expr)`, self-referential constraints. 1.27 (August 2026): generic methods; promoted fields as struct literal keys; generalized inference | [doc] go.dev/doc/go1.26, go.dev/doc/go1.27 |
| go/parser in Go 1.27.2 | Parses generic methods and `new(expr)` with 0 errors. `go vet` type-checks them under `go 1.27`; under `go 1.26` it reports "generic method requires go1.27 or later" | [probe] |

### 4.2 Numbers on all 1,840 `.go` files

Three runs each on the shared 32-core host. A later re-run under load average about 6 moved individual parse times by −5 % to +20 %.

| Measure | go/parser helper (`probes/goextract`) | tree-sitter 0.25.2 + tree-sitter-go 0.25.0 (`probes/tsextract.py`) |
|---|---|---|
| etcd (1,109 files, 6.7 MB): parse | 145–151 ms | 877–883 ms |
| etcd: extraction walk | 59–62 ms | 1,662–1,710 ms |
| etcd: wall time (start, read, JSON out) | 0.275–0.281 s | 2.71–2.77 s |
| etcd: max RSS | 33.7 MB | 37.8 MB |
| Prometheus (731 files, 12.1 MB): parse | 234–260 ms | 1,414–1,430 ms |
| Prometheus: extraction walk | 67–70 ms | 2,887–2,901 ms |
| Prometheus: wall time | 0.386–0.418 s | 4.50–4.52 s |
| Prometheus: max RSS | 56.7 MB | 64.2 MB |
| Files with syntax errors | 0 / 1,840 | 32 / 1,840: all in etcd, all at a `new(expr)` line (32 of 32 checked); 8 non-test files |
| Error regions if codedoc_python's A4 rule applied | — | 334 of 512 declarations and 5,000 of 6,536 call sites in those 32 files. `server/etcdserver/server.go` is lost from line 1399 |
| Files where only go/parser reports errors | 0 in both repos. Probe files: invalid UTF-8 (2 errors, tree-sitter silent); lone `\r` (both fail) | |
| Helper build (stdlib only) | Cold 3.2–3.8 s (fresh GOCACHE, 54 MB); warm 66–127 ms | none (wheel) |

Sources: `out/time-*.json`; `out/compare-*.txt`.

### 4.3 Agreement on the 1,808 files tree-sitter parses cleanly

`probes/compare.py`: [probe `out/compare-*.txt`]

| Item | etcd | Prometheus |
|---|---|---|
| Declarations (kind, name, parent, name range, qualname, receiver, generic flag) | 16,529 / 16,529 | 19,945 / 19,945 |
| Imports (path, alias, position) | 6,698 / 6,698 | 6,587 / 6,587 |
| Package clause, `//go:build`, `// +build`, generated flag | 1,077 / 1,077 files | 731 / 731 files |
| Call sites with a token (name, range, caller, wrapper flag) | 63,238 / 63,240 | 114,297 / 114,332 |
| Generic declarations (type-parameterized functions and types, methods on generic types) | 23 / 23 (24 in all files) | 41 / 41 |
| Disagreements | 2: explicit instantiation `random.PickRandom[options.ClusterOptions](..)`, func-map call `connFaults[r](bc)` | 35, e.g. `errors.AsType[*exec.ExitError](err)`, `sigfs[infoName](s.Metric)` |

All 37 disagreements are the same ambiguity. Go syntax cannot tell `G[T](x)` (instantiated call), `m[k](x)` (index call) and `T[U](x)` (generic conversion) apart. go/parser emits `CallExpr{IndexExpr}`. tree-sitter guesses `type_conversion_expression{generic_type}`, so a tree-sitter extractor would have to treat that node as a candidate call site.

### 4.4 Position conventions

| Source | Line | Column | Line breaks | Basis |
|---|---|---|---|---|
| go/token `Position()` | 1-based | 1-based **bytes**, *adjusted by `//line`* | `\n` | [probe `out/linedir.txt`]: `gram.y:100` vs `PositionFor(p,false)` = `p.go:4:1` |
| tree-sitter `Point` | 0-based | 0-based bytes | `\n` | [probe] |
| gopls v0.23.0 (LSP) | 0-based | **UTF-16 always**: `positionEncoding` absent even when the client offers `utf-8` first | `\n` only (`internal/protocol/mapper.go`, `initLines`); a `\r` before `\n` is stepped over | [src gopls module] + [probe]: definition at UTF-16 col 27 resolves, at byte col 33 returns `[]`. engine-probe reproduced it (`héllo`: UTF-16 col 16 resolves, UTF-8 col 22 gives `[]`; `engine-probe/out/utf8_check.txt`) |
| entities (keep codedoc_python's rule) | 1-based | 0-based UTF-16 of the raw text | `\n` | C2 |

Notes:
- **Real repos rarely exercise the conversion.** 0 call sites in either repo have a UTF-16 column different from the byte column; non-ASCII appears only on 41 and 349 lines, in comments and strings [probe]. The fixtures must therefore carry non-ASCII identifiers. Japanese identifiers parse identically in both extractors [probe `samples/japanese.go`].
- **`//line` directives:** none in etcd or Prometheus [probe], but goyacc and cgo emit them. The helper must use `PositionFor(p, false)`.
- **BOM:** both extractors count its bytes in columns. Whether gopls counts U+FEFF as one UTF-16 unit is **unverified**.

### 4.5 What each option can produce for the entities contract

| Need | go/parser + go/ast | tree-sitter-go 0.25.0 |
|---|---|---|
| Declarations with name ranges (func, method, type, alias, var, const, field) | yes | yes, except syntax it cannot parse |
| Call sites with callee-token ranges | yes. `CallExpr`; instantiation through `IndexExpr`/`IndexListExpr` | yes, plus a `type_conversion_expression` case (37 misses otherwise) |
| Imports (path, alias, position) | yes | yes |
| Receivers (base, pointer, generic) | yes | yes |
| Embedded fields (struct, interface; type-set terms) | yes | yes |
| Interface method specs | yes | yes (`method_elem`) |
| Type parameters (functions, types, aliases, methods) | yes, including Go 1.27 methods | no for methods |
| Build constraints | yes. `go/build/constraint` parses and evaluates; `build.Context.MatchFile` adds GOOS/GOARCH file-name rules | Comment text only; evaluation would be new code |
| Package clause | yes | yes |
| `// Code generated … DO NOT EDIT.` | `ast.IsGenerated` | regex over the comments before `package` |
| Syntax errors | `scanner.ErrorList` from the toolchain's own parser | ERROR/MISSING nodes; the grammar lags the language |
| Callers: functions, methods, closures, `<init>`, `init#k` | yes (probe) | yes (probe) |

### 4.6 Recommendation: option (b), a go/parser helper

1. **Correct on the target repos.** 0 error files versus 32, and 100 % declaration agreement where tree-sitter parses at all.
2. **Current with the language.** It is the toolchain's own parser, the same toolchain gopls needs. If a repo's `go` line exceeds the toolchain, the helper and gopls fail visibly together, instead of drifting apart silently. tree-sitter-go lags two language releases, and its fix PRs sit unreviewed (issue #195).
3. **Ships build-constraint evaluation.** `go/build` provides it, which matters for twins, `build_included` and receiver owners.
4. **Fast.** About 6× faster at parsing and 10–11× end to end; absolute times are under 0.5 s per repo.

**Costs:**
- a second language in the tool;
- helper tests in `go test`, outside pytest-cov;
- the helper is built per toolchain, cached by `go version` and source hash;
- the JSON protocol is versioned through `EXTRACTOR_REVISION`.

**Mitigation:** pytest's default run uses frozen helper outputs, as codedoc_python freezes pyright captures. Tests that build the helper carry a marker.

Keep tree-sitter out of codedoc_go entirely. The Gemini chunker can split on the helper's top-level declaration spans.

---

## 5. Tests: what carries over

| codedoc_python element | Carries over? | Go work |
|---|---|---|
| `conftest.py` fakes: Gemini, Spanner, vertexai, instant sleeps, ADC lookup disabled | yes | Replace `SAMPLE_TREE` |
| `fake_lsp_server.py`: scripted JSON-RPC, JSON script, `crash_on`, open-file gating | yes, adapt | Add `textDocument/implementation`, `serverInfo` in `initialize`, gopls configuration section, progress-based readiness. Drop the pyright log lines |
| `resolution_fakes.FakeSession/FakeScript` (session-level fake) | yes, adapt | Add `implementation`; drop the fake typeshed |
| Frozen captures `tests/fixtures/pyright/1.1.414/` with re-capture script and test | pattern yes, data no | New `tests/fixtures/gopls/v0.23.0/` |
| Markers `lsp` / `network`; coverage gate 85 % over `graph_generator`; no real sleeps | yes | `lsp` = real gopls; `network` = module download. The Python base shrinks by the extractor, so the gate covers the pipeline and resolver |
| World builders (`decl_builders`, `rule_worlds`, `gate_worlds`, `table_world`, `graph_samples`) | pattern yes | Go-shaped entity builders |
| Neutral test files (2,790 lines) | yes | Names and keys |
| Adapted files (5,884 lines) | logic yes | Go entities and vocabulary |
| Extractor tests (1,859 lines) | no | `go test` with golden JSON for the helper, plus Python loader tests |

The gopls captures need these shapes:
- an import answer with one package clause per compiled file, including the test-variant answer from an external test package;
- a `builtin.go` answer (`len`, `new`);
- GOROOT and GOMODCACHE paths (`module@version`, `!`-escaped);
- vendor paths;
- an interface method declaration;
- an `implementation` list that includes stdlib types;
- promoted methods through embedding;
- generic and instantiated targets;
- method values;
- local-variable answers for closure calls;
- empty answers inside build-excluded files;
- `serverInfo` JSON;
- hover forms, if any rule parses hover.

---

## 6. Size estimate

The baseline is codedoc_python's lines (`wc -l`). Ranges assume the recommendations above: Go helper, `Packages` table, per-file pseudo-class. They also depend on engine-probe's rule set.

| Component | Python baseline | Go variant (code) | Basis |
|---|---|---|---|
| Extractor | `treesitter_parser.py` 2,080 | Go helper 900–1,300 + Python loader and chunker 250–400 | The probe `main.go` (513 lines) already covers declarations, sites, imports, embeds, constraints and callers. Go has no counterpart to Python's bindings, blocks or single-binding machinery (roughly 800–900 of the 2,080 lines, estimated from its method list) |
| Discovery | `py_units` 774 + `py_envs` 777 | 500–800 | go.mod/go.work/vendor instead of nine Python managers and three central-env hash formulas |
| Session layout | `py_layout` 586 | 150–300 | No execution environments or symlink views |
| Session | `py_session` 225 | 220–300 | Add `implementation` and `serverInfo` |
| Classification | `py_classify` 679 | 350–550 | `DeclIndex` reused; new rows |
| Semantics, rules, gates, fallback | 963 + 873 + 1,365 + 398 = 3,599 | 900–1,600 | Keep the gate frame (about 100). No MRO, decorators, dunders or conditional bindings. **Most uncertain row** |
| Orchestration | `resolution.py` 1,057 | 850–1,050 | About 60 % neutral |
| Provisioning | `py_provision` 446 | 150–300 | `go mod download` into an isolated cache |
| Model, config, prompts, CLI | 197 + 89 + 147 + 623 | 1,000–1,150 | Vocabulary edits |
| Pipeline | 2,499 | 2,300–2,550 | Phase 1.5/1.6 glue, builders, package imports |
| Schema, crossref, evaluate | 445 + 406 + 784 | 1,500–1,700 | Columns, owners, pins |
| Query stack and surfaces | about 2.8k, including the frontend | about the same | Instruction and cookbook rewritten |
| **graph_generator total** | **15,747** | **Python 8.2–10.7k + Go 0.9–1.3k** | Sum of the rows above |
| Tests | 18,650 (keep 2,790, adapt 5,884, replace 9,976) | 13–17k: kept 2.8k, adapted 5.0–6.0k, new Python 4.5–7.0k, Go helper tests 0.6–1.2k | Replaced Python-semantics tests shrink in proportion to code |
| Corpus | F1/F2/F3 code 2,544, ground truth 9,259 | Code 3–4.5k Go; about 150 + 45 + 25 cases | Go is more verbose than Python for the same cases. This is a guess, not measured |
| Ground-truth tools | `_gt` 2,970 | 2.5–3.5k | Snippet plumbing reused. Anchors and qualnames need a Go-aware stdlib-only tool (a small Go program or a careful regex reader). Oracle per the runtime-oracle track |
| Docs | 1,482 Japanese + 528 English (CLAUDE.md, SKILL.md, mcp_server/README.md) | about the same | Rewrite in the same five-file structure |

---

## 7. Open risks and unverified items

1. **Schema decisions** for the spec: package-level import targets (`Packages`), closures as nodes, `build_included`. They touch Phase 9, crossref, evaluate QA (`imports_of`), the agent cookbook and the docs.
2. **The build configuration is identity-adjacent state.**
   - Receiver owners and `build_included` depend on GOOS, GOARCH and tags.
   - So the configuration must enter the entity reuse stamp, the analog of `filters_hash`.
   - Sites in excluded files (221 and 718) get no gopls answer. Name-heuristic `PossiblyCalls` there would add noise.
3. **Interface dispatch is not closed-world in Go.**
   - `implementation` also returns stdlib types that match structurally (`reflect.StructTag.Get`) [probe].
   - engine-probe confirms that it lists stdlib, dependency and test-only types: 37 locations for `io.Writer.Write`, 101 for a constraint's `String`. Offline, it also over-includes, because types from missing modules collapse to `invalid type`.
   - At one site, the stdlib `log.(*Logger).Printf` that it listed beside an in-repo `Logger.Printf` was the actual runtime target [engine-probe, `reports/engine-probe.md`].

   A U1 analog needs a certainty argument. This belongs to the engine-probe track.
4. **The toolchain couples the helper and gopls.** Repos whose `go` directive exceeds the local toolchain fail under `GOTOOLCHAIN=local`. This belongs to the go-modules policy.
5. **Unverified:**
   - gopls column handling after a BOM;
   - the tree-sitter fork (alienvspredator) was not evaluated;
   - closure qualnames versus runtime names under inlining were checked on one sample only;
   - duplicates inside broken or ignored files were not seen (0) but are possible.
6. **The measurements cover two repos only.** Both lack cgo, dot imports, `//line` directives and blank functions. The fixtures must cover them.

---

## Appendix A: probes and raw outputs (`$W` = /home/admin/scratch/codedoc-go-research/code-seams)

| Path | What it is |
|---|---|
| `probes/goextract/main.go`, `probes/bin/goextract` | go/parser helper probe (stdlib only, Go 1.27.2) |
| `probes/tsextract.py`, `venv/` | tree-sitter probe (tree-sitter 0.25.2, tree-sitter-go 0.25.0) |
| `probes/compare.py`, `probes/analyze.py`, `probes/analyze2.py`, `probes/analyze3.py`, `probes/test_lines.py`, `probes/timeit.py` | Comparison and analysis scripts |
| `probes/gopls_smoke.py`, `probes/lsp_client.py` (copied unchanged), `probes/gp/` | gopls smoke probe |
| `probes/runtime_names/`, `probes/linedir/`, `probes/samples/*.go` | Runtime-name, `//line` and syntax samples |
| `out/files-*.txt`, `out/go-*.jsonl`, `out/ts-*.jsonl` | File lists and per-file extractor outputs |
| `out/time-*.json`, `out/compare-*.txt`, `out/analyze*-*.txt` | Timing, agreement and mapping outputs |
| `out/gopls_smoke*.txt`, `out/runtime_names.txt`, `out/linedir.txt` | gopls, runtime-name and `//line` results |
| `out/pytest-codedoc_python.txt`, `out/test_lines.txt`, `out/test_imports.txt`, `out/test_verdicts.md` | Suite run and test inventory |
| `out/go_list_xtest.txt` | `go list -test` import paths for an external test package |
| `out/pypi-*.json`, `out/gh-tsgo-*.json` | PyPI and GitHub API snapshots |

## Appendix B: sources (accessed 2026-10-13)

- https://pypi.org/pypi/tree-sitter-go/json, https://pypi.org/pypi/tree-sitter/json
- https://api.github.com/repos/tree-sitter/tree-sitter-go/ (tags, commits, releases, pulls #193 #198 #199, issue #195)
- https://go.dev/doc/go1.26 (Changes to the language), https://go.dev/doc/go1.27 (Changes to the language)
- `go help packages`, `go help buildconstraint` (Go 1.27.2)
- gopls v0.23.0 source: `golang.org/x/tools/gopls@v0.23.0/internal/protocol/mapper.go`, `internal/server/general.go` (module cache)
