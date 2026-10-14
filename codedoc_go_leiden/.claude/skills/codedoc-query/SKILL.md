---
name: codedoc-query
description: >-
  Proactively query a CodeDoc-indexed codebase through the `codedoc` MCP server's
  `ask_codebase` tool (a graph_query_agent over a Spanner code graph) to understand
  code BEFORE grepping or editing. Use it on your own initiative — without waiting
  to be asked by name — whenever fixing a bug, debugging, refactoring, extending,
  or reviewing a large or unfamiliar codebase: to locate where something lives,
  list a type's methods or a package's functions, trace who-calls-what (call
  chains), find the implementers of an interface or what a struct embeds, map
  package dependencies, assess change impact / blast radius, plus "what does X do /
  explain this component" lookups. Prefer ask_codebase over manual code spelunking
  for whole-codebase questions. Follow-ups reuse the returned session_id. Triggers:
  fix/debug/refactor/understand a big codebase, impact analysis, call graph,
  dependency check.
---

# CodeDoc codebase query (MCP) — use proactively

The `codedoc` MCP server exposes a query agent (`graph_query_agent`) that answers
questions about an indexed codebase by running GQL against a **Spanner code
knowledge graph**. The graph's nodes carry both structure *and* generated
`summary` text, so one tool covers structural queries and documentation lookups.
**The agent answers in Japanese** (its configured default) — relay answers as-is
unless the user wants a translation.

## Use this proactively

When you're about to **fix, debug, refactor, extend, or review** a large or
unfamiliar codebase, reach for `ask_codebase` on your own initiative — *before*
grepping or reading files, and without waiting for the user to name the tool.
Whole-codebase questions are exactly what the graph answers fastest.

Default workflow when starting a task in such a codebase:

1. **Orient** — ask for a high-level overview, or where a feature/area lives.
2. **Locate** — ask which package / type / file / function implements the thing you must change.
3. **Check impact BEFORE editing** — ask what depends on / calls the target
   ("what calls `X`?", "which packages import `<path>`?", "blast radius of changing `Y`?").
4. **Drill down** — refine with follow-ups in the *same* session (see below).

Then make your edit with the dependency/impact picture in hand.

**When NOT to use it:** files already open in front of you (just read them), or
no `codedoc` server is connected (see Prerequisites — fall back to normal code
navigation and tell the user the index isn't available).

## Prerequisites: the MCP server must be connected

The tools appear under an MCP server named **`codedoc`**: `ask_codebase`,
`list_sessions`, `get_session_history`, `delete_session`, `rename_session`.

- **Claude Code**: run `/mcp` to confirm `codedoc` is connected. If not, add it to
  `.mcp.json` (project) or `~/.claude.json`:
  ```json
  { "mcpServers": { "codedoc": { "type": "http", "url": "http://127.0.0.1:8080/mcp" } } }
  ```
- **Antigravity CLI (`agy`) / Gemini CLI**: register an extension at
  `~/.gemini/extensions/codedoc/gemini-extension.json` pointing at the same URL
  via `httpUrl` (not the Claude shape).
- The server itself is started from the **CodeDoc MCP server repo** (local
  `python -m mcp_server` → `http://127.0.0.1:8080/mcp`, or a Cloud Run deploy
  reached via `gcloud run services proxy`). It needs GCP credentials and a
  **populated graph** for *this* codebase. See that repo's README for setup.

If the `codedoc` tools aren't present, don't guess from memory — tell the user the
index for this codebase isn't connected.

## Tools

| Tool | Use it for | Returns |
|---|---|---|
| `ask_codebase(question, session_id?)` | The main entry point. Ask one clear question; omit `session_id` to start fresh. | `{response, session_id, turn_count}` |
| `list_sessions()` | See active Q&A threads (most recent first). | list of `{id, label, first question, turn_count, timestamps}` |
| `get_session_history(session_id)` | Full transcript of a thread. | list of `{role, text, timestamp}` |
| `rename_session(session_id, label)` | Label a thread for later lookup. | `{session_id, label}` |
| `delete_session(session_id)` | Prune a thread (idempotent). | `{deleted, session_id}` |

Sessions are **in-memory for the life of the server process** — they don't
survive a server restart.

## What the graph contains

The agent translates questions into GQL over this schema (the Go variant of
CodeDoc with Leiden topics, `codedoc_go_leiden`): <!-- LEIDEN-HOOK -->

- **Nodes** (6): `Files`, `Packages`, `Classes`, `Methods`, `Topics`,
  `Directories`, each with a `summary`.
- **Edges** (17): `PackageImports`, `FileDependsOn`, `ClassEmbeds`,
  `ClassImplements`, `MethodCalls`, `PossiblyCalls`, `FileDefinesClass`,
  `ClassDefinesMethod`, `DirContainsFile`, `PackageContainsFile`,
  `FileBelongsToTopic`, `ClassBelongsToTopic`, `TopicDependsOn`,
  `TopicContainsTopic`, `CrossRepoCalls`, `CrossRepoRef`, `CrossRepoImports`. <!-- LEIDEN-HOOK -->

How Go code is represented:

- **`Classes` are Go types** (struct, interface, named type, alias); `Methods` are
  functions, methods, interface methods, `init` functions and closures.
- **`module` is the Go module path** (from `go.mod`) and **`package` the import
  path**, as columns on Files, Classes and Methods (`Files.module`,
  `Files.package`); `Packages` has one row per directory and package clause. One
  repository can hold several modules.
- **`Topics` are structural topics**, not Go modules: Leiden community
  detection groups types (and each file's `<package>` pseudo-type) by their
  confirmed calls, embeds and implements; Gemini only names the groups.
  Level-1 topics can split into level-2 subtopics (`TopicContainsTopic`);
  `ClassBelongsToTopic` gives a type's topic at each level with its role
  (`primary`, `attached-*`, or `unassigned` for types placed in no topic).
  `TopicDependsOn` links leaf topics by confirmed calls, embeds and
  implements; its `call_count` counts distinct caller-callee method pairs and
  is 0 when only embeds or implements link them. Ask about a module by its
  module path, not as a topic. <!-- LEIDEN-HOOK -->
- Every file has a **`<package>` pseudo-type**. Its methods are the file's
  package-level functions, its `init` functions (`init#1`, `init#2`, numbered per
  file) and `<init>`, the pseudo-function holding the package-level variable
  initializers (the caller of every call made there).
- **Qualified names**: functions `F`, methods `T.M` (value and pointer receivers
  share the name; the receiver is a column), interface methods `I.M`, closures
  `F.func1` (nested `F.func1.func1`, inside `<init>`: `<init>.func1`), blank
  functions `_#1`. `fqcn` (types) and `fqmn` (functions, methods) are
  `<import path>.<qualname>`, e.g. `example.com/iryo/billing.FeeCalculator.Calculate`.
  Per-file names and build-tagged twins share an `fqmn`; the file path tells them apart.

What the edges mean:

- `MethodCalls` holds only **confirmed** targets inside the repository, with a
  `resolution` provenance: `types` (go/types bound it) or `convention:<rule>`
  (`funclit`, `func-alias`, `linkname`, `range`). `MethodCalls.site_kind` is
  `call`, `go`, `defer` or `range`.
- **Interface calls** through a package-level named interface (also via a type
  parameter it constrains) are edges to the interface method. The in-repo
  types that may run instead are `PossiblyCalls` with reason `implementation`;
  Go interfaces are satisfied implicitly, so even a single implementer stays a
  "maybe". An unnamed interface (an interface type literal, a function-local
  interface or an inline constraint) has no method node: a call through it is
  `ambiguous` with those `PossiblyCalls` when the repository implements it,
  else `dynamic`. `open_world` marks calls where types outside the repository
  (standard library, dependencies) also satisfy the interface, so the real
  target may be none of the candidates. `ClassImplements` lists which types
  satisfy which in-repo interfaces; `ClassEmbeds` lists embedded types.
- Calls into the standard library or other modules (`external`), builtins
  (`builtin`, which includes `Error()` of the predeclared `error` interface),
  calls through func values (`dynamic`) and calls with no certain answer
  (`unresolved`, e.g. a module missing offline) are counted, **never edged**.
  A missing edge does not mean "not called". `ambiguous` calls have
  `PossiblyCalls`: reason `variant` when the test and production variants of a
  package disagree, reason `implementation` for calls through an unnamed
  interface.
- `PackageImports` links a file to the in-repo packages it imports;
  `FileDependsOn` comes from confirmed calls and embeds.
- Calls between modules and packages of one repository are ordinary
  `MethodCalls` (compare the two `module` / `package` values). A call into a
  sibling module is in-repo only when the go command builds that module from
  source (`go.work` or a local `replace`); otherwise it is `external`. Calls,
  imports and type references between repositories live only in
  `CrossRepoCalls`, `CrossRepoImports` and `CrossRepoRef` (kinds `embeds`,
  `implements`).

## Asking good questions

- **One concept per call.** Ask a single, specific question; drill down with
  follow-ups rather than stacking sub-questions.
- **Name concrete entities** with their import paths when you know them — it
  anchors the GQL. Use placeholders below with your real names:
  - "list the methods of type `<import/path.TypeName>`"
  - "which functions does package `<import/path>` define?"
  - "what calls `<import/path.Type.Method>`?" / "what does `<import/path.Func>` call, including interface candidates?"
  - "which types implement the interface `<import/path.Iface>`?"
  - "what does struct `<import/path.TypeName>` embed, and which types embed it?"
  - "which packages does package `<import/path>` import?" / "which packages import package `<import/path>`?"
  - "which files depend on `<path/to/file.go>`?"
  - "which calls go from module `<module/a>` to `<module/b>`?"
  - "which methods in other repositories call `<import/path.Func>`?"
  - "give a high-level overview of this codebase" / "what does the `<topic>` topic cover?"
  - "which topic does `<import/path.TypeName>` belong to, and which topics does that topic depend on?" <!-- LEIDEN-HOOK -->
- **Japanese phrasing works best**, English is accepted (e.g. `<import/path.TypeName> のメソッドを一覧して`).

## Multi-turn follow-ups

`ask_codebase` returns a `session_id`. Pass it back on the next call to continue
the same thread with full prior context:

```
1) ask_codebase("<import/path.TypeName> のメソッドを一覧して")
     → { response: "...", session_id: "abc123", turn_count: 1 }
2) ask_codebase("その中で外部 I/O を行うのは？", session_id: "abc123")
     → { ..., session_id: "abc123", turn_count: 2 }
```

Use `list_sessions()` to find a thread, `get_session_history(id)` to review it,
and `rename_session(id, "...")` to label a long investigation.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `NOT_FOUND: Instance not found` | The Spanner instance the server points at doesn't exist / the graph for this codebase isn't built. The codebase needs to be indexed by CodeDoc first. |
| `403 Forbidden` (Cloud Run) | Caller lacks `roles/run.invoker`, or `gcloud run services proxy` isn't running. |
| Empty / "見つかりません" answer | The entity isn't in the graph (wrong name, or not indexed). Re-check the name, or start with a broad overview question. |
| First call is slow | Cold start (agent + Spanner import). Subsequent calls are fast. |

This skill *queries* an index; it does not build one. Indexing a codebase into the
graph is done by the CodeDoc pipeline, not from here.
