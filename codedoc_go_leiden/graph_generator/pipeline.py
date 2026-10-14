"""Pipeline orchestrator and phase functions (spec §2.1).

The graph track (Phases 1 → 1.5 → 1.6 → 8 → 9) builds the code graph and makes
no LLM call. The wiki track (Phase 1 re-scan → 2 → 3 → 4 → 5 → 6 → 10) reads a
finished graph and adds Gemini summaries, topics, pages and embeddings.

Hand-off points that later work packages fill raise NotImplementedError naming
the package; modules owned by other packages are imported on first use
(_handoff), so this package imports before they exist.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import json
import os
import pickle
import random
import sqlite3
import time
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import config
from .lazy_import import LazyModule
from .prompts import (DIR_SUMMARY_PROMPT, PACKAGE_SUMMARY_PROMPT, PROMPT_VERSION,
                      TOPIC_EXTRACTION_PROMPT, TOPIC_MERGE_PROMPT, TOPIC_SUMMARY_PROMPT)

# Imported on first use: the graph track, doctor, evaluate and trial never call
# Gemini or Spanner, and the client libraries take about a second to import.
spanner = LazyModule("google.cloud.spanner")
genai = LazyModule("google.genai")
types = LazyModule("google.genai.types")

# Bump whenever the inputs of _make_id change (spec §6.4).
ID_SCHEME = 1


# ===================================================================
# Data model
# ===================================================================


@dataclass
class PipelineData:
    target_dir: str
    # Multi-repo: every node ID is scoped by the repo, so identical relpaths
    # and qualnames in different repos stay distinct nodes in one shared graph.
    # Defaults to the target directory basename (set in Phase 1).
    repo: str = ""

    # Run options: the module source mode ("offline" | "download", spec §3.7),
    # --no-spanner, the peers (go_model.Peer) and the toolchain preflight found
    mode: str = "offline"
    no_spanner: bool = False
    peers: list = field(default_factory=list)
    toolchain: Any = None

    # Phase 1: absolute paths of the scanned files, the directory tree, the
    # SHA-256 of every file by relpath (spec §2.6) and the skip counts
    file_list: list[str] = field(default_factory=list)
    dir_tree: dict[str, Any] = field(default_factory=dict)
    dir_queue: list[str] = field(default_factory=list)
    file_hashes: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    discovery: Any = None                      # go_model.Discovery (WP-D)

    # Phase 1.5: relpath → entities.json FileRecord (C3)
    extracted_entities: dict[str, Any] = field(default_factory=dict)
    # Phase 1.6: the resolutions.json artifact (C5) and its resolutions_hash,
    # taken once by run_graph_track for the checkpoints ("" = not taken)
    resolutions: dict[str, Any] = field(default_factory=dict)
    resolutions_digest: str = ""
    # Phases 8-9: rows written per table in this run
    graph_counts: dict[str, int] = field(default_factory=dict)

    # Wiki track. Every key is repo-relative (C7).
    file_summaries: dict[str, str] = field(default_factory=dict)             # Phase 2: relpath → text
    node_summaries: dict[str, dict[str, str]] = field(default_factory=dict)  # Phase 2: relpath → {qualname: text}
    package_summaries: dict[str, str] = field(default_factory=dict)          # Phase 3: "<dir>|<name>" → text
    dir_summaries: dict[str, str] = field(default_factory=dict)              # Phase 3: dir relpath ("" = root) → text
    topics: list[dict] = field(default_factory=list)                         # Phase 4: the topic tree (C7)
    topic_summaries: dict[str, str] = field(default_factory=dict)            # Phase 5: topic id → page
    graph_rows: dict[str, list[dict]] = field(default_factory=dict)          # graph_rows/*.jsonl (R20)
    topic_stats: dict[str, dict] = field(default_factory=dict)               # topics/stats.json (C7)
    # Gemini calls and input, output and thinking tokens per wiki phase, keyed
    # like timings: the timing report's Gemini block (R49)
    gemini_usage: dict[str, dict] = field(default_factory=dict)

    # Timing
    timings: dict[str, float] = field(default_factory=dict)


class PreflightError(Exception):
    """A preflight check failed; the CLI exits with code 3 (C9)."""


class WikiPreconditionError(Exception):
    """--wiki cannot run on this repo's graph; the CLI exits with code 4 (C9)."""


def _handoff(module: str, attr: str, wp: str):
    """graph_generator.<module>.<attr>, delivered by work package *wp*.

    Imported on first use. A graph_generator module or attribute that is not
    there yet raises NotImplementedError naming the work package; a missing
    third-party module raises as usual.
    """
    name = f"{__package__}.{module}"
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith(f"{__package__}."):
            raise
        raise NotImplementedError(
            f"{module}.{attr} is not implemented yet ({wp}): {exc}") from exc
    try:
        return getattr(mod, attr)
    except AttributeError:
        raise NotImplementedError(f"{module}.{attr} is not implemented yet ({wp})") from None


# ===================================================================
# Helpers
# ===================================================================

# The largest answer of gemini-3.5-flash (65,536 output tokens): the ceiling of
# the one retry with a larger budget that a cut answer gets (R49).
_MAX_ANSWER_TOKENS = 65_536
# Thinking tokens of a bounded call on Gemini 2.5, which rejects thinking_level (R49).
_JSON_THINKING_BUDGET = 1024


def _gen_config(max_output_tokens: int, low_thinking: bool) -> types.GenerateContentConfig:
    """The generation config of one call. A bounded call (JSON answers:
    batched node summaries, topic extraction and merge, JSON repair; and the
    plain-text package and directory summaries) gets a bounded thinking budget
    (R49): thinking_level LOW on Gemini 3 and later, where the numeric
    thinking_budget is deprecated, else a token budget. The others (single
    nodes, files from nodes, topic pages) keep the model's default. Thinking
    tokens count against max_output_tokens."""
    thinking = None
    if low_thinking:
        thinking = (types.ThinkingConfig(thinking_budget=_JSON_THINKING_BUDGET)
                    if "gemini-2.5" in config.MODEL
                    else types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW))
    return types.GenerateContentConfig(temperature=0.2, max_output_tokens=max_output_tokens,
                                       thinking_config=thinking)


def _print(msg: str):
    print(msg, flush=True)


async def _sleep(seconds: float) -> None:
    """Every async wait (backoff, cooldown, circuit pause) goes through here."""
    await asyncio.sleep(seconds)


def _sync_sleep(seconds: float) -> None:
    """Blocking twin of _sleep for the Spanner and embedding retries."""
    time.sleep(seconds)


def _fmt_elapsed(seconds: float) -> str:
    """Format seconds into human readable elapsed time."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def _out_root() -> str:
    """This repo's output directory. The CLI points config.OUTPUT_DIR at
    OUTPUT_DIR/repos/<repo> for the duration of a repo's run."""
    return os.path.join(os.getcwd(), config.OUTPUT_DIR)


def _base_output_dir() -> str:
    """OUTPUT_DIR without the repos/<repo> part the CLI adds during a repo's
    run: the home of the state repos share (go/modcache, go/bin, spec §2.3)."""
    out = os.path.abspath(config.OUTPUT_DIR)
    parent = os.path.dirname(out)
    return os.path.dirname(parent) if os.path.basename(parent) == "repos" else out


def _log_error(msg: str):
    """Append an error line to the error log file."""
    out_root = _out_root()
    os.makedirs(out_root, exist_ok=True)
    log_path = os.path.join(out_root, "error_log.txt")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _write_json(path: str, payload: Any) -> None:
    """Atomic ASCII JSON (temp file + rename): no file name can make the write
    fail, and an interrupted write never leaves half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True)
    os.replace(tmp, path)


def _read_json(path: str) -> Any:
    """The JSON at *path*, or None when it is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


_CHUNK_CHARS = 800_000  # ~200K tokens normal code, ~267K for data-dense — safe under 1M


def _read_source_file(file_path: str) -> str:
    """A source file as text. Go source is UTF-8 by spec; invalid bytes become U+FFFD."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _chunk_content(content: str, chunk_size: int = _CHUNK_CHARS) -> list[str]:
    """Split content into chunks. Prefers line boundaries but will hard-cut if needed."""
    if len(content) <= chunk_size:
        return [content]
    chunks = []
    start = 0
    while start < len(content):
        end = start + chunk_size
        if end < len(content):
            # Try to break at a newline
            nl = content.rfind("\n", start, end)
            if nl > start:
                end = nl + 1
            # else: hard-cut (file has lines > chunk_size, e.g. 100K hex lines)
        chunks.append(content[start:end])
        start = end
    return chunks


def _truncate(text: str, limit: int, marker: str = " ...[TRUNCATED]") -> str:
    """Truncate text to *limit* chars, appending *marker* if cut."""
    if len(text) <= limit:
        return text
    return text[: limit - len(marker)] + marker


class _AdaptiveThrottle:
    """Global adaptive throttle — TCP congestion-control style.

    Ramps up delay quickly on rate-limit errors, ramps down slowly on successes.
    Includes a circuit breaker: if >80% of last 50 calls fail, pauses for 60s.
    """

    _WINDOW = 50
    _OPEN_THRESHOLD = 0.8
    _OPEN_PAUSE = 60.0

    def __init__(self, min_delay: float = 0.0, max_delay: float = 5.0):
        self._delay = 0.0
        self._min = min_delay
        self._max = max_delay
        self._lock = asyncio.Lock()
        # Circuit breaker state
        self._results: list[bool] = []  # True=success, False=failure
        self._circuit_open = False
        self._circuit_reopen_at: float = 0.0

    def reset(self):
        """Reset throttle state between phases."""
        self._delay = 0.0
        self._results.clear()
        self._circuit_open = False
        self._circuit_reopen_at = 0.0

    def _record(self, success: bool):
        self._results.append(success)
        if len(self._results) > self._WINDOW:
            self._results = self._results[-self._WINDOW:]

    async def on_success(self):
        async with self._lock:
            self._delay = max(self._min, self._delay - 0.1)
            self._record(True)
            if self._circuit_open:
                self._circuit_open = False
                _print("  [Throttle] Circuit breaker CLOSED — probe succeeded")

    async def on_rate_limit(self):
        async with self._lock:
            self._delay = min(self._max, self._delay + 0.5)
            self._record(False)
            if not self._circuit_open and len(self._results) >= self._WINDOW:
                fail_rate = self._results.count(False) / len(self._results)
                if fail_rate >= self._OPEN_THRESHOLD:
                    self._circuit_open = True
                    self._circuit_reopen_at = time.perf_counter() + self._OPEN_PAUSE
                    _print(f"  [Throttle] Circuit breaker OPEN — {fail_rate:.0%} failure rate, pausing {self._OPEN_PAUSE:.0f}s")

    async def wait(self):
        if self._circuit_open:
            now = time.perf_counter()
            if now < self._circuit_reopen_at:
                wait = self._circuit_reopen_at - now
                _print(f"  [Throttle] Circuit open — waiting {wait:.0f}s before probe")
                await _sleep(wait)
            self._circuit_open = False
            _print("  [Throttle] Circuit breaker probing...")
        d = self._delay
        if d > 0:
            await _sleep(d + random.uniform(0, 0.2))

    @property
    def current_delay(self) -> float:
        return self._delay


# Global throttle instance — shared across all phases
_throttle = _AdaptiveThrottle()


def _genai_client() -> genai.Client:
    """The Vertex AI Gemini client of the wiki track."""
    return genai.Client(vertexai=True, project=config.GCP_PROJECT, location=config.GCP_REGION)


def _count_call(stats: dict | None, response: Any) -> None:
    """Add one answered call and its token counts to *stats*. Thinking tokens
    are billed as output but counted apart from the answer's."""
    if stats is None:
        return
    usage = getattr(response, "usage_metadata", None)
    stats["calls"] = stats.get("calls", 0) + 1
    stats["input_tokens"] = stats.get("input_tokens", 0) + (getattr(usage, "prompt_token_count", 0) or 0)
    stats["output_tokens"] = (stats.get("output_tokens", 0)
                              + (getattr(usage, "candidates_token_count", 0) or 0))
    stats["thinking_tokens"] = (stats.get("thinking_tokens", 0)
                                + (getattr(usage, "thoughts_token_count", 0) or 0))


def _tokens_text(stats: dict) -> str:
    return (f"{stats.get('input_tokens', 0):,} input, {stats.get('output_tokens', 0):,} output, "
            f"{stats.get('thinking_tokens', 0):,} thinking tokens")


USAGE_KEYS = ("calls", "input_tokens", "output_tokens", "thinking_tokens")


def _record_usage(data: PipelineData, phase: str, stats: dict) -> None:
    """A wiki phase's Gemini calls and tokens, for the timing report (R49)."""
    data.gemini_usage[phase] = {key: stats.get(key, 0) for key in USAGE_KEYS}


class TruncatedAnswer(RuntimeError):
    """A Gemini answer cut at max_output_tokens (finish reason MAX_TOKENS), also
    after the one retry with a larger budget (R49)."""


def _finish_reason(response: Any) -> str:
    """The first candidate's finish reason as text ("STOP", "MAX_TOKENS"), or ""."""
    try:
        reason = response.candidates[0].finish_reason if response.candidates else None
    except Exception:
        return ""
    return str(getattr(reason, "value", reason) or "")


async def _generate(client: genai.Client, prompt: str, max_retries: int = 5,
                    stats: dict | None = None, *, json_answer: bool = False,
                    low_thinking: bool = False) -> str:
    """Single async Gemini call with adaptive throttle + per-call backoff.

    An empty model response (e.g. a safety block) is treated as a retryable
    failure and ultimately raised — so callers never silently accept/write
    blank output. An answer cut at max_output_tokens (finish reason MAX_TOKENS,
    with text or without, when thinking used it all) is asked once more with
    four times MAX_OUTPUT_TOKENS, at most _MAX_ANSWER_TOKENS; cut again, it
    raises TruncatedAnswer, so no caller takes a cut answer for a whole one
    (R49). *json_answer* (a JSON answer) and *low_thinking* (a plain-text call
    that needs little reasoning) bound the thinking (_gen_config). Every
    answered call is counted in *stats* (calls, input, output and thinking
    tokens).
    """
    budget = config.MAX_OUTPUT_TOKENS
    for attempt in range(max_retries):
        await _throttle.wait()
        try:
            response = await client.aio.models.generate_content(
                model=config.MODEL,
                contents=prompt,
                config=_gen_config(budget, json_answer or low_thinking),
            )
            await _throttle.on_success()
            _count_call(stats, response)
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "429" in str(e) or "rate" in err_str or "quota" in err_str or "resource" in err_str
            if is_rate_limit:
                await _throttle.on_rate_limit()
            if attempt < max_retries - 1 and is_rate_limit:
                wait_time = min(1.0 + attempt * 0.5, 3.0) + random.uniform(0, 0.5)
                await _sleep(wait_time)
                continue
            raise

        finish_reason = _finish_reason(response)
        if finish_reason == "MAX_TOKENS":
            larger = min(4 * config.MAX_OUTPUT_TOKENS, _MAX_ANSWER_TOKENS)
            if budget < larger and attempt < max_retries - 1:
                budget = larger
                continue
            raise TruncatedAnswer(f"answer cut at max_output_tokens={budget} "
                                  "(finish_reason=MAX_TOKENS); consider raising MAX_OUTPUT_TOKENS")
        try:
            text = response.text or ""
        except Exception:
            text = ""
        if text.strip():
            return text

        # Empty text: retry a few times (handles transient blanks), then fail
        # loudly so the caller logs it and queues a retry instead of writing an
        # empty file.
        if attempt < max_retries - 1:
            await _sleep(min(1.0 + attempt * 0.5, 3.0) + random.uniform(0, 0.5))
            continue
        raise RuntimeError(
            f"empty model response after {max_retries} attempts "
            f"(finish_reason={finish_reason or None})"
        )


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    return text.strip()


class UnclosedJSONError(json.JSONDecodeError):
    """The first JSON array or object of an answer never closes: the text was cut."""


def _extract_json_substring(text: str):
    """The first JSON value in *text*, an array or an object, whichever opens
    first. One that never closes raises UnclosedJSONError, never an inner
    object of it: the real F1 merge, cut, came back as its first topic (R49)."""
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        raise json.JSONDecodeError("No JSON array/object found", text, 0)
    start = min(starts)
    open_ch = text[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise UnclosedJSONError(f"Unclosed JSON {'array' if open_ch == '[' else 'object'}", text, start)


async def _parse_json_response(text: str, client: genai.Client | None = None, *,
                               stats: dict | None = None):
    """Parse JSON from LLM response: strip fences -> bracket extract -> LLM
    repair (counted in *stats*). A cut answer (UnclosedJSONError) raises at
    once: a repair could only close it over what is missing (R49)."""
    cleaned = _strip_markdown_fences(text)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    try:
        return _extract_json_substring(cleaned)
    except UnclosedJSONError:
        raise
    except (json.JSONDecodeError, ValueError):
        pass

    if client is not None:
        repair_prompt = (
            "The following text was supposed to be valid JSON but has syntax errors. "
            "Return ONLY the corrected valid JSON, nothing else:\n\n" + cleaned[:50000]
        )
        try:
            repaired = await _generate(client, repair_prompt, stats=stats, json_answer=True)
            return json.loads(_strip_markdown_fences(repaired))
        except Exception:
            pass

    raise json.JSONDecodeError("All JSON parse attempts failed", text, 0)


# ===================================================================
# Preflight (spec §2.7): one table of checks, each problem with its fix
# ===================================================================


@dataclass
class Check:
    """One preflight row. status: ok, warn, error, todo (a work package has
    not delivered the check yet) or skip (an earlier check failed)."""
    name: str
    status: str
    detail: str
    fix: str = ""


_TOOLCHAIN_FIX = ("set GO_TOOLCHAIN to a go binary, or run "
                  "`python -m graph_generator setup go`")
# `setup go` installs go1.27.2, so it does not help a newer go line.
_GO_LINE_FIX = ("set GO_TOOLCHAIN to a go binary at least as new as the go line the "
                "discovery warning names")
_MEMINFO = "/proc/meminfo"
# The helper's load took 1.5-1.7 GB on etcd and 4.7-5.3 GB on Prometheus (spec §5.1).
_LOW_MEMORY_BYTES = 4 << 30


def build_configs() -> list:
    """The build configurations of spec §3.4 (go_units.builds_from_config,
    WP-D): the primary from GO_BUILD_*, then GO_EXTRA_BUILDS. Raises
    ValueError naming a malformed entry."""
    return _handoff("go_units", "builds_from_config", "WP-D")()


def _module_mode() -> str:
    return "download" if config.GO_MODULES_DOWNLOAD else "offline"


def _check_builds() -> Check:
    try:
        builds = build_configs()
    except NotImplementedError as exc:
        return Check("builds", "todo", str(exc))
    except ValueError as exc:
        return Check("builds", "error", str(exc),
                     "set GO_EXTRA_BUILDS to goos/goarch[+tag+tag] entries separated by "
                     "spaces, and GO_BUILD_TAGS to comma-separated tags")
    return Check("builds", "ok", ", ".join(f"{b.id} (primary)" if b.primary else b.id
                                           for b in builds))


def _check_toolchain() -> tuple[Check, Any]:
    try:
        toolchain = _handoff("go_toolchain", "find_toolchain", "WP-D")()
    except NotImplementedError as exc:
        return Check("toolchain", "todo", str(exc)), None
    except Exception as exc:  # the message names the version found and the minimum
        return Check("toolchain", "error", str(exc), _TOOLCHAIN_FIX), None
    return Check("toolchain", "ok", f"{toolchain.version} ({toolchain.go})"), toolchain


def _check_helper(toolchain) -> Check:
    if toolchain is None:
        return Check("helper", "skip", "needs a toolchain")
    try:
        path = _handoff("go_toolchain", "ensure_helper", "WP-D")(toolchain, _base_output_dir())
    except NotImplementedError as exc:
        return Check("helper", "todo", str(exc))
    except Exception as exc:
        return Check("helper", "error", f"gohelper build failed: {exc}",
                     "check the toolchain; the helper builds offline from gohelper/vendor")
    return Check("helper", "ok", str(path))


def _check_memory() -> Check:
    """Memory for the helper's load; low memory only warns (R15)."""
    try:
        with open(_MEMINFO, encoding="ascii") as f:
            info = dict(line.split(":", 1) for line in f if ":" in line)
        available = int(info["MemAvailable"].split()[0]) * 1024
    except (OSError, KeyError, ValueError, IndexError):
        return Check("memory", "skip", f"MemAvailable not readable from {_MEMINFO}")
    shown = f"{available / (1 << 30):.1f} GiB available"
    if available < _LOW_MEMORY_BYTES:
        return Check("memory", "warn", f"{shown}; the helper's load used 1.7 GB on etcd and "
                                       "5.3 GB on Prometheus",
                     "free memory or use a larger machine; a load that runs out of memory "
                     "leaves its group unresolved")
    return Check("memory", "ok", shown)


def _check_repo(target_dir: str, repo: str, toolchain, peers=(),
                modules: str | None = None) -> list[Check]:
    """Discovery rows of one repo (spec §2.4, §3.3, §3.7, R15): modules and
    groups, each group's go line against the toolchain, the modules missing
    offline and the discovery warnings. go_units.stops names what stops the
    run (a repo whose every group is too new); everything else warns."""
    if toolchain is None:
        return [Check("groups", "skip", "needs a toolchain")]
    try:
        disc = _handoff("go_units", "discover", "WP-D")(
            os.path.realpath(target_dir), repo, builds=build_configs(), peers=list(peers),
            mode=modules or _module_mode(), output_dir=_base_output_dir(), toolchain=toolchain)
        stops = _handoff("go_units", "stops", "WP-D")(disc)
    except NotImplementedError as exc:
        return [Check("groups", "todo", str(exc))]
    except Exception as exc:
        return [Check("groups", "error", f"discovery failed: {exc}")]
    rows = [Check("modules", "ok", f"{len(disc.units)} modules in {len(disc.groups)} groups; "
                                   f"modules {disc.mode} ({disc.module_source})")]
    for group in disc.groups:
        detail = f"{group.kind}, {len(group.modules)} modules, status {group.status}"
        fixes = []
        if group.status == "toolchain":
            fixes.append(_GO_LINE_FIX)
        elif group.status != "ok":
            fixes.append("see the discovery warnings below")
        if group.degraded:
            detail += ", degraded"
            fixes.append("see the discovery warnings below")
        if group.missing_modules:
            detail += (f"; {len(group.missing_modules)} required modules are missing offline, "
                       "so calls into them will be unresolved")
            fixes.append("add --download-modules (or GO_MODULES_DOWNLOAD=1)")
        rows.append(Check(f"group {group.id}", "warn" if fixes else "ok", detail,
                          "; ".join(dict.fromkeys(fixes))))
    rows += [Check("go lines", "error", stop) for stop in stops]  # each names its fix
    rows += [Check("discovery", "warn", warning) for warning in disc.warnings]
    return rows


def _quota_project_problem(exc: BaseException) -> bool:
    """The error says the ADC quota project cannot be used: USER_PROJECT_DENIED
    (a deleted project, or no serviceusage.services.use on it), or no quota
    project at all. gcloud itself keeps working, so the message must say it."""
    text = str(exc)
    return (getattr(exc, "reason", None) == "USER_PROJECT_DENIED"
            or "USER_PROJECT_DENIED" in text or "quota project" in text.lower())


def _quota_project_fix() -> str:
    project = config.GCP_PROJECT or "<project>"
    return (f"run `gcloud auth application-default set-quota-project {project}`, or set "
            f"GOOGLE_CLOUD_QUOTA_PROJECT={project}; Gemini and embedding calls use the same "
            "quota project")


def _check_spanner(soft: bool) -> Check:
    """The configured database holds the NODE_SPEC / EDGE_SPEC schema: every
    table and column, each of the spec's type (setup_spanner_graph.schema_drift).
    A drifted schema would fail Phase 8 after the long phases, so it stops here."""
    from .setup_spanner_graph import (_RECREATE_FIX, _existing_column_types, _existing_tables,
                                      schema_drift)
    label = _target_label(_spanner_target(False))
    bad = "warn" if soft else "error"
    try:
        db = _get_spanner_db()
        missing_tables, missing_columns, wrong_types = schema_drift(
            _existing_tables(db), _existing_column_types(db))
    except Exception as exc:
        if _quota_project_problem(exc):
            return Check("spanner", bad,
                         f"{label}: the quota project of the Application Default Credentials "
                         f"cannot be used ({exc})", _quota_project_fix())
        return Check("spanner", bad, f"{label}: {exc}",
                     "check GOOGLE_CLOUD_PROJECT, the credentials, SPANNER_INSTANCE and "
                     "SPANNER_DATABASE; run `python -m graph_generator setup spanner`; "
                     "or add --no-spanner")

    def shown(items: list[str]) -> str:
        return ", ".join(items[:5]) + (f" and {len(items) - 5} more" if len(items) > 5 else "")

    problems = []
    if missing_tables:
        problems.append(f"{len(missing_tables)} tables missing ({shown(missing_tables)})")
    if missing_columns:
        problems.append(f"{len(missing_columns)} columns missing "
                        f"({shown([f'{t}.{c}' for t, c, _typ in missing_columns])})")
    if wrong_types:
        retyped = [f"{t}.{c} is {have}, the spec says {want}" for t, c, have, want in wrong_types]
        problems.append(f"{len(wrong_types)} columns of another type ({shown(retyped)})")
    if not problems:
        return Check("spanner", "ok", f"{label}: schema present")
    fix = (_RECREATE_FIX if wrong_types else
           "run `python -m graph_generator setup spanner` "
           "(or `python -m graph_generator.setup_spanner_graph --migrate`)")
    return Check("spanner", bad, f"{label}: " + "; ".join(problems), f"{fix}, or add --no-spanner")


def _check_gemini(soft: bool) -> Check:
    """Application Default Credentials for Vertex AI (Gemini and embeddings)."""
    import google.auth
    bad = "warn" if soft else "error"
    try:
        _credentials, project = google.auth.default()
    except Exception as exc:
        return Check("gemini", bad, f"{config.MODEL}: no Application Default Credentials ({exc})",
                     "run `gcloud auth application-default login`, or set "
                     "GOOGLE_APPLICATION_CREDENTIALS")
    shown = config.GCP_PROJECT or project or "the ADC default project"
    return Check("gemini", "ok", f"{config.MODEL} in {config.GCP_REGION}, project {shown}")


def _default_repo_name(target_dir: str) -> str:
    return os.path.basename(os.path.abspath(target_dir).rstrip(os.sep)) or "repo"


def preflight(mode: str, *, no_spanner: bool = False, repos=(), peers=None,
              modules: str | None = None) -> tuple[list[Check], Any]:
    """Every check a run needs, all at once, before any work (spec §2.7, R15).

    mode is "graph", "wiki", "all" or "doctor". repos ([(name, path)]) adds
    each repo's discovery rows in the graph modes and doctor, with its peers
    from *peers* ({name: [go_model.Peer]}) and the module source *modules*
    ("offline" | "download"). Stops are error rows: no usable toolchain or
    helper, a malformed build setting, a repo whose every group is too new,
    a missing Spanner schema without --no-spanner, missing Gemini credentials
    for the wiki track. doctor reports Spanner and Gemini problems as
    warnings, as it does not know how the user will run. Returns the rows and
    the toolchain found (None without one).
    """
    doctor = mode == "doctor"
    graph = doctor or mode in ("graph", "all")
    wiki = doctor or mode in ("wiki", "all")
    checks: list[Check] = []
    toolchain = None
    if graph:
        checks.append(_check_builds())
        row, toolchain = _check_toolchain()
        checks += [row, _check_helper(toolchain), _check_memory()]
        for repo, path in repos:
            rows = _check_repo(path, repo, toolchain, (peers or {}).get(repo, ()), modules)
            if len(repos) > 1:
                rows = [dataclasses.replace(r, name=f"{repo}: {r.name}") for r in rows]
            checks += rows
    if not no_spanner:
        checks.append(_check_spanner(soft=doctor))
    if wiki:
        checks.append(_check_gemini(soft=doctor))
    checks += topics_phase.preflight_checks(mode)  # LEIDEN-HOOK: Leiden backend, topic settings
    return checks, toolchain


def format_checks(checks: list[Check]) -> str:
    """The preflight table: one row per check, a fix line under each problem."""
    width = max([len("Check")] + [len(c.name) for c in checks])
    lines = [f"  {'Check':<{width}}  Status  Detail", f"  {'-' * width}  ------  {'-' * 40}"]
    for c in checks:
        lines.append(f"  {c.name:<{width}}  {c.status:<6}  {c.detail}")
        if c.fix and c.status in ("warn", "error"):
            lines.append(f"  {'':<{width}}          fix: {c.fix}")
    return "\n".join(lines)


# ===================================================================
# Freshness (spec §2.6) and the graph checkpoint (C7)
# ===================================================================


def source_hash(path: str) -> str:
    """SHA-256 of a file's bytes: the freshness key of spec §2.6."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ensure_fresh(data: PipelineData) -> list[str]:
    """Spec §2.6 rule 3: drop the entity records of deleted files and of files
    whose sha256 differs from this scan's hash.

    Returns the relpaths that need (re-)extraction, sorted: changed files and
    files that never had a record.
    """
    changed = deleted = 0
    for rel in list(data.extracted_entities):
        current = data.file_hashes.get(rel)
        if current is None:
            del data.extracted_entities[rel]
            deleted += 1
        elif data.extracted_entities[rel].get("sha256") != current:
            del data.extracted_entities[rel]
            changed += 1
    if changed or deleted:
        _print(f"  [Freshness] dropped entities of {changed} changed and "
               f"{deleted} deleted files")
    return sorted(rel for rel in data.file_hashes if rel not in data.extracted_entities)


def resolutions_hash(artifact: dict | None) -> str:
    """SHA-256 of resolutions.json without "stats", whose timings change on
    every run; graph_checkpoint.json stores it (C7)."""
    kept = {k: v for k, v in (artifact or {}).items() if k != "stats"}
    return hashlib.sha256(json.dumps(kept, sort_keys=True).encode("utf-8")).hexdigest()


_CHECKPOINT = "graph_checkpoint.json"
_GRAPH_META = "graph_meta.json"
_STATE = "pipeline_data.pkl"


def _spanner_target(no_spanner: bool) -> dict[str, str] | None:
    """Where Phases 8-10 write (C7); None with --no-spanner."""
    if no_spanner:
        return None
    return {"instance": config.SPANNER_INSTANCE, "database": config.SPANNER_DATABASE,
            "graph": config.GRAPH_NAME, "id_prefix": config.ID_PREFIX}


def _target_label(target: dict | None) -> str:
    if target is None:
        return "off (--no-spanner)"
    return (f"{target.get('instance')}/{target.get('database')} "
            f"(graph {target.get('graph')}, ID prefix {target.get('id_prefix')})")


def _save_graph_checkpoint(data: PipelineData, phase: str, *, complete: bool) -> None:
    """graph_checkpoint.json (C7) after a graph phase: what the graph was built
    from, where it was written, and whether Phases 8 and 9 both finished."""
    _write_json(os.path.join(_out_root(), _CHECKPOINT), {
        "id_scheme": ID_SCHEME, "complete": complete, "phase": phase,
        "file_hashes": data.file_hashes,
        "resolutions_hash": data.resolutions_digest or resolutions_hash(data.resolutions),
        "spanner": _spanner_target(data.no_spanner), "communities_hash": topics_phase.communities_hash(data)})  # LEIDEN-HOOK


def _load_graph_checkpoint(data: PipelineData) -> str | None:
    """The last graph phase ("phase8" or "phase9") finished on these inputs,
    or None to start at Phase 8.

    Resuming needs the same ID scheme, file hashes, resolutions.json and
    Spanner target; any change rewrites nodes and edges from Phase 8.
    """
    cp = _read_json(os.path.join(_out_root(), _CHECKPOINT))
    if not isinstance(cp, dict):
        return None
    if cp.get("id_scheme") != ID_SCHEME:
        _print("[Resume] graph_checkpoint.json uses another ID scheme — "
               "rebuilding nodes and edges from Phase 8")
        return None
    if (cp.get("file_hashes") != data.file_hashes
            or cp.get("resolutions_hash") != (data.resolutions_digest
                                              or resolutions_hash(data.resolutions))
            or cp.get("spanner") != _spanner_target(data.no_spanner)
            or cp.get("communities_hash") != topics_phase.communities_hash(data)):  # LEIDEN-HOOK
        _print("[Resume] the sources, resolutions.json or the Spanner target changed since "
               "graph_checkpoint.json — rewriting nodes and edges from Phase 8")
        return None
    phase = cp.get("phase")
    return phase if phase in ("phase8", "phase9") else None


def _write_graph_meta(data: PipelineData) -> None:
    """graph_meta.json: the repo, its target dir, the ID scheme and prefix and
    the Spanner target (None with --no-spanner). crossref recomputes this
    repo's node IDs from it; --wiki checks the target (spec §7.1)."""
    _write_json(os.path.join(_out_root(), _GRAPH_META), {
        "repo": data.repo, "target_dir": data.target_dir, "id_scheme": ID_SCHEME,
        "id_prefix": config.ID_PREFIX, "spanner": _spanner_target(data.no_spanner)})


def save_pipeline_data(data: PipelineData) -> None:
    """Keep the finished graph track's data (discovery, entities, resolutions)
    for a later --wiki run; like entities.json it carries every file's hash
    (spec §2.6 rule 2)."""
    path = os.path.join(_out_root(), _STATE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(data, f)
    os.replace(tmp, path)


def load_pipeline_data(target_dir: str, repo: str) -> PipelineData | None:
    """The PipelineData a --graph run saved in this repo's output dir.

    None when it cannot be read, is not PipelineData, or belongs to another
    target directory or repo. Fields added since it was written get their
    defaults.
    """
    path = os.path.join(_out_root(), _STATE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
    except Exception as e:
        _print(f"  Ignoring unreadable pipeline data ({type(e).__name__}): {path}")
        return None
    if not isinstance(data, PipelineData):
        _print(f"  Ignoring pipeline data of type {type(data).__name__}: {path}")
        return None
    # Pickle restores __dict__ only — a pkl written by an older version lacks
    # fields added since. Backfill dataclass defaults.
    for f in dataclasses.fields(PipelineData):
        if not hasattr(data, f.name):
            if f.default is not dataclasses.MISSING:
                setattr(data, f.name, f.default)
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                setattr(data, f.name, f.default_factory())
    if (os.path.realpath(data.target_dir) != os.path.realpath(target_dir)
            or (data.repo and data.repo != repo)):
        _print(f"  Ignoring pipeline data of {data.target_dir} (repo {data.repo!r}); "
               f"this run is {target_dir} (repo {repo!r})")
        return None
    return data


# ===================================================================
# Phase 1: Scan and discovery
# ===================================================================


def _scan_go_files(root: str) -> tuple[list[str], dict[str, int]]:
    """Repo-relative paths of the .go files the go command sees under *root*
    and the skip counts by reason (go_units.scan_go_files, WP-D). The whole
    output tree is pruned when it lies inside the repo: its module cache
    holds .go files."""
    return _handoff("go_units", "scan_go_files", "WP-D")(root, exclude=(_base_output_dir(),))


def _hash_sources(root: str, rels: list[str]) -> tuple[list[str], dict[str, str], int]:
    """(absolute paths, relpath → sha256, skipped count). A path that is not
    valid UTF-8 or a file that cannot be read is skipped with a warning, so it
    never reaches entities, IDs or Spanner."""
    paths: list[str] = []
    hashes: dict[str, str] = {}
    skipped = 0
    for rel in rels:
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            _print(f"  [Phase 1] warning: skipped {ascii(rel)}: the path is not valid UTF-8")
            skipped += 1
            continue
        path = os.path.join(root, rel)
        try:
            hashes[rel] = source_hash(path)
        except OSError as exc:
            _print(f"  [Phase 1] warning: skipped {rel}: {exc.strerror or exc}")
            skipped += 1
            continue
        paths.append(path)
    return paths, hashes, skipped


def _build_dir_tree(data: PipelineData) -> None:
    """The directory tree of the scanned files (absolute paths) and its
    bottom-up order, for the directory summaries and the wiki index."""
    root = data.target_dir
    children: defaultdict[str, dict] = defaultdict(lambda: {"files": [], "subdirs": set()})
    all_dirs = {root}
    for fp in data.file_list:
        parent = os.path.dirname(fp)
        children[parent]["files"].append(fp)
        all_dirs.add(parent)
        current = parent
        while current != root and current != os.path.dirname(current):
            up = os.path.dirname(current)
            children[up]["subdirs"].add(current)
            all_dirs.add(up)
            current = up
    data.dir_tree = {d: {"files": children[d]["files"], "subdirs": sorted(children[d]["subdirs"])}
                     for d in all_dirs}

    queue: list[str] = []
    seen: set[str] = set()

    def visit(d):
        if d in seen:
            return
        for sub in data.dir_tree.get(d, {}).get("subdirs", []):
            visit(sub)
        queue.append(d)
        seen.add(d)

    for d in sorted(all_dirs):
        visit(d)
    data.dir_queue = queue


def _scan(data: PipelineData) -> None:
    """The scan both tracks share: file list, hashes, skip counts, dir tree."""
    if not data.repo:
        data.repo = _default_repo_name(data.target_dir)
    # Resolved once, so the root shares its prefix with the real paths the
    # helper reports (spec §5.3).
    data.target_dir = os.path.realpath(data.target_dir)
    rels, skipped = _scan_go_files(data.target_dir)
    data.file_list, data.file_hashes, unreadable = _hash_sources(data.target_dir, rels)
    data.skipped = dict(skipped)
    if unreadable:
        data.skipped["unreadable"] = data.skipped.get("unreadable", 0) + unreadable
    _build_dir_tree(data)


def _skip_note(skipped: dict[str, int]) -> str:
    shown = ", ".join(f"{reason} {n}" for reason, n in sorted(skipped.items()) if n)
    return f" (skipped: {shown})" if shown else ""


def _discover(data: PipelineData) -> None:
    """Spec §3 through go_units.discover (WP-D): data.discovery and units.json.
    discover names the control dirs OUTPUT_DIR/repos/<repo>/go/<group>/ from
    the base output dir."""
    out_dir = _out_root()
    if data.toolchain is None:
        data.toolchain = _handoff("go_toolchain", "find_toolchain", "WP-D")()
    discover = _handoff("go_units", "discover", "WP-D")
    disc = discover(data.target_dir, data.repo, builds=build_configs(), peers=list(data.peers),
                    mode=data.mode, output_dir=_base_output_dir(), toolchain=data.toolchain)
    data.discovery = disc
    os.makedirs(out_dir, exist_ok=True)
    _handoff("go_units", "write_units_json", "WP-D")(disc, os.path.join(out_dir, "units.json"))
    for warning in disc.warnings:
        _print(f"[Phase 1] warning: {warning}")
    missing = sum(len(g.missing_modules) for g in disc.groups)
    if missing:
        _print(f"[Phase 1] {missing} required modules are missing offline, so calls into them "
               "will be unresolved (missing-module); add --download-modules")
    groups = ", ".join(f"{g.id} {g.kind} {g.status}" for g in disc.groups) or "none"
    _print(f"[Phase 1] Discovered {len(disc.units)} modules; groups: {groups}; builds: "
           f"{', '.join(b.id for b in disc.builds)}; toolchain {disc.toolchain}; "
           f"modules {disc.mode} ({disc.module_source})")


def phase1_scan(data: PipelineData) -> None:
    """Phase 1 of the graph track (spec §3): scan with the go command's skip
    rules, hash every file, discover modules, groups and builds, write units.json."""
    t0 = time.perf_counter()
    _scan(data)
    _print(f"[Phase 1] Scanned {len(data.file_list)} files in {len(data.dir_queue)} dirs"
           + _skip_note(data.skipped))
    _discover(data)
    elapsed = time.perf_counter() - t0
    data.timings["phase1_scan"] = elapsed
    _print(f"[Phase 1] Scan and discovery took {elapsed:.1f}s")


def phase1_rescan(data: PipelineData) -> None:
    """Phase 1 of the wiki track: the scan only, to compare the hashes with
    the graph's (spec §2.6)."""
    t0 = time.perf_counter()
    _scan(data)
    elapsed = time.perf_counter() - t0
    data.timings["phase1_rescan"] = elapsed
    _print(f"[Phase 1] Re-scanned {len(data.file_list)} files in {elapsed:.1f}s"
           + _skip_note(data.skipped))


def scan_source_files(root: str) -> list[str]:
    """Sorted repo-relative paths of the source files under *root*, by Phase 1's
    rules (the go command's skip rules; unreadable and non-UTF-8 paths skipped
    with a warning). Used for peer repos and the freshness checks."""
    real = os.path.realpath(root)
    rels, _skipped = _scan_go_files(real)
    return sorted(_hash_sources(real, rels)[1])


# ===================================================================
# Phases 1.5 and 1.6: the helper (WP-B2)
# ===================================================================


def phase1b_extract(data: PipelineData) -> None:
    """Phase 1.5 (spec §4) through go_extract.extract_phase (WP-B2): `gohelper
    extract`, reuse by sha256 and reuse stamp (spec §2.6), entities.json (C3).
    Sets data.extracted_entities and data.timings["phase1b_extract"]."""
    _handoff("go_extract", "extract_phase", "WP-B2")(
        data, out_root=_out_root(), output_dir=_base_output_dir(), printer=_print)


def phase1c_resolve(data: PipelineData) -> None:
    """Phase 1.6 (spec §5) through resolution.resolve_phase (WP-B2): fresh
    entities, control dirs, downloads, `gohelper resolve` per group and build,
    classification and gates, resolutions.json (C5). Sets data.resolutions and
    data.timings["phase1c_resolve"]."""
    _handoff("resolution", "resolve_phase", "WP-B2")(
        data, out_root=_out_root(), output_dir=_base_output_dir(), printer=_print)


# ===================================================================
# Builders (WP-B3) and Spanner helpers
# ===================================================================


def _make_id(kind: str, repo: str, relpath: str, qualname: str) -> str:
    """Node ID (spec §6.4): ID_PREFIX + 16 hex chars of sha256 over
    (kind, repo, repo-relative path, qualified name). The parts are joined
    with NUL, which no path or name contains, so distinct inputs never hash
    the same text. The module path is a column, never an ID input.
    surrogateescape is a safety net: Phase 1 already skips non-UTF-8 paths."""
    raw = "\x00".join((kind, repo, relpath, qualname))
    h = hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return f"{config.ID_PREFIX}_{h}"


def _edge_id(table: str, source: str, target: str) -> str:
    """Edge ID: a hash of (table, source node ID, target node ID) (spec §6.3)."""
    return _make_id("edge", table, source, target)


def _relpath(data: PipelineData, path: str) -> str:
    """Repo-relative POSIX path of *path*; "" for the repo root itself."""
    rel = os.path.relpath(path, data.target_dir)
    return "" if rel == "." else rel.replace(os.sep, "/")


def _claim_id(owners: dict[str, tuple], node_id: str, owner: tuple) -> bool:
    """Record that *owner* holds *node_id* (the siblings' guard, spec §4.2).

    False for a repeat of the same owner (that record is written once); a
    different owner with the same ID is a collision and raises.
    """
    previous = owners.get(node_id)
    if previous is None:
        owners[node_id] = owner
        return True
    if previous != owner:
        raise RuntimeError(f"node ID collision: {previous} and {owner} map to {node_id}")
    return False


def build_node_rows(data: PipelineData) -> dict[str, list[dict]]:
    """Node rows of spec §6.1 as table → [column dict] (C6), pure: Files,
    Packages, Classes, Methods and Directories, IDs from _make_id
    (graph_rows.build_node_rows)."""
    from .graph_rows import build_node_rows as build
    return build(data)


def _node_index(data: PipelineData) -> dict[str, Any]:
    """Every node ID of this repo and the lookups derive_edge_rows needs
    (graph_rows.node_index)."""
    from .graph_rows import node_index
    return node_index(data)


def derive_edge_rows(data: PipelineData, node_index: dict[str, Any]) -> dict[str, list[dict]]:
    """Edge rows of spec §6.3 as table → [column dict] (C6), pure: MethodCalls,
    PossiblyCalls, PackageImports, ClassEmbeds, ClassImplements, FileDependsOn
    and the structural edges, only from records whose ends are nodes of this repo
    (graph_rows.derive_edge_rows; its result's `stats` counts what was not edged)."""
    from .graph_rows import derive_edge_rows as derive
    return derive(data, node_index)


def _columns(table: str) -> list[str]:
    """The insert columns of a table, from NODE_SPEC / EDGE_SPEC."""
    from .setup_spanner_graph import write_columns
    return write_columns(table)


def _row(columns: list[str], values: dict[str, Any]) -> list[Any]:
    """A row in the spec's column order; a missing column raises KeyError."""
    return [values[c] for c in columns]


def _get_spanner_db():
    """Return a Spanner database handle."""
    client = spanner.Client(project=config.GCP_PROJECT, disable_builtin_metrics=True)
    instance = client.instance(config.SPANNER_INSTANCE)
    return instance.database(config.SPANNER_DATABASE)


def _batch_insert(db, table: str, columns: list[str], rows: list[list], max_retries: int = 5):
    """Insert rows in batches via mutations with retry on transient errors."""
    _batch_mutate(db, "insert_or_update", table, columns, rows, max_retries)


def _batch_update(db, table: str, columns: list[str], rows: list[list], max_retries: int = 5,
                  batch_size: int | None = None):
    """Update existing rows in batches, with the same retries (Phase 10)."""
    _batch_mutate(db, "update", table, columns, rows, max_retries, batch_size)


# Spanner allows 80,000 mutations per commit and counts an insert or update
# once per column it writes (cloud.google.com/spanner/quotas); half of that
# leaves room for index entries.
_MUTATIONS_PER_COMMIT = 40_000


def _batch_mutate(db, op: str, table: str, columns: list[str], rows: list[list], max_retries: int,
                  batch_size: int | None = None):
    from google.api_core.exceptions import Aborted, ServiceUnavailable, DeadlineExceeded

    batch_size = min(batch_size or config.SPANNER_BATCH_SIZE,
                     max(1, _MUTATIONS_PER_COMMIT // max(1, len(columns))))
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for attempt in range(max_retries):
            try:
                with db.batch() as txn:
                    getattr(txn, op)(table=table, columns=columns, values=batch)
                break
            except (Aborted, ServiceUnavailable, DeadlineExceeded) as e:
                if attempt == max_retries - 1:
                    _log_error(f"[Spanner] {table} batch {i} failed after {max_retries} attempts: {e}")
                    raise
                _sync_sleep(2 ** attempt + random.uniform(0, 1))


def _secondary_index_count(table: str) -> int:
    from .setup_spanner_graph import SECONDARY_INDEXES
    return len(SECONDARY_INDEXES.get(table, ()))


def _batch_delete(db, table: str, keys: list, max_retries: int = 5) -> None:
    """Delete rows by primary key, as KeySet delete mutations in batches with
    the retries of _batch_mutate. A deleted row counts as one mutation plus
    one per secondary-index entry (cloud.google.com/spanner/quotas), so a
    commit holds at most SPANNER_BATCH_SIZE and 40,000 // (1 + indexes) keys."""
    from google.api_core.exceptions import Aborted, ServiceUnavailable, DeadlineExceeded
    from google.cloud.spanner_v1 import KeySet

    batch_size = min(config.SPANNER_BATCH_SIZE,
                     max(1, _MUTATIONS_PER_COMMIT // (1 + _secondary_index_count(table))))
    for i in range(0, len(keys), batch_size):
        keyset = KeySet(keys=[[key] for key in keys[i : i + batch_size]])
        for attempt in range(max_retries):
            try:
                with db.batch() as txn:
                    txn.delete(table, keyset)
                break
            except (Aborted, ServiceUnavailable, DeadlineExceeded) as e:
                if attempt == max_retries - 1:
                    _log_error(f"[Spanner] {table} delete batch {i} failed after "
                               f"{max_retries} attempts: {e}")
                    raise
                _sync_sleep(2 ** attempt + random.uniform(0, 1))


# ===================================================================
# Phases 8-9: write nodes and edges (Spanner, or graph_rows/*.jsonl)
# ===================================================================


def _graph_rows_dir() -> str:
    return os.path.join(_out_root(), "graph_rows")


def _write_jsonl(table: str, rows: list[dict]) -> None:
    """graph_rows/<Table>.jsonl (C6): one row per line, written atomically."""
    path = os.path.join(_graph_rows_dir(), f"{table}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
    os.replace(tmp, path)


def _clear_graph_rows() -> None:
    """Drop the previous run's jsonl files when Phase 8 starts, so a table
    without rows in this run leaves no stale file behind."""
    rows_dir = _graph_rows_dir()
    if os.path.isdir(rows_dir):
        for name in os.listdir(rows_dir):
            if name.endswith(".jsonl"):
                os.remove(os.path.join(rows_dir, name))


def _write_rows(data: PipelineData, rows: dict[str, list[dict]], *,
                parallel: bool = False) -> dict[str, int]:
    """Write builder rows (table → [column dict]): always to
    graph_rows/<Table>.jsonl, where the wiki track reads its graph context
    (R20), and unless --no-spanner to Spanner as idempotent upserts in
    NODE_SPEC / EDGE_SPEC column order. Returns the row count per table."""
    counts = {table: len(table_rows) for table, table_rows in rows.items()}
    for table, table_rows in rows.items():
        _write_jsonl(table, table_rows)
    if not data.no_spanner:
        db = _get_spanner_db()

        def write(table: str) -> None:
            columns = _columns(table)
            _batch_insert(db, table, columns, [_row(columns, r) for r in rows[table]])

        tables = [table for table, table_rows in rows.items() if table_rows]
        if parallel and len(tables) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(tables)) as pool:
                list(pool.map(write, tables))
        else:
            for table in tables:
                write(table)
    data.graph_counts.update(counts)
    return counts


def _destination(data: PipelineData) -> str:
    if data.no_spanner:
        return "graph_rows/"
    return f"graph_rows/ and Spanner {_target_label(_spanner_target(False))}"


def _counts_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{table} {n}" for table, n in counts.items() if n) or "none"


# R48: re-ingest removes the rows the code no longer has. Phase 8 records the
# primary keys of the previous rows; Phase 9 deletes those it did not write.
# graph_prune.json keeps one record per Spanner target.
_PRUNE = "graph_prune.json"
_PRUNE_VERSION = 2


class PruneError(RuntimeError):
    """The rows this run wrote cannot be read back, so nothing is pruned."""


class _UnreadableRows(Exception):
    """A graph_rows/*.jsonl file that cannot be parsed."""


def _graph_row_keys(tables) -> dict[str, set[str] | None]:
    """table → the primary keys in graph_rows/<Table>.jsonl; None when the
    file is missing. Raises _UnreadableRows naming a file that does not parse."""
    from .setup_spanner_graph import EDGE_SPEC, NODE_SPEC
    out: dict[str, set[str] | None] = {}
    for table in tables:
        pk = (NODE_SPEC.get(table) or EDGE_SPEC[table])[0]
        path = os.path.join(_graph_rows_dir(), f"{table}.jsonl")
        try:
            with open(path, encoding="utf-8") as f:
                out[table] = {json.loads(line)[pk] for line in f if line.strip()}
        except FileNotFoundError:
            out[table] = None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise _UnreadableRows(f"graph_rows/{table}.jsonl: {exc}") from exc
    return out


def _pruned_tables() -> tuple[str, ...]:
    """The tables Phases 8-9 write and prune (not the wiki's topic tables)."""
    from .graph_rows import EDGE_ROW_TABLES, NODE_ROW_TABLES
    return (*NODE_ROW_TABLES, *EDGE_ROW_TABLES)


def _own_keys(keys) -> set[str]:
    """The keys shaped like this run's IDs (ID_PREFIX + 16 hex digits): a
    side-by-side graph under another ID_PREFIX is never recorded or deleted."""
    import re
    pattern = re.compile(rf"{re.escape(config.ID_PREFIX)}_[0-9a-f]{{16}}")
    return {key for key in keys if isinstance(key, str) and pattern.fullmatch(key)}


def _prune_path() -> str:
    return os.path.join(_out_root(), _PRUNE)


def _read_prune_targets(data: PipelineData, phase: str) -> list[dict]:
    """The records of graph_prune.json, one per Spanner target ([] when there
    is no file; an unreadable file is reported and ignored)."""
    doc = _read_json(_prune_path())
    if doc is None:
        return []
    if (isinstance(doc, dict) and doc.get("version") == _PRUNE_VERSION
            and doc.get("repo") == data.repo and isinstance(doc.get("targets"), list)):
        return [t for t in doc["targets"] if isinstance(t, dict) and isinstance(t.get("tables"), dict)]
    _print(f"[{phase}] warning: ignoring an unreadable graph_prune.json; its rows are not pruned")
    return []


def _write_prune_targets(data: PipelineData, targets: list[dict]) -> None:
    """graph_prune.json with the records that still hold keys, or no file."""
    targets = [t for t in targets if any(t["tables"].values())]
    if targets:
        _write_json(_prune_path(), {"version": _PRUNE_VERSION, "repo": data.repo,
                                    "targets": targets})
    elif os.path.exists(_prune_path()):
        os.remove(_prune_path())


def _split_targets(targets: list[dict], target: dict) -> tuple[dict | None, list[dict]]:
    """(the record of *target* or None, the records of the other targets)."""
    mine = [t for t in targets if t.get("spanner") == target]
    return (mine[0] if mine else None), [t for t in targets if t.get("spanner") != target]


def _repo_has_rows(db, repo: str) -> bool:
    """One cheap read: the target holds Files rows of this repo (a recreated
    database holds none, so nothing there needs pruning)."""
    with db.snapshot() as snapshot:
        rows = snapshot.execute_sql("SELECT 1 FROM Files WHERE repo = @repo LIMIT 1",
                                    params={"repo": repo},
                                    param_types={"repo": spanner.param_types.STRING})
        return any(True for _row in rows)


def _record_previous_rows(data: PipelineData) -> None:
    """R48 recording, before Phase 8 rewrites graph_rows/: the primary keys of
    the previous rows of every pruned table join this target's record in
    graph_prune.json (which may hold keys an interrupted run left). Recorded
    only when this run writes to Spanner, graph_meta.json names this repo and
    this target, and the repo has rows in the target; only keys of this
    ID_PREFIX are recorded. Records of other targets are kept for them."""
    if data.no_spanner:
        return
    target = _spanner_target(False)
    mine, others = _split_targets(_read_prune_targets(data, "Phase 8"), target)
    tables = {t: _own_keys(keys) for t, keys in (mine or {}).get("tables", {}).items()
              if t in _pruned_tables()}
    meta = _read_json(os.path.join(_out_root(), _GRAPH_META))
    if (isinstance(meta, dict) and meta.get("repo") == data.repo
            and meta.get("spanner") == target and _repo_has_rows(_get_spanner_db(), data.repo)):
        for table in _pruned_tables():
            try:
                keys = _graph_row_keys([table])[table]
            except _UnreadableRows as exc:   # fewer recorded keys only means fewer deletes
                _print(f"[Phase 8] warning: cannot read {exc}; its previous rows are not pruned")
                continue
            if keys:
                tables.setdefault(table, set()).update(_own_keys(keys))
    _write_prune_targets(data, others + [
        {"spanner": target, "tables": {t: sorted(keys) for t, keys in tables.items() if keys}}])


def _file_topic_keys(db, file_ids: list[str]) -> list[str]:
    """edge_id of the FileBelongsToTopic rows of these files (Phase 10 wrote
    them; graph_rows/ may no longer list them)."""
    keys: list[str] = []
    for i in range(0, len(file_ids), 1000):
        with db.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                "SELECT edge_id FROM FileBelongsToTopic WHERE file_id IN UNNEST(@ids)",
                params={"ids": file_ids[i : i + 1000]},
                param_types={"ids": spanner.param_types.Array(spanner.param_types.STRING)})
            keys += [row[0] for row in rows if isinstance(row[0], str)]
    return keys


def _prune_stale_rows(data: PipelineData) -> dict[str, int]:
    """R48 pruning, after the Phase 9 upserts (also when Phase 8 was resumed
    from an earlier run): delete the keys of this target's record that this
    run did not write, edges first, then the FileBelongsToTopic rows of
    deleted files, then the nodes. A table whose jsonl is missing keeps its
    keys for the next run; rows this run wrote that cannot be read back stop
    the run (PruneError) before any delete, and run_graph_track makes the
    next run rewrite them from Phase 8 and prune. Returns the deleted key
    count per table."""
    if data.no_spanner:
        return {}
    target = _spanner_target(False)
    mine, others = _split_targets(_read_prune_targets(data, "Phase 9"), target)
    if mine is None:
        return {}
    try:
        written = _graph_row_keys(_pruned_tables())
    except _UnreadableRows as exc:
        raise PruneError(f"cannot read {exc}. Nothing was deleted; run the same command again: "
                         "it rewrites the rows from Phase 8 and removes the stale ones") from exc
    recorded = {t: set(keys) for t, keys in mine["tables"].items() if t in _pruned_tables()}
    stale = {t: sorted(_own_keys(keys - written[t]))
             for t, keys in recorded.items() if written.get(t) is not None}
    waiting = {t: sorted(keys) for t, keys in recorded.items() if written.get(t) is None}
    from .graph_rows import EDGE_ROW_TABLES, NODE_ROW_TABLES
    db = _get_spanner_db() if any(stale.values()) else None
    removed: dict[str, int] = {}
    for table in EDGE_ROW_TABLES:
        if stale.get(table):
            _batch_delete(db, table, stale[table])
            removed[table] = len(stale[table])
    if stale.get("Files"):
        topic_links = _file_topic_keys(db, stale["Files"])
        if topic_links:
            _batch_delete(db, "FileBelongsToTopic", topic_links)
            removed["FileBelongsToTopic"] = len(topic_links)
    for table in NODE_ROW_TABLES:
        if stale.get(table):
            _batch_delete(db, table, stale[table])
            removed[table] = len(stale[table])
    _write_prune_targets(data, others + [{"spanner": target, "tables": waiting}])
    if removed:
        _print(f"[Phase 9] Removed {sum(removed.values())} stale rows: " + _counts_text(removed))
    return removed


def phase8_write_nodes(data: PipelineData) -> None:
    """Phase 8: build_node_rows, written to Spanner or graph_rows/. The
    previous rows' keys are recorded for the R48 prune first; then
    graph_meta.json names this run's target before graph_rows/ is rewritten,
    so an interrupted run never leaves this run's keys under another
    target's name. With Spanner, the wiki's summaries that still describe
    the sources are re-applied after the rewrite (_keep_wiki_summaries)."""
    t0 = time.perf_counter()
    rows = build_node_rows(data)
    _record_previous_rows(data)
    _write_graph_meta(data)
    _clear_graph_rows()
    counts = _write_rows(data, rows)
    kept = {} if data.no_spanner else _keep_wiki_summaries(data, rows)
    elapsed = time.perf_counter() - t0
    data.timings["phase8_write_nodes"] = elapsed
    _print(f"[Phase 8] Wrote {sum(counts.values())} node rows to {_destination(data)} "
           f"in {elapsed:.1f}s: " + _counts_text(counts))
    if any(kept.values()):
        _print(f"[Phase 8] Kept {sum(kept.values())} wiki summaries from the summary cache: "
               + _counts_text(kept))


def _keep_wiki_summaries(data: PipelineData, rows: dict[str, list[dict]]) -> dict[str, int]:
    """Re-apply to Spanner the cached wiki summaries of the node rows Phase 8
    just rewrote with doc comments (graph_rows.cached_summaries), so a --graph
    after --wiki keeps them (spec §7.4). graph_rows/*.jsonl keeps the doc
    comments, which Phase 2 reads. Returns the count per table."""
    from .graph_rows import cached_summaries
    updates = cached_summaries(data, rows)
    if any(updates.values()):
        db = _get_spanner_db()
        for table, pairs in updates.items():
            if pairs:
                _batch_update(db, table, [_ID_COLUMNS[table], "summary"], [list(p) for p in pairs])
    return {table: len(pairs) for table, pairs in updates.items()}


def phase9_write_edges(data: PipelineData) -> None:
    """Phase 9: derive_edge_rows, written like Phase 8, then the R48 prune of
    the rows this run no longer has (_prune_stale_rows). What was counted and
    not edged (spec §5.4, §6.3), with the external imports per module, goes to
    the run's output and to graph_rows/stats.json."""
    t0 = time.perf_counter()
    rows = derive_edge_rows(data, _node_index(data))
    counts = _write_rows(data, rows, parallel=True)
    _prune_stale_rows(data)
    elapsed = time.perf_counter() - t0
    data.timings["phase9_write_edges"] = elapsed
    _print(f"[Phase 9] Wrote {sum(counts.values())} edge rows to {_destination(data)} "
           f"in {elapsed:.1f}s: " + _counts_text(counts))
    not_edged = dict(sorted((getattr(rows, "stats", None) or {}).items()))
    by_module = sorted((getattr(rows, "external_imports", None) or {}).items(),
                       key=lambda item: (-item[1], item[0]))
    _write_json(os.path.join(_graph_rows_dir(), "stats.json"),
                {"not_edged": not_edged, "external_imports": dict(by_module)})
    if any(not_edged.values()):
        _print("[Phase 9] Not edged: " + _counts_text(not_edged))
    if by_module:
        more = f" and {len(by_module) - 5} more modules" if len(by_module) > 5 else ""
        _print("[Phase 9] External imports by module: " + _counts_text(dict(by_module[:5])) + more)


def _print_graph_summary(data: PipelineData) -> None:
    """The status summary that ends the graph track (spec §2.7)."""
    by_status = (data.resolutions or {}).get("stats", {}).get("by_status", {})
    _print("[Graph] Sites by status: "
           + (", ".join(f"{status} {n}" for status, n in by_status.items()) or "none"))
    _print("[Graph] Rows written: " + (_counts_text(data.graph_counts) if data.graph_counts
                                       else "none (unchanged since the last run)"))


def run_graph_track(data: PipelineData) -> None:
    """Phases 1 → 1.5 → 1.6 → 8 → 9 (spec §2.1); Phases 8-9 resume from
    graph_checkpoint.json, and the finished data is kept for --wiki."""
    phase1_scan(data)
    phase1b_extract(data)
    phase1c_resolve(data)
    # Hashed once for the checkpoints below; the artifact is large.
    data.resolutions_digest = resolutions_hash(data.resolutions)
    topics_phase.phase4a_communities(data)  # LEIDEN-HOOK: Phase 4a (L-R24)
    last = _load_graph_checkpoint(data)
    if last is None:
        # Marked incomplete before any row is rewritten: an interrupted run
        # never passes the --wiki precondition.
        _save_graph_checkpoint(data, "phase1.6", complete=False)
        phase8_write_nodes(data)
        _save_graph_checkpoint(data, "phase8", complete=False)
    else:
        _print("[Resume] Skipping Phase 8 (already completed on these inputs)")
    if last != "phase9":
        try:
            phase9_write_edges(data)
        except PruneError:
            # The rows cannot be read back: the next run rewrites them from Phase 8.
            _save_graph_checkpoint(data, "phase1.6", complete=False)
            raise
        topics_phase.write_graph_topics(data)  # LEIDEN-HOOK: the five topic tables (R10)
        _save_graph_checkpoint(data, "phase9", complete=True)
    else:
        _print("[Resume] Skipping Phase 9 (already completed on these inputs)")
    save_pipeline_data(data)
    _print_graph_summary(data)


# ===================================================================
# Wiki preconditions (spec §7.1)
# ===================================================================

_MAX_LISTED_FILES = 50


def _graph_command(target_dir: str) -> str:
    return f"`python -m graph_generator analyze {target_dir} --graph`"


def _require_graph(repo: str, target_dir: str) -> tuple[dict, dict]:
    """graph_checkpoint.json and graph_meta.json of a finished graph of this
    repo built with this ID scheme (spec §7.1 rows 1-2)."""
    out = _out_root()
    cp = _read_json(os.path.join(out, _CHECKPOINT))
    meta = _read_json(os.path.join(out, _GRAPH_META))
    if (not isinstance(cp, dict) or not cp.get("complete") or not isinstance(meta, dict)
            or meta.get("repo") != repo):
        raise WikiPreconditionError(
            f"No finished graph for repo {repo!r} in {out}: run --graph first "
            f"({_graph_command(target_dir)}).")
    if cp.get("id_scheme") != ID_SCHEME:
        raise WikiPreconditionError(
            f"The graph of repo {repo!r} uses ID scheme {cp.get('id_scheme')}; this version "
            f"uses {ID_SCHEME}: run --graph first ({_graph_command(target_dir)}).")
    if not os.path.isdir(_graph_rows_dir()):
        raise WikiPreconditionError(
            f"{_graph_rows_dir()} is missing; the wiki track reads its graph context there: "
            f"run --graph first ({_graph_command(target_dir)}).")
    return cp, meta


def load_graph_state(target_dir: str, repo: str) -> PipelineData:
    """The data the finished --graph run of this repo kept, checked against
    graph_checkpoint.json; else WikiPreconditionError naming the fix."""
    cp, _meta = _require_graph(repo, target_dir)
    data = load_pipeline_data(target_dir, repo)
    if data is None or data.file_hashes != cp.get("file_hashes"):
        raise WikiPreconditionError(
            f"The saved graph state of repo {repo!r} is missing or does not match "
            f"graph_checkpoint.json: run --graph first ({_graph_command(target_dir)}).")
    data.timings, data.graph_counts = {}, {}
    return data


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    lines = []
    for rel in sorted(set(before) | set(after)):
        if rel not in after:
            lines.append(f"deleted  {rel}")
        elif rel not in before:
            lines.append(f"added    {rel}")
        elif before[rel] != after[rel]:
            lines.append(f"changed  {rel}")
    return lines


def prepare_wiki(data: PipelineData, *, no_spanner: bool) -> None:
    """The wiki track's Phase 1 and its preconditions (spec §7.1).

    Re-scans, then refuses with every problem and its fix when the files
    differ from the graph's, when the graph was built with --no-spanner but
    this run writes to Spanner, or when the Spanner target differs. A
    --no-spanner run writes nothing to Spanner, so no target has to match.
    Sets data.no_spanner.
    """
    cp, meta = _require_graph(data.repo, data.target_dir)
    phase1_rescan(data)
    problems = []
    changed = _changed_files(cp.get("file_hashes") or {}, data.file_hashes)
    if changed:
        listed = changed[:_MAX_LISTED_FILES]
        if len(changed) > _MAX_LISTED_FILES:
            listed.append(f"... and {len(changed) - _MAX_LISTED_FILES} more")
        problems.append(f"{len(changed)} files differ from the graph:\n"
                        + "\n".join(f"  {line}" for line in listed)
                        + f"\nRun {_graph_command(data.target_dir)} (or `--all`) first.")
    if not no_spanner:
        recorded = meta.get("spanner")
        configured = _spanner_target(False)
        if recorded is None:
            problems.append(
                "The graph was built with --no-spanner, so it is not in Spanner. Run --wiki "
                "with --no-spanner (summaries and pages are then written locally only), or "
                f"rebuild the graph without --no-spanner ({_graph_command(data.target_dir)} "
                "or `--all`).")
        elif recorded != configured:
            problems.append(
                f"The graph was written to Spanner {_target_label(recorded)}, but the "
                f"configured target is {_target_label(configured)}. Set SPANNER_INSTANCE, "
                "SPANNER_DATABASE, GRAPH_NAME and ID_PREFIX back, or build the graph for the "
                f"configured target ({_graph_command(data.target_dir)}).")
    problems += topics_phase.wiki_problems(data, cp)  # LEIDEN-HOOK: communities (leiden spec §2)
    if problems:
        raise WikiPreconditionError("\n\n".join(problems))
    data.no_spanner = no_spanner


# ===================================================================
# Phases 2-3: node, package and directory summaries
# ===================================================================


def summary_cache_dir(repo: str) -> str:
    """The summary cache (R11): WIKI_SUMMARY_CACHE/<repo>, which codedoc_go and
    codedoc_go_leiden may share, else OUTPUT_DIR/repos/<repo>/summaries."""
    if config.WIKI_SUMMARY_CACHE:
        return os.path.join(os.path.abspath(os.path.expanduser(config.WIKI_SUMMARY_CACHE)), repo)
    return os.path.join(_out_root(), "summaries")


def _graph_rows(data: PipelineData) -> dict[str, list[dict]]:
    """The rows Phases 8-9 wrote to graph_rows/*.jsonl (R20), read once per run."""
    if not data.graph_rows:
        rows: dict[str, list[dict]] = {}
        rows_dir = _graph_rows_dir()
        for name in sorted(os.listdir(rows_dir)) if os.path.isdir(rows_dir) else []:
            if name.endswith(".jsonl"):
                with open(os.path.join(rows_dir, name), encoding="utf-8") as f:
                    rows[name[:-len(".jsonl")]] = [json.loads(line) for line in f if line.strip()]
        data.graph_rows = rows
    return data.graph_rows


def _packages(data: PipelineData, rows: dict[str, list[dict]]) -> dict[str, dict]:
    """"<dir>|<name>" → {"import_path", "directory", "name", "files"} of every
    package, from the Files rows (the entities when a file has no row)."""
    files = {r.get("path"): r for r in rows.get("Files", [])}
    out: dict[str, dict] = {}
    for rel in sorted(data.file_hashes):
        row = files.get(rel) or {}
        ent = data.extracted_entities.get(rel) or {}
        name = row.get("package_name") or ent.get("package_name")
        if not name:
            continue
        directory = row.get("directory", rel.rpartition("/")[0])
        pkg = out.setdefault(f"{directory}|{name}", {
            "import_path": row.get("package") or ent.get("package") or "",
            "directory": directory, "name": name, "files": []})
        pkg["files"].append(rel)
    return out


def _children_hash(children: list[tuple[str, str]]) -> str:
    """Cache key of a package or directory summary: its children's summaries (C7)."""
    payload = json.dumps([PROMPT_VERSION, config.MODEL, sorted(children)], ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _summary_cache(data: PipelineData, name: str) -> dict:
    """summaries/<name>.json from the first summary directory that has it."""
    from .node_summaries import summary_dirs
    for directory in summary_dirs(data):
        cached = _read_json(os.path.join(directory, f"{name}.json"))
        if isinstance(cached, dict):
            return cached
    return {}


def estimate_wiki_calls(data: PipelineData) -> dict[str, int]:
    """Gemini calls the wiki track expects (R11): Phase 2 exactly, after its
    cache; Phases 3-5 as upper bounds, since their caches may hold."""
    from . import node_summaries
    rows = _graph_rows(data)
    phase2 = node_summaries.estimate(data, rows)
    chunks = _topic_chunks(data, lambda _data, fp: fp)  # every file: no summary is written yet
    return {"phase2": phase2["calls"], "files": phase2["files"], "cached": phase2["cached"],
            "generated": phase2["generated"],
            "phase3": len(_packages(data, rows)) + len(data.dir_queue),
            "phase4": len(chunks) + 1, "phase5": 15,
            **topics_phase.estimate_calls(data)}  # LEIDEN-HOOK: 4b batches, 5 pages


def _estimate_text(e: dict[str, int]) -> str:
    return (f"[Wiki] Estimated Gemini calls: Phase 2 = {e['phase2']} ({e['files']} files to "
            f"summarize; {e['cached']} cached, {e['generated']} generated without a call); "
            f"Phases 3-5 at most {e['phase3']} + {e['phase4']} + {e['phase5']}, fewer when "
            f"their caches hold")


async def phase2_node_summaries(data: PipelineData, client: genai.Client) -> None:
    """Phase 2 (spec §7.2) through node_summaries. Sets data.node_summaries
    and data.file_summaries (relpath keys)."""
    from . import node_summaries
    _throttle.reset()
    result = await node_summaries.summarize(data, client, _graph_rows(data))
    data.timings["phase2_node_summaries"] = result["seconds"]
    _record_usage(data, "phase2_node_summaries", result)
    _print(f"[Phase 2] {len(data.file_summaries)} file summaries in "
           f"{_fmt_elapsed(result['seconds'])}: batched {result.get('batched', 0)}, "
           f"per-node {result.get('per-node', 0)}, cached {result.get('cached', 0)}, "
           f"generated {result.get('generated', 0)}; {result['calls']} Gemini calls "
           f"({_tokens_text(result)})"
           + (f"; {result['incomplete']} incomplete, retried on the next run (see error_log.txt)"
              if result.get("incomplete") else ""))


async def phase3_package_dir_summaries(data: PipelineData, client: genai.Client) -> None:
    """Phase 3 (spec §7.3): one summary per package from its files' summaries
    (summaries/packages.json) and one per directory, bottom-up, from its files
    and subdirectories (summaries/dirs.json, "" = the repo root). A summary is
    reused while the hash of its children's summaries is unchanged (C7)."""
    MAX_PHASE_RETRIES = 3
    RETRY_COOLDOWNS = [30, 90, 180]
    from .node_summaries import summary_dirs, write_json

    t0 = time.perf_counter()
    _throttle.reset()
    stats: dict[str, int] = {}
    sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)
    packages = _packages(data, _graph_rows(data))
    caches = {"packages": _summary_cache(data, "packages"), "dirs": _summary_cache(data, "dirs")}
    written: dict[str, dict] = {"packages": {}, "dirs": {}}
    reused = 0

    def _block(items: list[tuple[str, str]]) -> str:
        return "\n\n".join(f"**{name}**: {_truncate(text, 1500)}" for name, text in items)

    async def summarize(kind: str, key: str, children: list[tuple[str, str]], prompt: str,
                        target: dict[str, str]) -> bool:
        nonlocal reused
        digest = _children_hash(children)
        hit = caches[kind].get(key)
        if isinstance(hit, dict) and hit.get("hash") == digest and hit.get("summary"):
            summary = hit["summary"]
            reused += 1
        else:
            try:
                async with sem:
                    summary = (await _generate(client, prompt, stats=stats,
                                               low_thinking=True)).strip()
            except Exception as e:
                _log_error(f"[Phase 3] {kind} summary failed: {key or '.'}: {e}")
                return False
        target[key] = summary
        written[kind][key] = {"hash": digest, "summary": summary}
        return True

    async def package(key: str) -> bool:
        pkg = packages[key]
        children = [(rel, data.file_summaries[rel]) for rel in pkg["files"]
                    if data.file_summaries.get(rel)]
        if not children:
            return True
        prompt = PACKAGE_SUMMARY_PROMPT.format(
            import_path=pkg["import_path"] or "-", directory=pkg["directory"] or ".",
            name=pkg["name"], file_summaries=_block(children))
        return await summarize("packages", key, children, prompt, data.package_summaries)

    async def directory(dp: str) -> bool:
        node = data.dir_tree.get(dp, {"files": [], "subdirs": []})
        files = [(os.path.basename(fp), data.file_summaries.get(_relpath(data, fp), ""))
                 for fp in node["files"]]
        subdirs = [(os.path.basename(sd) + "/", data.dir_summaries.get(_relpath(data, sd), ""))
                   for sd in node["subdirs"]]
        files, subdirs = [c for c in files if c[1]], [c for c in subdirs if c[1]]
        if not files and not subdirs:
            return True
        rel_dir = _relpath(data, dp)
        prompt = DIR_SUMMARY_PROMPT.format(dir_name=rel_dir or ".",
                                           child_file_summaries=_block(files) or "(なし)",
                                           child_dir_summaries=_block(subdirs) or "(なし)")
        return await summarize("dirs", rel_dir, files + subdirs, prompt, data.dir_summaries)

    async def run_with_retries(label: str, job, keys: list[str]) -> list[str]:
        results = await asyncio.gather(*(job(k) for k in keys))
        failed = [k for k, ok in zip(keys, results) if not ok]
        for retry_round in range(1, MAX_PHASE_RETRIES + 1):
            if not failed:
                break
            cooldown = RETRY_COOLDOWNS[min(retry_round - 1, len(RETRY_COOLDOWNS) - 1)]
            _print(f"  [Phase 3] Waiting {cooldown}s, then retry round {retry_round}: "
                   f"{len(failed)} {label}")
            await _sleep(cooldown)
            results = await asyncio.gather(*(job(k) for k in failed))
            failed = [k for k, ok in zip(failed, results) if not ok]
        return failed

    failed = await run_with_retries("packages", package, sorted(packages))
    depth = {dp: _relpath(data, dp).count("/") + bool(_relpath(data, dp)) for dp in data.dir_queue}
    for level in sorted(set(depth.values()), reverse=True):  # bottom-up
        failed += await run_with_retries(
            "directories", directory, [dp for dp in data.dir_queue if depth[dp] == level])

    for directory_path in summary_dirs(data):
        for kind in ("packages", "dirs"):
            write_json(os.path.join(directory_path, f"{kind}.json"), written[kind])
    elapsed = time.perf_counter() - t0
    data.timings["phase3_package_dir_summaries"] = elapsed
    _record_usage(data, "phase3_package_dir_summaries", stats)
    _print(f"[Phase 3] {len(data.package_summaries)} package and {len(data.dir_summaries)} "
           f"directory summaries in {_fmt_elapsed(elapsed)} ({reused} reused); "
           f"{stats.get('calls', 0)} Gemini calls ({_tokens_text(stats)})"
           + (f"; {len(failed)} failed (see error_log.txt)" if failed else ""))


# ===================================================================
# Phase 4: Topics (concurrent fan-out + merge; files linked by relpath)
# ===================================================================

# Files per topic-extraction chunk at most (R49). The extraction answer lists
# every file of its chunk, so a top-level directory above the cap is split by
# subdirectory: no prompt or answer grows with the repo.
_TOPIC_CHUNK_FILES = 60
# Topics Phase 4 keeps at most (the merge prompt asks for 3-15); beyond them,
# and for chunk topics the merge left out, the topic "その他" (R49).
_MAX_TOPICS = 15
_OTHER_TOPIC = "その他"


def _summary_line(data: PipelineData, fp: str) -> str | None:
    rel = _relpath(data, fp)
    summary = data.file_summaries.get(rel, "")
    return f"**{rel}**: {summary[:300]}" if summary else None


def _pack(units: list[tuple[str, list[str]]], cap: int) -> list[tuple[str, list[str]]]:
    """(label, lines) units in order, neighbours grouped while a group holds
    at most *cap* lines; a group's label lists its units' labels."""
    groups: list[tuple[list[str], list[str]]] = []
    for label, lines in units:
        if groups and len(groups[-1][1]) + len(lines) <= cap:
            if label not in groups[-1][0]:
                groups[-1][0].append(label)
            groups[-1][1].extend(lines)
        else:
            groups.append(([label], list(lines)))
    return [(", ".join(labels), lines) for labels, lines in groups]


def _topic_chunks(data: PipelineData, line=_summary_line) -> list[tuple[str, list[str]]]:
    """Phase 4's extraction chunks as (label, one *line* per file): one per
    top-level directory, plus the files at the root (spec §7.3). A directory
    above _TOPIC_CHUNK_FILES files splits into its own files, sliced at the
    cap, and its subdirectories (split again when above it), small neighbours
    grouped up to the cap (R49). Every chunk has a label of its own: slices of
    one directory are "dir (1/3)", "dir (2/3)" and so on."""
    cap = _TOPIC_CHUNK_FILES
    empty = {"files": [], "subdirs": []}

    def own(dp: str) -> list[str]:
        return [text for fp in data.dir_tree.get(dp, empty)["files"] if (text := line(data, fp))]

    def subtree(dp: str) -> list[str]:
        return own(dp) + [text for sub in data.dir_tree.get(dp, empty)["subdirs"]
                          for text in subtree(sub)]

    def slices(label: str, lines: list[str]) -> list[tuple[str, list[str]]]:
        parts = [lines[i:i + cap] for i in range(0, len(lines), cap)]
        if len(parts) == 1:
            return [(label, parts[0])]
        return [(f"{label} ({n}/{len(parts)})", part) for n, part in enumerate(parts, 1)]

    def children(dp: str) -> list[tuple[str, list[str]]]:
        """A chunk per subdirectory of *dp*, those above the cap split."""
        out: list[tuple[str, list[str]]] = []
        for sub in data.dir_tree.get(dp, empty)["subdirs"]:
            lines = subtree(sub)
            if len(lines) > cap:
                out += _pack(slices(_relpath(data, sub), own(sub)) + children(sub), cap)
            elif lines:
                out.append((_relpath(data, sub), lines))
        return out

    return slices(".", own(data.target_dir)) + children(data.target_dir)


def _coerce_topic_list(parsed: Any) -> list[dict]:
    """Normalize an LLM topic-JSON response into a list of topic dicts.

    Tolerates the model returning a bare list, a wrapper object such as
    {"topics": [...]}, or a single topic object. Returns [] for anything
    unusable — this prevents a malformed (non-array) response from silently
    wiping out the topic list.
    """
    if isinstance(parsed, list):
        return [t for t in parsed if isinstance(t, dict)]
    if isinstance(parsed, dict):
        if "name" in parsed:  # a single topic object
            return [parsed]
        for value in parsed.values():  # a wrapper like {"topics": [...]}
            if isinstance(value, list):
                return [t for t in value if isinstance(t, dict)]
    return []


def _known_relpaths(values: Any, known: set[str], dropped: list[str]) -> list[str]:
    """The paths in *values* that name scanned files, in order and once each.

    Topics link files by repo-relative path only (spec §7.3). A bare file name
    is never matched, because names like doc.go repeat; unknown paths go to
    *dropped*.
    """
    out: list[str] = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, str):
            continue
        rel = value.strip().replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        if rel in known:
            if rel not in out:
                out.append(rel)
        elif rel:
            dropped.append(rel)
    return out


def _text(value: Any) -> str:
    """An answer's text field on one line; "" when it is no text."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def _clean_topic(raw: Any, known: set[str], dropped: list[str]) -> dict | None:
    """An answered topic as {"name", "summary", "files", "subtopics": [{"name",
    "summary", "files"}]}, its paths filtered to scanned relpaths (unknown ones
    go to *dropped*); None without a name."""
    name = str(raw.get("name", "")).strip() if isinstance(raw, dict) else ""
    if not name:
        return None
    raw_subs = raw.get("subtopics")
    subtopics = []
    for sub in raw_subs if isinstance(raw_subs, list) else []:
        sub_name = str(sub.get("name", "")).strip() if isinstance(sub, dict) else ""
        if sub_name:
            subtopics.append({"name": sub_name, "summary": _text(sub.get("summary")),
                              "files": _known_relpaths(sub.get("files", sub.get("linked_files")),
                                                       known, dropped)})
    return {"name": name, "summary": _text(raw.get("summary")),
            "files": _known_relpaths(raw.get("files", raw.get("linked_files")), known, dropped),
            "subtopics": subtopics}


def _join_into(same: dict, topic: dict) -> None:
    """Join *topic* into *same*, the topic of its name: files and subtopics
    united, subtopics of one name joined in turn, so no file is lost."""
    same["files"] += [rel for rel in topic["files"] if rel not in same["files"]]
    subs = {sub["name"]: sub for sub in same["subtopics"]}
    for sub in topic["subtopics"]:
        if sub["name"] in subs:
            subs[sub["name"]]["files"] += [rel for rel in sub["files"]
                                           if rel not in subs[sub["name"]]["files"]]
        else:
            subs[sub["name"]] = {**sub, "files": list(sub["files"])}
            same["subtopics"].append(subs[sub["name"]])
    same["summary"] = same["summary"] or topic["summary"]


def _topic_tree(raw_topics: list[dict], data: PipelineData) -> tuple[list[dict], list[str]]:
    """Gemini's topics as the topic tree of C7 — {"id", "name", "summary",
    "files", "subtopics": [{"name", "summary", "files"}]} — with the files
    filtered to scanned relpaths. The id is the Topics node ID (spec §6.4), so
    topics of one name are joined, never dropped. Returns the tree and the
    dropped paths."""
    known = set(data.file_hashes)
    dropped: list[str] = []
    tree: list[dict] = []
    by_name: dict[str, dict] = {}
    for raw in raw_topics:
        topic = _clean_topic(raw, known, dropped)
        if topic is None:
            continue
        if topic["name"] in by_name:
            _join_into(by_name[topic["name"]], topic)
            continue
        by_name[topic["name"]] = {"id": _make_id("topic", data.repo, topic["name"], ""), **topic}
        tree.append(by_name[topic["name"]])
    return tree, dropped


def _merge_entries(chunk_topics: list[dict]) -> str:
    """The merge prompt's input (R49): a numbered line per chunk topic with its
    name, file count and summary, and no file list."""
    return "\n".join(f"{n}. {t['name']}（{len(topic_file_relpaths(t))} ファイル）"
                     + (f": {t['summary'][:200]}" if t["summary"] else "")
                     for n, t in enumerate(chunk_topics, 1))


def _members(values: Any, count: int, invalid: list) -> list[int]:
    """The chunk-topic numbers 1..*count* in *values*, in order and once each;
    anything else goes to *invalid*."""
    out: list[int] = []
    for value in values if isinstance(values, list) else []:
        number = int(value) if isinstance(value, str) and value.strip().isdecimal() else value
        if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= count:
            if number not in out:
                out.append(number)
        else:
            invalid.append(value)
    return out


def _subtopic_of(topic: dict) -> dict:
    return {"name": topic["name"], "summary": topic["summary"], "files": topic_file_relpaths(topic)}


def _with_other(topics: list[dict], others: list[dict]) -> list[dict]:
    """*topics*, and *others* as subtopics of the topic "その他" (joined into
    one of that name if the model made it)."""
    if not others:
        return topics
    subtopics = [_subtopic_of(t) for t in others]
    files = list(dict.fromkeys(rel for sub in subtopics for rel in sub["files"]))
    other = next((t for t in topics if t["name"] == _OTHER_TOPIC), None)
    if other is None:
        return [*topics, {"name": _OTHER_TOPIC, "summary": "", "files": files, "subtopics": subtopics}]
    other["files"] += [rel for rel in files if rel not in other["files"]]
    other["subtopics"] += subtopics
    return topics


def _merged_topics(answer: list[dict], chunk_topics: list[dict], known: set[str],
                   dropped: list[str]) -> tuple[list[dict], int, int]:
    """The merge answer's topics with their files (R49). A topic or subtopic
    takes the union of its members' files, plus any path it lists; a topic of
    one member and no subtopics keeps that chunk topic's subtopics; topics of
    one name are joined. A chunk topic no topic names joins the topic of its
    name, or, with a file no topic links, goes to "その他", as do the smallest
    topics beyond _MAX_TOPICS: no file is lost, and at most _MAX_TOPICS topics
    remain. Returns the topics ([] when the answer has none with a file), the
    member numbers that name no chunk topic and the chunk topics left out."""
    invalid: list = []
    used: set[int] = set()
    out: list[dict] = []
    by_name: dict[str, dict] = {}

    def files(numbers: list[int], listed: Any) -> list[str]:
        rels: list[str] = []
        for rel in [*(r for n in numbers for r in topic_file_relpaths(chunk_topics[n - 1])),
                    *_known_relpaths(listed, known, dropped)]:
            if rel not in rels:
                rels.append(rel)
        return rels

    def add(topic: dict) -> None:
        if topic["name"] in by_name:
            _join_into(by_name[topic["name"]], topic)
        else:
            by_name[topic["name"]] = topic
            out.append(topic)

    for raw in answer:
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        members = _members(raw.get("members"), len(chunk_topics), invalid)
        raw_subs = raw.get("subtopics")
        subtopics = []
        for sub in raw_subs if isinstance(raw_subs, list) else []:
            sub_name = str(sub.get("name", "")).strip() if isinstance(sub, dict) else ""
            if sub_name:
                sub_members = _members(sub.get("members"), len(chunk_topics), invalid)
                used.update(sub_members)
                subtopics.append({"name": sub_name, "summary": _text(sub.get("summary")),
                                  "files": files(sub_members, sub.get("files"))})
        used.update(members)
        if len(members) == 1 and not subtopics:
            subtopics = [{**sub, "files": list(sub["files"])}
                         for sub in chunk_topics[members[0] - 1]["subtopics"]]
        topic = {"name": name, "summary": _text(raw.get("summary")),
                 "files": files(members, raw.get("files")), "subtopics": subtopics}
        if topic_file_relpaths(topic):
            add(topic)
    if not out:
        return [], len(invalid), len(chunk_topics)
    left = [t for n, t in enumerate(chunk_topics, 1) if n not in used]
    for topic in left:
        if topic["name"] in by_name:
            add(topic)
    linked = {rel for t in out for rel in topic_file_relpaths(t)}
    homeless = [t for t in left if t["name"] not in by_name
                and not set(topic_file_relpaths(t)) <= linked]
    fold = 0
    if homeless or len(out) > _MAX_TOPICS:  # room for "その他" among the _MAX_TOPICS
        fold = max(0, len(out) - (_MAX_TOPICS - 1))
    smallest = set(sorted(range(len(out)), key=lambda i: (len(topic_file_relpaths(out[i])), -i))[:fold])
    kept = [t for i, t in enumerate(out) if i not in smallest]
    return (_with_other(kept, [t for i, t in enumerate(out) if i in smallest] + homeless),
            len(invalid), len(left))


def _fallback_topics(by_chunk: list[tuple[str, list[dict]]]) -> list[dict]:
    """The topics when the merge gives no usable answer (R49): a topic per
    chunk, named by its labels (unique, "ルート" for the root's files), its
    chunk topics as subtopics, neighbour chunks sharing a topic so at most
    _MAX_TOPICS remain. Every file a chunk topic links stays linked."""
    groups = [(label, topics) for label, topics in by_chunk if topics]
    size = max(1, -(-len(groups) // _MAX_TOPICS))
    out = []
    for i in range(0, len(groups), size):
        part = groups[i:i + size]
        subtopics = [_subtopic_of(t) for _, topics in part for t in topics]
        out.append({"name": ", ".join("ルート" + label[1:] if label.startswith(".") else label
                                      for label, _ in part),
                    "summary": "", "subtopics": subtopics,
                    "files": list(dict.fromkeys(rel for sub in subtopics for rel in sub["files"]))})
    return out


def topic_file_relpaths(topic: dict) -> list[str]:
    """Every file a topic links, its subtopics' included, in order and once
    each: the files of its page (Phase 5) and its FileBelongsToTopic rows."""
    out: list[str] = []
    for rel in [*topic.get("files", []),
                *(r for sub in topic.get("subtopics", []) for r in sub.get("files", []))]:
        if rel not in out:
            out.append(rel)
    return out


def _write_topic_tree(data: PipelineData) -> None:
    topics_dir = os.path.join(_out_root(), "topics")
    os.makedirs(topics_dir, exist_ok=True)
    with open(os.path.join(topics_dir, "topic_tree.json"), "w", encoding="utf-8") as f:
        json.dump(data.topics, f, indent=2, ensure_ascii=False)


def _topic_inputs_hash(data: PipelineData) -> str:
    """What topics and pages are built from: the file summaries and sources."""
    payload = json.dumps([PROMPT_VERSION, config.MODEL, sorted(data.file_summaries.items()),
                          sorted(data.file_hashes.items())], ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reuse_topics(data: PipelineData, digest: str) -> bool:
    """Spec §7.4: topics and pages are rebuilt only when a summary changed.
    True (data.topics and the pages on disk loaded) when topics/inputs.json
    records *digest* and topic_tree.json is readable."""
    topics_dir = os.path.join(_out_root(), "topics")
    inputs = _read_json(os.path.join(topics_dir, "inputs.json"))
    tree = _read_json(os.path.join(topics_dir, "topic_tree.json"))
    if not (isinstance(inputs, dict) and inputs.get("hash") == digest and isinstance(tree, list)
            and tree and all(isinstance(t, dict) and t.get("id") and t.get("name") for t in tree)):
        return False
    data.topics = tree
    for topic in tree:
        page = os.path.join(topics_dir, f"{topic['id']}.md")
        if os.path.isfile(page):
            with open(page, encoding="utf-8") as f:
                text = f.read()
            if text.strip():
                data.topic_summaries[topic["id"]] = text
    return True


def _write_topic_stats(data: PipelineData) -> None:
    """topics/stats.json (C7): calls, tokens and seconds of topic discovery and
    pages. A step that made no call this run keeps its earlier numbers."""
    path = os.path.join(_out_root(), "topics", "stats.json")
    stats = _read_json(path)
    stats = stats if isinstance(stats, dict) else {}
    for key in ("discovery", "pages"):
        new = data.topic_stats.get(key)
        if new is not None and (new.get("calls") or key not in stats):
            stats[key] = new
        stats.setdefault(key, _step_stats({}, 0.0))
    _write_json(path, stats)


def _step_stats(stats: dict, seconds: float) -> dict:
    return {"calls": stats.get("calls", 0), "input_tokens": stats.get("input_tokens", 0),
            "output_tokens": stats.get("output_tokens", 0),
            "thinking_tokens": stats.get("thinking_tokens", 0), "seconds": round(seconds, 3)}


async def phase4_topics(data: PipelineData, client: genai.Client):
    t0 = time.perf_counter()
    _throttle.reset()
    digest = _topic_inputs_hash(data)
    if _reuse_topics(data, digest):
        data.timings["phase4_topics"] = time.perf_counter() - t0
        _record_usage(data, "phase4_topics", {})
        _print(f"[Phase 4] {len(data.topics)} topics reused: no file summary changed")
        return
    stats: dict[str, int] = {}

    # One chunk per top-level directory, plus one for the files at the root;
    # a chunk above _TOPIC_CHUNK_FILES files is split by subdirectory (R49).
    chunks: list[tuple[str, str]] = []
    for label, lines in _topic_chunks(data):
        text = "\n\n".join(lines)
        if len(text) > 200000:
            text = text[:200000] + "\n\n... [TRUNCATED]"
        chunks.append((label, text))

    _print(f"[Phase 4] Fan-out: {len(chunks)} topic extraction chunks")

    sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)
    failed: list[str] = []

    async def extract_chunk(label: str, summaries_text: str) -> Any:
        async with sem:
            prompt = TOPIC_EXTRACTION_PROMPT.format(summaries=summaries_text)
            try:
                text = await _generate(client, prompt, stats=stats, json_answer=True)
                return await _parse_json_response(text, client=client, stats=stats)
            except Exception as e:
                _log_error(f"[Phase 4] topic extraction failed: {label}: {e}")
                failed.append(label)
                return []

    chunk_results = await asyncio.gather(*[extract_chunk(label, st) for label, st in chunks])

    # The chunk topics (a chunk may answer with a wrapper object instead of a
    # list), their paths filtered to scanned files; a topic left with no file
    # carries nothing
    known = set(data.file_hashes)
    dropped: list[str] = []
    by_chunk = [(label, [topic for raw in _coerce_topic_list(answer)
                         if (topic := _clean_topic(raw, known, dropped)) and topic_file_relpaths(topic)])
                for (label, _), answer in zip(chunks, chunk_results)]
    for label, topics in by_chunk:
        if not topics and label not in failed:  # it answered, but linked no file
            _log_error(f"[Phase 4] topic extraction gave no usable topic: {label}")
            failed.append(label)
    chunk_topics = [topic for _, topics in by_chunk for topic in topics]

    # Merge: the chunk topics go in as numbers, names, file counts and
    # summaries, and the answer names its topics' members by number (R49).
    # Without a merge, _topic_tree joins chunk topics of one name.
    if len(chunk_topics) <= _MAX_TOPICS:
        merged = chunk_topics
    else:
        merged = []
        merge_prompt = TOPIC_MERGE_PROMPT.format(topics=_merge_entries(chunk_topics))
        try:
            text = await _generate(client, merge_prompt, stats=stats, json_answer=True)
            answer = _coerce_topic_list(await _parse_json_response(text, client=client, stats=stats))
            merged, invalid, left = _merged_topics(answer, chunk_topics, known, dropped)
            if merged and invalid:
                _log_error(f"[Phase 4] {invalid} member numbers name no chunk topic")
            if merged and left:
                _log_error(f"[Phase 4] {left} chunk topics in no merged topic: joined to a topic "
                           f"of their name or to 「{_OTHER_TOPIC}」 when their files were unlinked")
        except Exception as e:
            _log_error(f"[Phase 4] topic merge failed: {e}")
        if not merged:
            # Merge produced no usable list (e.g. the model emitted a non-array
            # object, or the answer was cut). Fall back to one topic per chunk
            # so topics are never silently lost.
            _log_error("[Phase 4] topic merge produced no usable list; "
                       "falling back to one topic per chunk")
            failed.append("merge")
            merged = _fallback_topics(by_chunk)

    data.topics, _ = _topic_tree(merged, data)  # its paths are scanned files by now
    dropped = sorted(set(dropped))  # a path several chunk topics name counts once
    if dropped:
        _log_error(f"[Phase 4] {len(dropped)} topic file paths match no scanned file: "
                   f"{dropped[:20]}")
    data.topic_summaries = {}  # new topics get new pages
    if data.topics:
        _write_topic_tree(data)
    inputs = os.path.join(_out_root(), "topics", "inputs.json")
    if data.topics and not failed:
        _write_json(inputs, {"hash": digest})
    else:
        if failed:
            _log_error(f"[Phase 4] {len(failed)} calls failed ({', '.join(failed[:10])}); "
                       "topics are not cached: the next --wiki builds them again")
        if os.path.exists(inputs):
            os.remove(inputs)

    linked = {rel for topic in data.topics for rel in topic_file_relpaths(topic)}
    elapsed = time.perf_counter() - t0
    data.topic_stats["discovery"] = _step_stats(stats, elapsed)
    data.timings["phase4_topics"] = elapsed
    _record_usage(data, "phase4_topics", stats)
    _print(f"[Phase 4] Extracted {len(data.topics)} topics from {len(chunk_topics)} chunk topics, "
           f"linking {len(linked)}/{len(data.file_hashes)} files in {elapsed:.1f}s"
           + (f" ({len(dropped)} unknown paths dropped)" if dropped else "")
           + (f"; {len(failed)} calls failed (see error_log.txt), so the next --wiki builds the "
              "topics again" if failed else ""))


# ===================================================================
# Phase 5: Topic pages (concurrent)
# ===================================================================


async def phase5_topic_pages(data: PipelineData, client: genai.Client):
    MAX_PHASE_RETRIES = 3
    RETRY_COOLDOWNS = [30, 90, 180]
    t0 = time.perf_counter()
    _throttle.reset()

    if not data.topics:
        _print("[Phase 5] No topics to write pages for")
        data.timings["phase5_topic_pages"] = 0.0
        _record_usage(data, "phase5_topic_pages", {})
        _write_topic_stats(data)
        return

    pending = [t for t in data.topics if t["id"] not in data.topic_summaries]
    if not pending:
        _print(f"[Phase 5] All {len(data.topics)} topic pages already written, skipping")
        data.timings["phase5_topic_pages"] = 0.0
        _record_usage(data, "phase5_topic_pages", {})
        _write_topic_stats(data)
        return
    stats: dict[str, int] = {}

    _print(f"[Phase 5] Writing {len(pending)}/{len(data.topics)} topic pages")

    topics_dir = os.path.join(_out_root(), "topics")
    os.makedirs(topics_dir, exist_ok=True)

    sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)
    p5_done = 0
    p5_total = len(pending)
    failed_topics: list[dict] = []
    failed_lock = asyncio.Lock()

    async def write_one_page(topic: dict) -> None:
        nonlocal p5_done
        async with sem:
            file_contents_parts = []
            file_summary_parts = []
            for rel in topic_file_relpaths(topic):
                try:
                    content = _read_source_file(os.path.join(data.target_dir, rel))
                    file_contents_parts.append(f"### {rel}\n```go\n{content[:15000]}\n```")
                except OSError:
                    pass
                s = data.file_summaries.get(rel, "")
                if s:
                    file_summary_parts.append(f"**{rel}**: {_truncate(s, 500)}")

            prompt = TOPIC_SUMMARY_PROMPT.format(
                topic_name=topic["name"],
                file_contents="\n\n".join(file_contents_parts)[:200000],
                file_summaries="\n\n".join(file_summary_parts),
            )
            try:
                page = await _generate(client, prompt, stats=stats)
                data.topic_summaries[topic["id"]] = page
                with open(os.path.join(topics_dir, f"{topic['id']}.md"), "w", encoding="utf-8") as f:
                    f.write(page)
                p5_done += 1
                elapsed = time.perf_counter() - t0
                print(f"\r  [Phase 5] {p5_done}/{p5_total} {_fmt_elapsed(elapsed)} | {topic['name']}          ", end="", flush=True)
            except Exception as e:
                p5_done += 1
                _log_error(f"[Phase 5] topic page failed: {topic['name']}: {e}")
                async with failed_lock:
                    failed_topics.append(topic)

    await asyncio.gather(*[write_one_page(t) for t in pending])

    # Retry failed topics with cooldown
    for retry_round in range(1, MAX_PHASE_RETRIES + 1):
        if not failed_topics:
            break
        retry_batch = failed_topics.copy()
        failed_topics.clear()
        print()
        cooldown = RETRY_COOLDOWNS[min(retry_round - 1, len(RETRY_COOLDOWNS) - 1)]
        _print(f"  [Phase 5] Waiting {cooldown}s for rate limit recovery...")
        await _sleep(cooldown)
        _print(f"  [Phase 5] Retry round {retry_round}: {len(retry_batch)} topics")
        await asyncio.gather(*[write_one_page(t) for t in retry_batch])

    print()
    elapsed = time.perf_counter() - t0
    data.timings["phase5_topic_pages"] = elapsed
    _record_usage(data, "phase5_topic_pages", stats)
    data.topic_stats["pages"] = _step_stats(stats, elapsed)
    _write_topic_stats(data)
    msg = f"[Phase 5] Completed: {len(data.topic_summaries)}/{len(data.topics)} topic pages in {_fmt_elapsed(elapsed)}"
    if failed_topics:
        msg += f" ({len(failed_topics)} failed — see error_log.txt)"
    _print(msg)


# ===================================================================
# Phase 6: Wiki index
# ===================================================================


def _first_prose_line(text: str) -> str:
    """The first line of *text* that is neither blank nor a Markdown heading:
    a topic page opens with its own `# name` heading and section headings."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def phase6_index(data: PipelineData):
    t0 = time.perf_counter()
    out_root = _out_root()

    # The topic tree, each topic's summary taken from its page (C7)
    for topic in data.topics:
        topic["summary"] = data.topic_summaries.get(topic["id"], "")[:500]
    _write_topic_tree(data)
    # Pages of topics that no longer exist go away with them.
    current = {f"{t['id']}.md" for t in data.topics} | topics_phase.page_names(data)  # LEIDEN-HOOK
    topics_dir = os.path.join(out_root, "topics")
    for name in os.listdir(topics_dir):
        if name.endswith(".md") and name not in current:
            os.remove(os.path.join(topics_dir, name))

    # Build reasoning_index.json
    def build_node(path):
        node_info = data.dir_tree.get(path, {"files": [], "subdirs": []})
        children = []
        for fp in node_info["files"]:
            rel = _relpath(data, fp)
            children.append({
                "path": rel,
                "type": "file",
                "name": os.path.basename(fp),
                "summary": data.file_summaries.get(rel, "")[:500],
            })
        for dp in node_info["subdirs"]:
            children.append(build_node(dp))
        rel_dir = _relpath(data, path)
        return {
            "path": rel_dir,
            "type": "directory",
            "name": os.path.basename(path),
            "summary": data.dir_summaries.get(rel_dir, "")[:500],
            "children": children,
        }

    summary_tree = build_node(data.target_dir)
    index_data = {"summary_tree": summary_tree, "topic_tree": data.topics}
    json_path = os.path.join(out_root, "reasoning_index.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2, ensure_ascii=False)

    # Build index.md
    md_lines = [f"# {data.repo} ドキュメント\n"]
    md_lines.append("## はじめに\n")
    md_lines.append(f"{summary_tree.get('summary', '')}\n")
    md_lines.append("## トピック\n")
    for t in data.topics:
        first_line = _first_prose_line(t.get("summary", ""))[:150]
        md_lines.append(f"*   **[{t['name']}](topics/{t['id']}.md)**: {first_line}")
    md_lines.append("")

    # Component table
    md_lines.append("## コンポーネント一覧\n")
    md_lines.append("| ファイル | 概要 |")
    md_lines.append("|------|---------|")
    for count, (rel, s) in enumerate(sorted(data.file_summaries.items())):
        if count >= 200:
            md_lines.append(f"| ... | 他 {len(data.file_summaries) - 200} ファイル |")
            break
        first_line = s.split("\n")[0][:80] if s else ""
        md_lines.append(f"| {rel} | {first_line} |")

    index_md_path = os.path.join(out_root, "index.md")
    with open(index_md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    # Metadata
    metadata = {
        "generation_info": {
            "timestamp": datetime.now().isoformat(),
            "model": config.MODEL,
            "generator": "codedoc_go wiki track (google-genai)",
            "repo": data.repo,
            "repo_path": data.target_dir,
        },
        "statistics": {
            "total_files": len(data.file_summaries),
            "total_directories": len(data.dir_summaries),
            "total_topics": len(data.topics),
        },
    }
    metadata_path = os.path.join(out_root, "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    elapsed = time.perf_counter() - t0
    data.timings["phase6_index"] = elapsed
    _print(f"[Phase 6] Index assembled in {elapsed:.1f}s")


# ===================================================================
# Phase 10: write-back and embeddings (WP-B4)
# ===================================================================


def _embedding_model():
    """The Vertex text-embedding model (EMBED_MODEL)."""
    import vertexai
    from vertexai.language_models import TextEmbeddingModel

    vertexai.init(project=config.GCP_PROJECT, location="us-central1")
    return TextEmbeddingModel.from_pretrained(config.EMBED_MODEL)


def _embed(model, texts: list[str]) -> list:
    if config.EMBED_DIMENSION:
        return model.get_embeddings(texts, output_dimensionality=config.EMBED_DIMENSION)
    return model.get_embeddings(texts)


def _embed_and_write(model, db, items: list[tuple[str, str]], table: str, id_col: str,
                     label: str, vectors: dict[str, list[float]] | None = None) -> int:
    """Embed (id, text) items in concurrent batches with retry and write each
    vector to table.embedding; failed IDs are logged and reported. Returns the
    number embedded; *vectors* receives each new vector by ID."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    t0 = time.perf_counter()
    embed_lock = threading.Lock()
    total_batches = (len(items) + config.EMBED_BATCH_SIZE - 1) // config.EMBED_BATCH_SIZE
    embedded = 0
    all_failed: list[str] = []

    def _do_batch(batch_items: list[tuple[str, str]], batch_idx: int) -> tuple[int, list[str]]:
        texts = [item[1] for item in batch_items]
        for attempt in range(5):
            try:
                embeddings = _embed(model, texts)
                rows = [[item_id, list(emb.values)] for (item_id, _), emb in zip(batch_items, embeddings)]
                with db.batch() as txn:
                    txn.update(table=table, columns=[id_col, "embedding"], values=rows)
                return len(batch_items), [], rows
            except Exception as e:
                err_str = str(e)
                if ("429" in err_str or "Resource exhausted" in err_str) and attempt < 4:
                    _sync_sleep(2 ** attempt * 2 + random.uniform(0, 1))
                    continue
                _log_error(f"[Phase 10] {label} batch {batch_idx} failed after {attempt+1} attempts: {e}")
                return 0, [item_id for item_id, _ in batch_items], []
        return 0, [item_id for item_id, _ in batch_items], []

    with ThreadPoolExecutor(max_workers=config.EMBED_CONCURRENCY) as pool:
        futures = {}
        for i in range(0, len(items), config.EMBED_BATCH_SIZE):
            batch = items[i : i + config.EMBED_BATCH_SIZE]
            futures[pool.submit(_do_batch, batch, i)] = i

        done_count = 0
        for future in as_completed(futures):
            count, failed_ids, rows = future.result()
            if vectors is not None:
                vectors.update({item_id: values for item_id, values in rows})
            with embed_lock:
                embedded += count
                if failed_ids:
                    all_failed.extend(failed_ids)
                done_count += 1
                if done_count % 100 == 0 or done_count == total_batches:
                    elapsed = time.perf_counter() - t0
                    print(f"\r  [Phase 10] {label}: {done_count}/{total_batches} batches ({embedded} embedded) {_fmt_elapsed(elapsed)}          ", end="", flush=True)

    print()
    if all_failed:
        _log_error(f"[Phase 10] {label}: {len(all_failed)} items failed embedding: {all_failed[:20]}")
        _print(f"  [Phase 10] WARNING: {len(all_failed)} {label} failed embedding — see error_log.txt")
    return embedded


_ID_COLUMNS = {"Files": "file_id", "Packages": "package_id", "Classes": "class_id",
               "Methods": "method_id", "Directories": "dir_id", "Topics": "topic_id"}
_SUMMARY_LIMIT = 4000


def _summary_updates(data: PipelineData, rows: dict[str, list[dict]]) -> dict[str, list[tuple[str, str]]]:
    """(node ID, summary) per table for the summary columns (spec §7.5). The
    <package> pseudo-type takes its file's summary; only nodes of the graph
    rows are updated."""
    path_of = {r.get("file_id"): r.get("path", "") for r in rows.get("Files", [])}
    out: dict[str, list[tuple[str, str]]] = {t: [] for t in _ID_COLUMNS if t != "Topics"}

    def add(table: str, node_id: str, summary: str | None) -> None:
        if node_id and summary:
            out[table].append((node_id, summary[:_SUMMARY_LIMIT]))

    for r in rows.get("Files", []):
        add("Files", r.get("file_id"), data.file_summaries.get(r.get("path", "")))
    for r in rows.get("Packages", []):
        add("Packages", r.get("package_id"),
            data.package_summaries.get(f"{r.get('directory', '')}|{r.get('name', '')}"))
    for r in rows.get("Classes", []):
        rel = path_of.get(r.get("file_id"), "")
        add("Classes", r.get("class_id"),
            data.file_summaries.get(rel) if r.get("kind") == "package"
            else data.node_summaries.get(rel, {}).get(r.get("qualname", "")))
    for r in rows.get("Methods", []):
        add("Methods", r.get("method_id"),
            data.node_summaries.get(path_of.get(r.get("file_id"), ""), {}).get(r.get("qualname", "")))
    dir_ids = {r.get("dir_id") for r in rows.get("Directories", [])}
    for rel_dir, summary in data.dir_summaries.items():
        node_id = _make_id("dir", data.repo, rel_dir, "")
        if node_id in dir_ids:
            add("Directories", node_id, summary)
    return out


def _topic_rows(data: PipelineData, rows: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    """Topics and FileBelongsToTopic rows (spec §6.2); files link by relpath."""
    file_id = {r.get("path"): r.get("file_id") for r in rows.get("Files", [])}
    topics, belongs, seen = [], [], set()
    for topic in data.topics:
        tid = topic["id"]
        topics.append({"topic_id": tid, "name": topic["name"],
                       "summary": data.topic_summaries.get(tid, "")[:_SUMMARY_LIMIT],
                       "repo": data.repo})
        for rel in topic_file_relpaths(topic):
            fid = file_id.get(rel)
            if fid and (fid, tid) not in seen:
                seen.add((fid, tid))
                belongs.append({"edge_id": _edge_id("FileBelongsToTopic", fid, tid),
                                "file_id": fid, "topic_id": tid})
    return topics, belongs


def _replace_topics(db, repo: str, topics: list[dict], belongs: list[dict]) -> None:
    """R10: in one transaction, delete this repo's rows of the topic tables for
    the current ID_PREFIX, then insert the new ones."""
    params = {"repo": repo, "prefix": f"{config.ID_PREFIX}_"}
    param_types = {"repo": spanner.param_types.STRING, "prefix": spanner.param_types.STRING}

    def work(txn) -> None:
        txn.execute_update(
            "DELETE FROM FileBelongsToTopic WHERE STARTS_WITH(edge_id, @prefix) AND "
            "(file_id IN (SELECT file_id FROM Files WHERE repo = @repo) OR "
            "topic_id IN (SELECT topic_id FROM Topics WHERE repo = @repo))",
            params=params, param_types=param_types)
        txn.execute_update(
            "DELETE FROM Topics WHERE repo = @repo AND STARTS_WITH(topic_id, @prefix)",
            params=params, param_types=param_types)
        for table, table_rows in (("Topics", topics), ("FileBelongsToTopic", belongs)):
            if table_rows:
                columns = _columns(table)
                txn.insert(table=table, columns=columns, values=[_row(columns, r) for r in table_rows])

    db.run_in_transaction(work)


def _embedding_items(data: PipelineData, rows: dict[str, list[dict]],
                     updates: dict[str, list[tuple[str, str]]],
                     topics: list[dict]) -> dict[str, list[tuple[str, str]]]:
    """(ID, text) for Files, Packages, Classes, Methods and Topics (spec §7.5).
    Closures, <init> and the <package> pseudo-type are not embedded."""
    labels: dict[str, dict[str, str]] = {
        "Files": {r.get("file_id"): r.get("path", "") for r in rows.get("Files", [])},
        "Packages": {r.get("package_id"): r.get("import_path", "") for r in rows.get("Packages", [])},
        "Classes": {r.get("class_id"): f"{r.get('fqcn') or r.get('qualname')} ({r.get('kind')})"
                    for r in rows.get("Classes", []) if r.get("kind") != "package"},
        "Methods": {r.get("method_id"): r.get("fqmn") or r.get("qualname", "")
                    for r in rows.get("Methods", [])
                    if r.get("member_kind") not in ("closure", "var_init")},
    }
    items = {table: [(nid, _truncate(f"{labels[table][nid]}: {summary}", 2000))
                     for nid, summary in updates.get(table, []) if nid in labels[table]]
             for table in labels}
    items["Topics"] = [(t["topic_id"], _truncate(f"{t['name']}: {t['summary']}", 2000))
                       for t in topics if t["summary"]]
    return items


_VECTOR_BATCH = 500  # rows per Spanner commit when cached vectors are written


class _EmbeddingCache:
    """Vectors by (sha256(text), EMBED_MODEL, EMBED_DIMENSION), in one SQLite
    file under OUTPUT_DIR/repos/<repo>/embeddings/. Vectors are kept as
    float64, so a cache hit writes exactly what the API returned."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vec BLOB)")

    @staticmethod
    def key(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()
        return f"{digest}|{config.EMBED_MODEL}|{config.EMBED_DIMENSION or 'default'}"

    def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        unique = sorted(set(keys))
        for i in range(0, len(unique), 500):
            chunk = unique[i:i + 500]
            query = f"SELECT key, vec FROM vectors WHERE key IN ({','.join('?' * len(chunk))})"
            for key, blob in self._db.execute(query, chunk):
                found[key] = array("d", blob).tolist()
        return found

    def put_many(self, pairs: list[tuple[str, list[float]]]) -> None:
        self._db.executemany("INSERT OR REPLACE INTO vectors (key, vec) VALUES (?, ?)",
                             [(key, array("d", vec).tobytes()) for key, vec in pairs])
        self._db.commit()

    def close(self) -> None:
        self._db.close()


def _write_embeddings(data: PipelineData, db, items_by_table: dict[str, list[tuple[str, str]]]
                      ) -> dict[str, tuple[int, int]]:
    """Write every item's vector to its table's embedding column. A cached
    vector costs no API call; only new texts are embedded (and cached).
    Returns (embedded, cached) per table."""
    cache = _EmbeddingCache(os.path.join(_out_root(), "embeddings", "cache.sqlite"))
    model = None
    report: dict[str, tuple[int, int]] = {}
    try:
        for table, items in items_by_table.items():
            if not items:
                continue
            id_col = _ID_COLUMNS[table]
            keys = {nid: _EmbeddingCache.key(text) for nid, text in items}
            hits = cache.get_many(list(keys.values()))
            cached = [[nid, hits[keys[nid]]] for nid, _text in items if keys[nid] in hits]
            if cached:
                _batch_update(db, table, [id_col, "embedding"], cached, batch_size=_VECTOR_BATCH)
            missing = [(nid, text) for nid, text in items if keys[nid] not in hits]
            embedded = 0
            if missing:
                model = model or _embedding_model()
                vectors: dict[str, list[float]] = {}
                embedded = _embed_and_write(model, db, missing, table, id_col, table.lower(),
                                            vectors=vectors)
                cache.put_many([(keys[nid], vec) for nid, vec in vectors.items()])
            report[table] = (embedded, len(cached))
    finally:
        cache.close()
    return report


def phase10_write_back(data: PipelineData) -> None:
    """Phase 10 (spec §7.5, R10). The Topics and FileBelongsToTopic rows go to
    graph_rows/ (R20). With Spanner, batched updates set the summary columns of
    Files, Packages, Classes, Methods and Directories, the repo's topic rows
    are replaced (deleted for the current ID_PREFIX, then inserted), and Files,
    Packages, Classes, Methods (closures excluded) and Topics are embedded.
    With --no-spanner nothing is written to Spanner."""
    t0 = time.perf_counter()
    rows = _graph_rows(data)
    topics, belongs = topics_phase.wiki_topic_rows(data, rows)  # LEIDEN-HOOK
    _write_jsonl("Topics", topics)
    _write_jsonl("FileBelongsToTopic", belongs)
    if data.no_spanner:
        data.timings["phase10_write_back"] = time.perf_counter() - t0
        _print(f"[Phase 10] --no-spanner: {len(topics)} topics and {len(belongs)} topic links "
               "in graph_rows/, summaries in summaries/; nothing written to Spanner")
        return
    db = _get_spanner_db()
    updates = _summary_updates(data, rows)
    for table, pairs in updates.items():
        if pairs:
            _batch_update(db, table, [_ID_COLUMNS[table], "summary"], [list(p) for p in pairs])
    topics_phase.replace_topic_tables(db, data, "Phase 10")  # LEIDEN-HOOK: batched, not atomic
    report = _write_embeddings(data, db, _embedding_items(data, rows, updates, topics))
    elapsed = time.perf_counter() - t0
    data.timings["phase10_write_back"] = elapsed
    _print(f"[Phase 10] Updated summaries ({_counts_text({t: len(p) for t, p in updates.items()})}), "
           f"replaced {len(topics)} topics and {len(belongs)} topic links in {_fmt_elapsed(elapsed)}")
    _print(f"[Phase 10] Embeddings: {sum(e for e, _ in report.values())} embedded, "
           f"{sum(c for _, c in report.values())} cached ("
           + (", ".join(f"{t} {e}+{c}" for t, (e, c) in report.items()) or "none") + ")")


# ===================================================================
# Wiki track
# ===================================================================


async def run_wiki_track(data: PipelineData, client: genai.Client | None = None) -> None:
    """Phases 2 → 3 → 4 → 5 → 6 → 10 (spec §2.1) on data prepare_wiki accepted."""
    client = client or _genai_client()
    _print(_estimate_text(estimate_wiki_calls(data)))
    await phase2_node_summaries(data, client)
    await phase3_package_dir_summaries(data, client)
    await topics_phase.phase4b_naming(data, client)  # LEIDEN-HOOK: 4b replaces Phase 4
    await topics_phase.phase5_reports(data, client)  # LEIDEN-HOOK: Phase 5 from structural evidence
    phase6_index(data)
    phase10_write_back(data)


# The leiden-only phases (Phase 4a, topic tables, 4b, 5 reports); imported last because
# topics_phase uses this module.
from . import topics_phase  # noqa: E402  LEIDEN-HOOK
