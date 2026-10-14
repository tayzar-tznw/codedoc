# codedoc_go_leiden design: Leiden topics for Go, compared with LLM topics

**Status:** v1, 2026-10-13. Written under the user's goal "design, implement and test until everything is working correctly and efficiently". The user is offline and asked the lead to proceed on recommendations.
**Request (2026-10-13):** "Create a directory called codedoc_go_leiden, there create topic extraction based on [docs/] leiden community detection. There are both designs from claude and codex. Use knowledge from both and create. I want to compare leiden topics and current way of creating topics." And: "Make sure both has same type of docs as other programming language variants."
**Basis:** the codedoc_go spec (`codedoc_go/docs/superpowers/specs/2026-10-13-codedoc-go-design.md`, the parent of this delta); Claude's design `docs/LEIDEN_CLAUDE/2026-09-18-codedoc-shared-core-and-leiden-topics-design.md` §2.2, §5, §6.5–6.7, Appendix B; Codex's design `docs/LEIDEN_CODEX.md` §2.2, §7, §8, §9, §13.3–13.6, Appendix B (both at the monorepo root, local-only).

This is a **delta**: everything not stated here is codedoc_go's design (helper, discovery, resolution, gates, schema, three run modes, wiki node summaries, evaluate, trial, docs set).

---

## 0. Summary

`codedoc_go_leiden/` is a standalone sibling of `codedoc_go/` (a copy made after codedoc_go is built, the family's fork convention). It changes one thing: **topics come from Leiden community detection on the resolved code graph** instead of the LLM's directory-bucketed discovery. A `compare-topics` command compares both ways on the same repo and commit.

What each design contributes:

| Concern | Taken from | Choice |
|---|---|---|
| Responsibility split | both | Structure decides membership; the LLM only names communities and writes reports from structural evidence |
| Clustering unit | Codex §7.2, Claude §5.4 | Declared types, plus codedoc_go's per-file `<package>` pseudo-type for package-level functions |
| Relations | Codex §7.3 (strict profile) | Exact facts only: resolved calls, interface-method calls (the static target), embedding, implements. No inferred candidates, no directory prior, no LLM edges |
| Weights | Codex §7.4 | `Σ_r α_r · min(log2(1 + n_r), cap_r)` over distinct directed method pairs; hub factor 1; a ledger per edge |
| Tests and generated code | Codex §7.3 | Excluded from the clustering, attached afterwards with a marked role |
| Backend | Claude §5.5, §6.6 (licence) | `graspologic-native` (MIT), modularity, pinned, canonical input; **no silent fallback** (Codex §8.2) |
| Seeds and trials | Codex §8.4, §8.6 | Seeds 42–46, best normalized modularity, tie-break by membership hash, every trial recorded, seed ARI reported |
| Resolution | Claude §5.5 | Sweep and the longest-plateau rule inside a count band |
| Hierarchy | Codex §8.3, Claude §5.5 | Two levels: topics, then subtopics by bounded recursion inside each topic's membership |
| Small and isolated atoms | Claude §5.5, Codex §7.5 | Thin communities merge into the strongest neighbour; isolates attach by file co-location, marked `attached-file`; the rest go to 「未分類」 |
| Identity | both | Topic ID = hash of level and sorted member IDs; names carried over by Jaccard ≥ 0.6 |
| Naming and reports | Claude §5.6, Codex §9 | Batched naming with structural evidence, hub-name fallback; reports from a bounded evidence package, Mermaid from real edges, a grounding check |
| Evaluation | Codex §13.4–13.6, Claude §6.5 | AMI/ARI against each other and against directories, coverage, structural quality, stability, cost, grounding, feature recovery on F1 |

---

## 1. Scope and "done"

**In scope.** `codedoc_go_leiden/` (all of codedoc_go plus `graph_generator/topics/`), schema additions (§4), `compare-topics` (§6), the same five-file Japanese docs set as every sibling (`README.md`, `docs/ARCHITECTURE.md`, `docs/LSP_RESOLUTION.md`, `docs/GRAPH_SCHEMA.md`, `docs/CROSS_PROJECT.md`) plus the adapted English and support files, tests.

**Out of scope.** CPM, multiplex text layers, LLM refinement of membership (Claude Tier 2 / Codex "research-only"), VTA-derived edges, a test-inclusive profile as default (it is a profile option).

**Done means, each shown with output:**
1. Every codedoc_go done criterion holds for codedoc_go_leiden (the resolution core is identical; `evaluate` both modes 0 wrong; pytest ≥ 85 %; docs gate).
2. Integrity gates pass on F1, etcd and Prometheus: every eligible atom has exactly one primary leaf; each level covers every atom once; child union equals parent; every member is a real `class_id`; topic pages name no identifier that the graph does not contain beyond the reported grounding misses.
3. Determinism: three runs on the same input give the same membership hash.
4. `compare-topics` reports on F1, etcd and Prometheus, with a real Gemini run for both variants' wiki tracks (`--no-spanner` for etcd and Prometheus).

---

## 2. Pipeline changes

| Mode | codedoc_go | codedoc_go_leiden |
|---|---|---|
| `--graph` | 1 → 1.5 → 1.6 → 8 → 9 | 1 → 1.5 → 1.6 → 8 → 9 → **4a communities** (deterministic, no LLM): `Topics` rows with hub labels, membership and topic edges |
| `--wiki` | 2 → 3 → 4 (LLM discovery) → 5 → 6 → 10 | 2 → 3 → **4b naming** (LLM, batched) → **5 reports** (LLM, structural evidence) → 6 → 10 |

Communities are graph analytics and need no LLM, so they belong to `--graph`; `--wiki` names them and writes the pages. The codedoc_go precondition for `--wiki` (a finished `--graph` on the same hashes) applies unchanged and now also guarantees that the communities match. `topics/communities.json` (§3.6) checkpoints 4a; the graph checkpoint key includes its hash.

---

## 3. Communities (Phase 4a, `graph_generator/topics/`)

### 3.1 Inputs

The derived graph rows of the run (`build_node_rows` / `derive_edge_rows`; also readable from `graph_rows/*.jsonl`), never Spanner. Atoms are `Classes` rows: kinds `struct`, `interface`, `named`, `alias` and the per-file `package` pseudo-type.

**Owner atom** of a `Methods` row: its `class_id` (receiver type, interface, or the file's `<package>` for functions, `init#k`, `<init>`). Closures belong to the owner of their outermost enclosing declaration (codedoc_go §4.2).

**Eligibility (profile `strict`, default):** origin `package` or `command`, and not generated. Test (`test`, `xtest`) and generated atoms are excluded from the clustering (Codex §7.3) and attached in §3.4.

### 3.2 Projection (`graph_builder.py`)

| Relation | Source rows | Between atoms | α | cap |
|---|---|---|---|---|
| `call` | `MethodCalls`, resolution `types` or `convention:*`, callee not an interface method | caller's owner – callee's owner | 1.0 | 4 |
| `iface_call` | `MethodCalls` whose callee is an interface method (the static target) | caller's owner – the interface | 1.0 | 4 |
| `embed` | `ClassEmbeds` | child – embedded | 2.0 | 1 |
| `implements` | `ClassImplements` | type – interface | 0.5 | 1 |

`n_r(u,v)` counts **distinct directed (caller method, callee method) pairs** in both directions for call relations (repeated sites collapse, Codex §7.6), and 1 per pair for `embed` and `implements`. `weight(u,v) = Σ_r α_r · min(log2(1 + n_r), cap_r)`. Self-pairs are dropped; zero weights are removed. Excluded: `PossiblyCalls` (inferred), external targets, directory or package co-membership, topics, any text similarity. Profile options, recorded in the run: `degree_norm` (`w / sqrt(deg_u · deg_v)`), `calls_only` (an ablation), `with_tests` (tests as atoms).

A **ledger** row per projected edge (atoms, per-relation counts before and after deduplication, contribution, weight) goes to `topics/projection.jsonl`, so an unexpected grouping can be traced to calls (Codex §7.6).

### 3.3 Leiden (`community.py`)

- **Backend:** `graspologic_native.leiden` / `hierarchical_leiden`, pinned exactly; version recorded. A missing backend stops 4a with an install hint — never a silent switch to Louvain (Codex §8.2). graspologic-native drops isolates, so isolates are handled explicitly (§3.4). `starting_communities` is never passed (graspologic-native 1.3.1 panics on it).
- **Call:** `use_modularity=True`, `randomness=0.01`, `iterations=10`, `trials=1` per seed, seeds 42, 43, 44, 45, 46. Input canonicalised: sorted atom IDs, `(min, max)` endpoint order, sorted edge list.
- **Trial selection:** the highest **normalized** modularity recomputed by our own implementation (checked against Codex §8.5's worked example, Q = 11/26), ties broken by the lexicographically smallest membership hash. All trials, their qualities and the mean pairwise seed ARI are recorded.
- **Resolution (level 1):** sweep γ ∈ {0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5}; count communities with ≥ 3 atoms; pick the smallest γ on the longest plateau of that count inside the band `[TOPIC_MIN, TOPIC_MAX]` (defaults 5 and 25, comparable with the LLM baseline's 3–15 topics plus subtopics). If no γ lands in the band (for example more than 25 components of ≥ 3 atoms), pick the γ whose count is closest to the band and report it.
- **Level 2 (subtopics):** each level-1 topic with more than `SUBTOPIC_MIN_ATOMS` (default 12) atoms is re-clustered on its induced subgraph (γ = 1.0, same seeds). The split is accepted only if it is complete and nontrivial: at least two children with ≥ 3 atoms (Codex §8.3 rule 4, Claude §5.5). Otherwise the topic is a leaf. Children are disjoint and their union equals the parent.

### 3.4 Post-processing

1. **Thin communities** (< 3 atoms) merge into the neighbouring community with the most shared weight; with no neighbour they go to 「未分類」.
2. **Isolated eligible atoms** (no edges) attach to the community holding most other atoms of their file, role `attached-file`; else 「未分類」 (Claude §5.4). The role is visible in every output (Codex §7.5).
3. **Excluded atoms** (tests, generated) attach to the community they share the most weight with, roles `attached-test` / `attached-generated`; none → 「未分類」.
4. **Cohesion** (`internal edges / possible pairs`) and **conductance** are stored per community.

### 3.5 Identity and carry-over

- `topic_id = _make_id("topic", repo, f"L{level}", membership_hash)`, where `membership_hash = sha256("|".join(sorted(member class_ids)))[:16]`. A rename never changes an ID; any membership change does.
- **Carry-over:** new communities are matched to the previous run's by Jaccard overlap of member IDs (greedy, one-to-one). At ≥ 0.6, the previous name and report are reused and marked `inherited`, and the LLM is asked only about new or changed communities.
- The **hub** of a community is its member with the highest PageRank inside the community; its name is the deterministic label until 4b runs (`label_source = hub`).

### 3.6 Outputs (`OUTPUT_DIR/repos/<repo>/topics/`)

`communities.json` (parameters, backend version, sweep, trials, chosen γ, membership per level, roles, hashes, metrics), `projection.jsonl`, `names.json` (4b), `topic_tree.json` (the siblings' shape plus `class_ids` and `files`), `*.md` (5), `metrics.json`.

---

## 4. Schema additions (graph)

| Table | Change |
|---|---|
| `Topics` | adds `level`, `parent_topic_id`, `size` (atoms), `file_count`, `cohesion`, `conductance`, `hub_class_id`, `label_source` (`hub` / `llm` / `inherited`), `basis` (`structure`), `membership_hash` |
| `ClassBelongsToTopic` (new) | edge_id, class_id, topic_id, role (`primary` / `attached-file` / `attached-test` / `attached-generated`) |
| `FileBelongsToTopic` | derived: a file belongs to every leaf topic holding one of its atoms; role `primary` for the topic holding most of its declarations (types plus functions and methods), `secondary` otherwise |
| `TopicDependsOn` (new) | edge_id, source_topic, target_topic, weight, call_count — directed dependencies between leaf topics (L-R41) |
| `TopicContainsTopic` (new) | edge_id, parent_topic, child_topic |

Written in Phase 9 of `--graph` (labels from hubs) and updated in Phase 10 of `--wiki` (names, summaries, embeddings). The agent instruction gains the topic cookbook: members of a topic, topic of a class, dependencies between topics, the topic tree.

---

## 5. Naming and reports (`--wiki`)

### 5.1 Naming (4b)

One batched call per ≤ 60 communities, Japanese, 2–5 words, functional rather than layered (「機能・特徴別に分類」). Per community: the top 12 members by in-community PageRank (name, kind, package, the first line of the member's Phase 2 summary), the hub, packages and modules touched, the names of the three strongest neighbouring communities, the size, and for subtopics the parent's name. Output `{topic_id: name}`; validation: non-empty, unique after normalisation, not a placeholder; invalid → one retry → hub-name fallback (`label_source = hub`).

### 5.2 Reports (5)

One call per topic, from a bounded evidence package (12,000 input tokens, Codex §8.6): a members table (type or function, kind, file, one-line summary), hubs with call counts, the ten strongest intra-topic call paths, the inter-topic dependencies (`TopicDependsOn`), entry points (atoms called only from other topics), and for parents the children's summaries. Evidence rows are ordered by combined endpoint degree and cut at the budget; omissions are recorded (Claude §5.6). The Japanese page template stays the siblings' (はじめに / アーキテクチャと設計 / コンポーネント関係図 / データフロー / 主要コンポーネント / 依存関係) plus 「構造的根拠」. Mermaid diagrams are scaffolded from real edges; the LLM annotates, never adds edges (Codex §9.2).

**Grounding check** (deterministic): every code identifier in backticks on a page must exist in the graph's name set (types, functions, methods, packages, files); misses are listed at the page bottom and counted. The same check runs on codedoc_go's LLM-topic pages in `compare-topics`, so both ways are measured alike.

---

## 6. `compare-topics`

```
python -m graph_generator compare-topics --llm <codedoc_go OUTPUT_DIR>/repos/<repo> \
    --leiden <codedoc_go_leiden OUTPUT_DIR>/repos/<repo> [--llm2 <second LLM run>] \
    [--features features.json] [--out DIR]
```

It refuses unless both outputs come from the same file hashes (`entities.json`). The LLM topics are codedoc_go's `topics/topic_tree.json` (file-level, overlapping memberships kept); the Leiden topics are `communities.json`. Both are projected to the same atom graph (§3.2).

| Group | Metric | Notes |
|---|---|---|
| Coverage | production files and atoms with ≥ 1 topic; LLM paths that are not repo files (invented); files in several topics | Codex §13.5 membership integrity |
| Size | topics per level; size distribution (files, atoms); largest topic share | |
| Agreement | AMI, ARI (and NMI for reference) on files with exactly one topic in both; mean best-match Jaccard both ways; a side-by-side table matching each LLM topic to its best Leiden topic | Chance-corrected metrics first (Claude §6.5) |
| Directory alignment | AMI of each method against the top-level directory and against the package partition | Claude §6.5 expects 0.3–0.7 for useful structure |
| Structural quality | normalized modularity of each partition on the shared atom graph; share of edge weight inside topics; mean conductance | LLM atoms take the topic of their file; atoms of multi-topic files are reported |
| Stability | Leiden: seed ARI and rerun hash equality; LLM: ARI between two runs when `--llm2` is given | Codex §13.4 separates the two |
| Cost | LLM calls, tokens and wall time for discovery and for pages, per method | From the run logs |
| Grounding | identifier existence rate on topic pages, per method | §5.2 |
| Feature recovery | with `--features`: per reviewed feature F, `max_G |F ∩ G| / |F|`, dispersion (topics needed to cover 80 % of F) and the irrelevant share of the best G | Codex §13.6; F1 ships `ground_truth/features.json`, written from the corpus design before any topic run |

Outputs: `topic_comparison.json` and `topic_comparison.md` (English, with the Japanese topic names as they are). No metric picks a winner by itself; the report shows all of them side by side (Codex §13.5).

---

## 7. Dependencies, settings, tests, docs

- **Dependencies:** `graspologic-native` (exact pin, MIT, Python 3.13 wheels — verified at build time), `networkx` (BSD; PageRank, conductance). AMI, ARI and NMI are implemented in plain Python and checked against frozen scikit-learn values in the tests (no scikit-learn dependency).
- **Settings:** `TOPIC_MIN` (5), `TOPIC_MAX` (25), `SUBTOPIC_MIN_ATOMS` (12), `TOPIC_PROFILE` (`strict`), `TOPIC_SEEDS` (`42,43,44,45,46`), `SPANNER_DATABASE` default `codedoc-go-leiden`, deploy service and image `codedoc-go-leiden-mcp`.
- **Tests:** projection (weights, deduplication, exclusions, ledger), community (determinism, isolates, thin merge, split acceptance, IDs, carry-over), metrics (ARI/AMI/NMI against frozen values; modularity against Codex's worked example), naming and report validation (fakes), the grounding check, `compare-topics` on synthetic inputs, F1 end to end with fakes.
- **Docs:** the five-file set adapted — `ARCHITECTURE.md` describes 4a/4b/5 and the comparison design and keeps the dated comparison results; `GRAPH_SCHEMA.md` adds §4's tables and the topic cookbook; `README.md` adds `compare-topics` and the topic settings; `LSP_RESOLUTION.md` and `CROSS_PROJECT.md` are codedoc_go's.

---

## 8. Decisions

| # | Choice | Reason | Alternative |
|---|---|---|---|
| L1 | Communities in `--graph`, naming and reports in `--wiki` | Deterministic structure needs no LLM; the user's mode split | All topic work in `--wiki` |
| L2 | Types plus per-file `<package>` atoms | Go code is often package-level functions; per-file keeps codedoc_go's IDs and freshness | Types only (functions unassigned); files only |
| L3 | Strict profile (exact facts) | Codex §7.3: apparent coverage from inferred edges hides weak evidence | Enriched profile with `PossiblyCalls` |
| L4 | graspologic-native, no fallback | MIT licence (Claude §6.6); silent backend switches corrupt comparisons (Codex §8.2) | leidenalg (GPL) |
| L5 | Plateau rule in a 5–25 band, plus subtopics | Comparable granularity with the LLM baseline; deterministic | A fixed γ = 1.0 |
| L6 | Tests and generated code attached, not clustered | Codex §7.3; generated protobuf types act as hubs | Clustered |
| L7 | AMI/ARI in plain Python | Avoid a heavy scikit-learn dependency | scikit-learn |

## Appendix — evidence carried over

Claude §5.2 measured on the C# corpus: Leiden at γ 1.0 vs LLM topics NMI 0.56 / ARI 0.36; hub exclusion hurt agreement (ARI 0.36 → 0.16); seeding changed nothing; CPM gave directory-like groups (ARI 0.02–0.06); seed ARI 0.81–0.86, so determinism must be engineered. Literature: dependency-only clustering reaches about 45–60 % MoJoFM against expert decompositions. These shaped the defaults; the Go comparison measures them again.

---

## Appendix B — Review amendments (2026-10-13)

From `/home/admin/scratch/codedoc-go-research/spec-review/REVIEW.md`; **they override the main text**. Parent-spec amendments (codedoc_go Appendix B) apply too, notably R10 (delete-then-insert of topic tables) and R11 (shared Phase 2 cache).

| # | Finding | Amendment |
|---|---|---|
| L-R8 | `leiden([])` raises; a zero weight panics in Rust (`PanicException`, a `BaseException`) | Total weight 0, or a level-2 subgraph without edges → singletons / a leaf without calling the backend; zero or NaN weights are never passed; backend calls are wrapped in `except BaseException` and stop 4a with a clear message, never the graph track |
| L-R9 | Integrity gate not implementable as written | Primary membership = the clustered (eligible, connected) atoms. All other atoms (isolates, thin leftovers, tests, generated, build-excluded) are **attached** at every level inside their parent with the same rules: by file co-location (role `attached-file`), else by shared weight on a `with_tests` projection with the same relations and α (roles `attached-test`, `attached-generated`, `attached-excluded`), else 「未分類」. 「未分類」 is one `Topics` row per repo (level 1, role `unassigned` for its members, never sent to the LLM, counted by the gate). Thin communities (< 3 atoms) merge into the neighbour with the most shared weight; with no neighbour their atoms are treated like isolates. Leaves that are not split are carried to level 2. `membership_hash` covers primary members only. Ties: smallest class_id / membership hash. `SUBTOPIC_MIN_ATOMS` counts primary members |
| L-R24 | Circular phase order | 4a runs after Phase 1.6 on the derived rows (pure builders), **before** Phase 8; Phase 8 writes `Topics`, Phase 9 writes the topic edges; the graph checkpoint stores the communities hash |
| L-R25 | Trial selection and sweep | Trials are compared by modularity at the trial's own γ (`graspologic_native.modularity(..., resolution=γ)`); plateau length = number of consecutive grid points with the same count; ties → the smaller γ; "closest to the band" ties → the smaller γ. `hierarchical_leiden` is not used (level 2 uses `leiden` on induced subgraphs) |
| L-R26 | Projection details | Call pairs are counted between the callers' **outermost declarations** and callees (closures collapse); `implements` stays at α 0.5 and `compare-topics` reports the `no_implements` ablation; build-excluded atoms are not eligible (attached, role `attached-excluded`) |
| L-R27 | compare-topics inputs | Both variants write `OUTPUT_DIR/repos/<repo>/topics/topic_tree.json`; `compare-topics` requires identical entity hashes, build configuration, mode and `resolutions.json` hash, and projects both partitions onto the same atom graph; atoms of multi-topic LLM files are excluded from the modularity metric and reported |
| L-R38 | Carry-over reused stale reports | Carry-over reuses the **name** only; the report is regenerated whenever membership changed |
| L-R39 | Partial resolution was silent | `communities.json` records resolution coverage per group; topics touching a failed group are marked `partial` |
| L-R40 | `FileBelongsToTopic.role` | The column is added (`primary` / `secondary`); ties → the smaller topic_id |
| L-R41 | `TopicDependsOn` was described as calls only, but the projection's dependency strength also counts embeds and implements (found while writing the docs, 2026-10-14) | A row exists for every ordered pair of leaf topics linked by confirmed calls (interface method calls included), embeds or implements; `weight` is their weighted strength; `call_count` counts distinct (caller, callee) method pairs, so it is 0 on rows built only from embeds or implements |
| L-R42 | The webapp graph explorer (codedoc_go R51) | Comes with the sync and reads the leiden topic tables generically: the topic map draws level-1 topics with their level-2 subtopics (`TopicContainsTopic`), weighted `TopicDependsOn` edges, roles from `ClassBelongsToTopic`, and 「未分類」 apart |
| L-R43 | Inside the chosen plateau the smallest γ won, but there modularity's resolution limit keeps a giant community while small components fill the count (Prometheus γ 0.2: 1,108 of 1,681 atoms in one topic, 65.9 %; γ 0.35: 36.9 %) | Amends L-R25's within-plateau tie-break: the **largest** γ of the chosen plateau wins. Equal-length plateaus and "closest to the band" keep the smaller γ; `resolution_rule` stays `plateau`. Adopted 2026-10-14 after the first real-repo measurement: it changes etcd 0.35 → 0.5 and Prometheus 0.2 → 0.35; F1's partition is unchanged (γ recorded 0.75). Evaluation table: `/home/admin/scratch/codedoc-go-build/L1/plateau/eval_tables.txt` (from `eval_plateau.py` in the same directory) |
