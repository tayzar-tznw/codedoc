# Workflow orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately – don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Research Mode Default
- Always use web search to verify about the latest updates and truth

### 3. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution
- If subagents need to share some context use agent team
- Utilize agent team whenever possible for efficient work

### 4. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 5. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 6. Continuously improve the work
- Even after doing the first verification create review agent and ask for improvements.

### 7. Final documentation
- Always document the feature of the codebase and how it works in details in a Markdown. Create a subagent to do this. Think of it like a secretary. If such Markdown already exist command that subagent to always update the Markdown.

# Test corpus knowledge (test_codes/)

- `test_codes/iryo-kanri-system/` is the hand-crafted 医療管理システム (clinic management) Java/Maven corpus — the ONLY committed fixture (`.gitignore` negation: `test_codes/*` ignored, `!test_codes/iryo-kanri-system/` re-included; its `_feed/` and `_m2/` stay local-only). It is the ground-truth baseline for the Phase 1.6 jdtls resolution feature, and the current pipeline resolves it (2026-09-17 full-corpus run: 12/12 projects resolved, 57 `.java` files, 227 declarations, 403 sites = 208 resolved / 194 external / 1 unresolved / 0 ambiguous, ~157 s).
- Topology is the point: 12 self-contained Maven projects, coordinates `jp.iryo:<artifactId>:1.0.0` (directory name == artifactId) — 9 libraries (`iryo-kernel`, `iryo-domain`, `iryo-data`, `iryo-billing`, `iryo-pharmacy`, `iryo-scheduling`, `iryo-reporting`, `iryo-legacy-claims`, `iryo-interop`) + the `reception-desk`/`admin-console` consoles + `patient-portal` (JDK `com.sun.net.httpserver`; its `src/main/webapp/patient_view.jsp` is a vestigial template that is never served). EVERY inter-project dependency is a jar restored from the gitignored local feed `_feed/` (`file://../_feed`) into the isolated local repo `_m2/` — there is NO parent POM, reactor, or relative module reference anywhere. Each project has its own `pom.xml`, `build.sh`, `README.md`, `.gitignore`; the only shared build config at the fixture root is `settings.xml` (empty `<mirrors/>`).
- Toolchain: JDK 21 (`JAVA21_HOME` overrides `/usr/lib/jvm/java-21-openjdk-amd64`) + Maven 3.9+; all POMs use `--release 21`, `-parameters`, UTF-8. Build: run each project's `build.sh` in the topological order in the fixture `README.md` (`rm -rf _feed _m2` → iryo-kernel → iryo-domain → iryo-data/billing/pharmacy/scheduling/reporting → iryo-legacy-claims/iryo-interop → reception-desk/admin-console/patient-portal → `ground_truth/SemanticProbe`). A dependent built before `_feed/` exists fails to resolve `jp.iryo:iryo-kernel:jar:1.0.0` — that is by design, not breakage. Exactly one expected javac warning: the deprecation warning for the intentional 旧点数表 call in reception-desk `AccountingFlow.java` (`new OldTable().calculate(30, 10)`; `OldTable extends PointTable`, both `@Deprecated` in iryo-legacy-claims); only reception-desk enables `-Xlint:deprecation`.
- Ground truth lives ONLY in `ground_truth/` (product sources are clean, no tags/instrumentation). GROUND-TRUTH STATUS: the runtime gate is `ground_truth/SemanticProbe/build.sh` (`jp.iryo:semantic-probe`, 87 checks against the `_feed/` jars) → must print 「検証項目: 87 件」 and `SEMANTIC PROBE PASSED`. The manifest chain `cases.py` → `generate_manifest.py` → `expected_resolutions.json` → `check_ground_truth.py` is STILL the C# v2 version (106 cases pointing at `Iryo.*/**/*.cs`, targets keyed by `assembly`, via nuget/source/bcl) and has NOT been ported: `check_ground_truth.py` exits 1 with 106 「ファイルがない」 mismatches, and `graph_generator/scorer.py` expects a Java v3 manifest (`artifact` = Maven artifactId or JDK module name, via `source`/`maven`/`jdk`), so `python -m graph_generator score` is NOT usable as the gate until the port. Do not run `generate_manifest.py` meanwhile (it overwrites the manifest).
- Per-project roles and the trap map (scenario categories XPR/TWIN/OVL/INH/DIM/DI/IFI/GEN/STR/MRF/REC/NSP/BND/UNR): `docs/PROJECTS.md`. Signature eval cases: the XPR pair — byte-identical procedure lines in `reception-desk/src/main/java/iryo/reception/flows/DailyCloseFlow.java` and `admin-console/src/main/java/iryo/admin/DailyBatch.java` that bind to different artifacts purely via each file's imports (受付: `[記録]`/true/150円 vs 管理: `[調剤]`/false/162円).

### Maintenance invariants (when editing the corpus)
- NEVER add tags, asserts, or instrumentation to product sources (`iryo-*`, `reception-desk`, `admin-console`, `patient-portal`) — they must stay realistic and clean; verification code belongs in `ground_truth/SemanticProbe` only. Ground-truth cases belong in `ground_truth/cases.py` (file + unique snippet + expected symbol) with declaration anchors in its `DECL_PINS`/`STATIC_TARGET_PINS` tables (`scorer.py` reads both) — but those tables and every case are still C#, so the case mechanism is pending the Java port; new cases must use the v3 `artifact`/`via` shape and the `java_decls` symbol grammar (e.g. `iryo.billing.FeeCalculator.calculate(int, int)`).
- The XPR pair invariant: the procedure lines of `DailyCloseFlow.run()` and `DailyBatch.run()` must stay byte-identical (only the imports, class name, and heading `System.out.println` differ). SemanticProbe mirrors the pair in `iryo.probe.xpr.ReceptionSide`/`AdminSide` under the same rule; editing one side means mirroring the other three, or the cross-project divergence cases lose their point.
- Maven cache gotcha: after editing a library, run that library's `build.sh` (it evicts its own artifact from BOTH `_m2/jp/iryo/<artifactId>` and `~/.m2/repository/jp/iryo/<artifactId>`, then deploys the jar + `-sources.jar` into `_feed/`) BEFORE rebuilding dependents — otherwise dependents silently reuse the stale 1.0.0. jdtls/m2e reads `~/.m2` by default, so a stale user cache also poisons Phase 1.6; `JAVA_LSP_MAVEN_SETTINGS=/path/to/settings.xml` points m2e at an isolated repository instead.
- Once the manifest is ported: if `generate_manifest.py` fails after a code edit, it names the case ID whose snippet became missing/non-unique — fix the snippet in `cases.py`, don't touch the tooling. `expected.*` in the manifest is javac/JLS semantics (stable); `today.*` is a snapshot of the current pipeline and is EXPECTED to go stale when the pipeline improves — update `today.*` notes then, never `expected.*`.
- Verification today = SemanticProbe (87 runtime checks) + the full-corpus Phase 1.6 run; the manifest/checker leg returns after the port. Full definition of done for any fixture change: affected `build.sh` chain green → both console demos print `=== 受付業務デモ完了 ===` / `=== 管理タスク完了 ===` → `SEMANTIC PROBE PASSED` → (once the manifest is ported) `generate_manifest.py` → `check_ground_truth.py` exit 0.
