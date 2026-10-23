"""Pipeline orchestrator and phase functions using Google Gen AI SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from google.cloud import spanner

from google import genai
from google.genai import types

from . import config
from .prompts import (
    FILE_SUMMARY_PROMPT,
    FILE_CHUNK_SUMMARY_PROMPT,
    FILE_MERGE_SUMMARY_PROMPT,
    DIR_SUMMARY_PROMPT,
    TOPIC_EXTRACTION_PROMPT,
    TOPIC_SUMMARY_PROMPT,
    TOPIC_MERGE_PROMPT,
)


# ===================================================================
# Data model
# ===================================================================


@dataclass
class PipelineData:
    target_dir: str
    # Phase 1
    file_list: list[str] = field(default_factory=list)
    dir_tree: dict[str, Any] = field(default_factory=dict)
    dir_queue: list[str] = field(default_factory=list)

    # Phase 2
    file_summaries: dict[str, str] = field(default_factory=dict)

    # Phase 3
    dir_summaries: dict[str, str] = field(default_factory=dict)

    # Phase 4
    topics: list[dict] = field(default_factory=list)

    # Phase 5
    topic_summaries: dict[str, str] = field(default_factory=dict)

    # Phase 7
    extracted_entities: dict[str, Any] = field(default_factory=dict)

    # Phase 1.6 (Java LSP semantic resolution) — resolution.json contents
    resolution: dict | None = None

    # Phase 8-10 (graph ID maps)
    file_id_map: dict[str, str] = field(default_factory=dict)
    class_id_map: dict[str, str] = field(default_factory=dict)
    method_id_map: dict[str, str] = field(default_factory=dict)
    module_id_map: dict[str, str] = field(default_factory=dict)
    dir_id_map: dict[str, str] = field(default_factory=dict)

    # Timing
    timings: dict[str, float] = field(default_factory=dict)


# ===================================================================
# Helpers
# ===================================================================

# Shared generation config
_GEN_CONFIG = types.GenerateContentConfig(
    temperature=0.2,
    max_output_tokens=config.MAX_OUTPUT_TOKENS,
)


def _print(msg: str):
    print(msg, flush=True)


def _fmt_elapsed(seconds: float) -> str:
    """Format seconds into human readable elapsed time."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def _log_error(msg: str):
    """Append an error line to the error log file."""
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    os.makedirs(out_root, exist_ok=True)
    log_path = os.path.join(out_root, "error_log.txt")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


_CHUNK_CHARS = 800_000  # ~200K tokens normal code, ~267K for data-dense — safe under 1M


def _read_source_file(file_path: str) -> str:
    """Read the entire source file."""
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
                    self._circuit_reopen_at = time.time() + self._OPEN_PAUSE
                    _print(f"  [Throttle] Circuit breaker OPEN — {fail_rate:.0%} failure rate, pausing {self._OPEN_PAUSE:.0f}s")

    async def wait(self):
        if self._circuit_open:
            now = time.time()
            if now < self._circuit_reopen_at:
                wait = self._circuit_reopen_at - now
                _print(f"  [Throttle] Circuit open — waiting {wait:.0f}s before probe")
                await asyncio.sleep(wait)
            self._circuit_open = False
            _print("  [Throttle] Circuit breaker probing...")
        d = self._delay
        if d > 0:
            await asyncio.sleep(d + random.uniform(0, 0.2))

    @property
    def current_delay(self) -> float:
        return self._delay


# Global throttle instance — shared across all phases
_throttle = _AdaptiveThrottle()


def _load_summaries_from_disk(data: PipelineData):
    """Load existing file/dir/topic summaries from the output directory.

    This allows 'generate graph' to work standalone by picking up
    summaries written by a previous 'generate wiki' run.
    """
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    files_dir = os.path.join(out_root, "summaries", "files")
    dirs_dir = os.path.join(out_root, "summaries", "dirs")
    topics_dir = os.path.join(out_root, "topics")

    # Build reverse map: doc filename → source path
    rel_to_fp = {}
    for fp in data.file_list:
        rel = os.path.relpath(fp, data.target_dir)
        doc_name = rel.replace(os.sep, "_") + ".md"
        rel_to_fp[doc_name] = fp

    if os.path.isdir(files_dir):
        for fname in os.listdir(files_dir):
            fp = rel_to_fp.get(fname)
            if fp and fp not in data.file_summaries:
                path = os.path.join(files_dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if content.strip():  # skip blank files so they get regenerated
                    data.file_summaries[fp] = content

    if os.path.isdir(dirs_dir):
        for fname in os.listdir(dirs_dir):
            path = os.path.join(dirs_dir, fname)
            with open(path, "r", encoding="utf-8") as f:
                # dir summaries keyed by dir path — reconstruct from filename
                content = f.read()
            # Store with filename as key for now; Phase 8 uses dir_summaries
            if content.strip():  # skip blank files so they get regenerated
                data.dir_summaries[fname] = content

    # Load topics from index
    index_path = os.path.join(out_root, "reasoning_index.json")
    if os.path.exists(index_path) and not data.topics:
        with open(index_path, "r", encoding="utf-8") as f:
            idx = json.load(f)
            data.topics = idx.get("topics", [])

    if os.path.isdir(topics_dir):
        for fname in os.listdir(topics_dir):
            if fname.endswith(".md"):
                tname = fname[:-3]
                path = os.path.join(topics_dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if content.strip():  # skip blank files so they get regenerated
                    data.topic_summaries[tname] = content

    loaded = len(data.file_summaries)
    _print(f"  Loaded {loaded} file summaries, {len(data.dir_summaries)} dir summaries, "
           f"{len(data.topic_summaries)} topic summaries from disk")


async def _generate(client: genai.Client, prompt: str, max_retries: int = 5) -> str:
    """Single async Gemini call with adaptive throttle + per-call backoff.

    An empty model response (e.g. MAX_TOKENS exhausted by thinking, or a safety
    block) is treated as a retryable failure and ultimately raised — so callers
    never silently accept/write blank output.
    """
    for attempt in range(max_retries):
        await _throttle.wait()
        try:
            response = await client.aio.models.generate_content(
                model=config.MODEL,
                contents=prompt,
                config=_GEN_CONFIG,
            )
            await _throttle.on_success()
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "429" in str(e) or "rate" in err_str or "quota" in err_str or "resource" in err_str
            if is_rate_limit:
                await _throttle.on_rate_limit()
            if attempt < max_retries - 1 and is_rate_limit:
                wait_time = min(1.0 + attempt * 0.5, 3.0) + random.uniform(0, 0.5)
                await asyncio.sleep(wait_time)
                continue
            raise

        try:
            text = response.text or ""
        except Exception:
            text = ""
        if text.strip():
            return text

        # Empty text: capture finish_reason, retry a few times (handles transient
        # blanks), then fail loudly so the caller logs it and queues a retry
        # instead of writing an empty file.
        finish_reason = None
        try:
            if response.candidates:
                finish_reason = response.candidates[0].finish_reason
        except Exception:
            pass
        if attempt < max_retries - 1:
            await asyncio.sleep(min(1.0 + attempt * 0.5, 3.0) + random.uniform(0, 0.5))
            continue
        raise RuntimeError(
            f"empty model response after {max_retries} attempts "
            f"(finish_reason={finish_reason}); consider raising MAX_OUTPUT_TOKENS"
        )


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    return text.strip()


def _extract_json_substring(text: str):
    for open_ch, close_ch in [("[", "]"), ("{", "}")]:
        start = text.find(open_ch)
        if start == -1:
            continue
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
    raise json.JSONDecodeError("No JSON array/object found", text, 0)


async def _parse_json_response(text: str, client: genai.Client | None = None):
    """Parse JSON from LLM response: strip fences -> bracket extract -> LLM repair."""
    cleaned = _strip_markdown_fences(text)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    try:
        return _extract_json_substring(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    if client is not None:
        repair_prompt = (
            "The following text was supposed to be valid JSON but has syntax errors. "
            "Return ONLY the corrected valid JSON, nothing else:\n\n" + cleaned[:50000]
        )
        try:
            repaired = await _generate(client, repair_prompt)
            return json.loads(_strip_markdown_fences(repaired))
        except Exception:
            pass

    raise json.JSONDecodeError("All JSON parse attempts failed", text, 0)


def _save_summaries(target_dir: str, file_summaries: dict, dir_summaries: dict | None = None):
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)

    if file_summaries:
        files_dir = os.path.join(out_root, "summaries", "files")
        os.makedirs(files_dir, exist_ok=True)
        for fp, s in file_summaries.items():
            rel = os.path.relpath(fp, target_dir)
            doc_name = rel.replace(os.sep, "_") + ".md"
            with open(os.path.join(files_dir, doc_name), "w", encoding="utf-8") as f:
                f.write(s)

    if dir_summaries:
        dirs_dir = os.path.join(out_root, "summaries", "dirs")
        os.makedirs(dirs_dir, exist_ok=True)
        for dp, s in dir_summaries.items():
            rel = os.path.relpath(dp, os.path.dirname(target_dir))
            doc_name = rel.replace(os.sep, "_") + ".md"
            with open(os.path.join(dirs_dir, doc_name), "w", encoding="utf-8") as f:
                f.write(s)


# ===================================================================
# Resume / Checkpoint
# ===================================================================


def _load_checkpoint(data: PipelineData):
    """Load existing output from a previous (possibly interrupted) run."""
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    abs_path = data.target_dir
    resumed = {"files": 0, "dirs": 0, "topics": 0, "topic_summaries": 0, "entities": 0}

    # -- File summaries --
    files_dir = os.path.join(out_root, "summaries", "files")
    if os.path.isdir(files_dir):
        expected_map: dict[str, str] = {}
        for fp in data.file_list:
            doc_name = os.path.relpath(fp, abs_path).replace(os.sep, "_") + ".md"
            expected_map[doc_name] = fp
        for fn in os.listdir(files_dir):
            if fn.endswith(".md") and fn in expected_map:
                fp = expected_map[fn]
                try:
                    with open(os.path.join(files_dir, fn), "r", encoding="utf-8") as f:
                        content = f.read()
                    if content.strip():  # skip blank files so they get regenerated
                        data.file_summaries[fp] = content
                        resumed["files"] += 1
                except Exception:
                    pass

    # -- Dir summaries --
    dirs_dir = os.path.join(out_root, "summaries", "dirs")
    if os.path.isdir(dirs_dir):
        expected_map = {}
        for dp in data.dir_queue:
            doc_name = os.path.relpath(dp, os.path.dirname(abs_path)).replace(os.sep, "_") + ".md"
            expected_map[doc_name] = dp
        for fn in os.listdir(dirs_dir):
            if fn.endswith(".md") and fn in expected_map:
                dp = expected_map[fn]
                try:
                    with open(os.path.join(dirs_dir, fn), "r", encoding="utf-8") as f:
                        content = f.read()
                    if content.strip():  # skip blank files so they get regenerated
                        data.dir_summaries[dp] = content
                        resumed["dirs"] += 1
                except Exception:
                    pass

    # -- Topics --
    topics_dir = os.path.join(out_root, "topics")
    topic_tree_path = os.path.join(topics_dir, "topic_tree.json")
    if os.path.isfile(topic_tree_path):
        try:
            with open(topic_tree_path, "r", encoding="utf-8") as f:
                loaded_topics = json.load(f)
            # Tolerate a stale/malformed topic_tree.json (e.g. an empty object)
            # — only accept a list so downstream phases don't choke.
            data.topics = loaded_topics if isinstance(loaded_topics, list) else []
            resumed["topics"] = len(data.topics)
        except Exception:
            pass

    # -- Topic summaries --
    if data.topics and os.path.isdir(topics_dir):
        for t in data.topics:
            tname = t.get("name", "")
            safe_name = tname.lower().replace(" ", "_")
            safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
            md_path = os.path.join(topics_dir, f"{safe_name}.md")
            if os.path.isfile(md_path):
                try:
                    with open(md_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    if content.strip():  # skip blank files so they get regenerated
                        data.topic_summaries[tname] = content
                        resumed["topic_summaries"] += 1
                except Exception:
                    pass

    # -- Entities --
    entities_path = os.path.join(out_root, "entities.json")
    if os.path.isfile(entities_path):
        try:
            with open(entities_path, "r", encoding="utf-8") as f:
                data.extracted_entities = json.load(f)
            resumed["entities"] = len(data.extracted_entities)
        except Exception:
            pass

    if any(v > 0 for v in resumed.values()):
        _print(f"[Resume] Loaded checkpoint: {resumed['files']} file summaries, "
               f"{resumed['dirs']} dir summaries, {resumed['topics']} topics, "
               f"{resumed['topic_summaries']} topic summaries, {resumed['entities']} entities")


def _save_entities(data: PipelineData):
    """Save extracted entities to disk for resume support."""
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    os.makedirs(out_root, exist_ok=True)
    entities_path = os.path.join(out_root, "entities.json")
    with open(entities_path, "w", encoding="utf-8") as f:
        json.dump(data.extracted_entities, f, ensure_ascii=False)
    _print(f"  [Checkpoint] Saved {len(data.extracted_entities)} entities to {entities_path}")


def _save_graph_checkpoint(data: PipelineData, phase: str):
    """Save graph ID maps after each phase for resume support."""
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    os.makedirs(out_root, exist_ok=True)
    cp_path = os.path.join(out_root, "graph_checkpoint.json")
    cp = {
        "completed_phase": phase,
        "file_id_map": data.file_id_map,
        "class_id_map": data.class_id_map,
        "method_id_map": data.method_id_map,
        "module_id_map": data.module_id_map,
        "dir_id_map": data.dir_id_map,
    }
    with open(cp_path, "w", encoding="utf-8") as f:
        json.dump(cp, f)
    total_ids = (len(data.file_id_map) + len(data.class_id_map) +
                 len(data.method_id_map) + len(data.module_id_map) + len(data.dir_id_map))
    _print(f"  [Checkpoint] Saved graph state after {phase} ({total_ids} IDs) to {cp_path}")


def _load_graph_checkpoint(data: PipelineData) -> str | None:
    """Load graph checkpoint. Returns the last completed phase or None."""
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    cp_path = os.path.join(out_root, "graph_checkpoint.json")
    if not os.path.isfile(cp_path):
        return None
    try:
        with open(cp_path, "r", encoding="utf-8") as f:
            cp = json.load(f)
        data.file_id_map = cp.get("file_id_map", {})
        data.class_id_map = cp.get("class_id_map", {})
        data.method_id_map = cp.get("method_id_map", {})
        data.module_id_map = cp.get("module_id_map", {})
        data.dir_id_map = cp.get("dir_id_map", {})
        phase = cp.get("completed_phase", "")
        total_ids = (len(data.file_id_map) + len(data.class_id_map) +
                     len(data.method_id_map) + len(data.module_id_map) + len(data.dir_id_map))
        _print(f"[Resume] Loaded graph checkpoint: {phase} completed ({total_ids} IDs)")
        return phase
    except Exception:
        return None


# ===================================================================
# Phase 1: Scanner
# ===================================================================


def phase1_scan(data: PipelineData):
    t0 = time.time()
    abs_path = os.path.abspath(data.target_dir)
    data.target_dir = abs_path

    source_files = []
    for root, dirs, files in os.walk(abs_path):
        dirs[:] = [d for d in dirs if d not in config.SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext in config.SOURCE_EXTENSIONS:
                source_files.append(os.path.join(root, f))

    # Build directory tree
    dir_children = defaultdict(lambda: {"files": [], "subdirs": set()})
    all_dirs = {abs_path}

    for fp in source_files:
        parent = os.path.dirname(fp)
        dir_children[parent]["files"].append(fp)
        all_dirs.add(parent)
        current = parent
        while current != abs_path and current != os.path.dirname(current):
            parent_of_current = os.path.dirname(current)
            dir_children[parent_of_current]["subdirs"].add(current)
            all_dirs.add(parent_of_current)
            current = parent_of_current

    dir_tree = {}
    for d in all_dirs:
        dir_tree[d] = {
            "files": dir_children[d]["files"],
            "subdirs": sorted(dir_children[d]["subdirs"]),
        }

    # Bottom-up order
    dir_queue: list[str] = []
    processed: set[str] = set()

    def visit(d):
        if d in processed:
            return
        for sub in dir_tree.get(d, {}).get("subdirs", []):
            visit(sub)
        dir_queue.append(d)
        processed.add(d)

    for d in sorted(all_dirs):
        visit(d)

    data.file_list = source_files
    data.dir_tree = dir_tree
    data.dir_queue = dir_queue

    # --- RESUME: load existing output from previous runs ---
    _load_checkpoint(data)

    elapsed = time.time() - t0
    data.timings["phase1_scan"] = elapsed
    _print(f"[Phase 1] Scanned {len(source_files)} files, {len(dir_queue)} dirs in {elapsed:.1f}s")


# ===================================================================
# Phase 1.5: Tree-sitter Entity Extraction (local, instant)
# ===================================================================


def phase1b_treesitter_entities(data: PipelineData):
    """Extract entities from all files using tree-sitter AST parsing.

    Runs locally with zero API calls. Processes 37k files in ~2 minutes.
    Skips files that already have entities (resume support).
    """
    from .treesitter_parser import parse_entities

    t0 = time.time()
    total = len(data.file_list)
    pending = [fp for fp in data.file_list if fp not in data.extracted_entities]

    if not pending:
        _print(f"[Phase 1.5] All {total} entities already extracted (resumed), skipping")
        data.timings["phase1b_treesitter"] = 0.0
        return

    _print(f"[Phase 1.5] Tree-sitter parsing {len(pending)}/{total} files")

    parsed = 0
    skipped = 0
    for i, fp in enumerate(pending):
        try:
            content = _read_source_file(fp)
            if not content.strip():
                skipped += 1
                continue
            entities = parse_entities(fp, content)
            if entities:
                data.extracted_entities[fp] = entities
                parsed += 1
            else:
                skipped += 1  # unsupported language
        except Exception:
            skipped += 1

        if (i + 1) % 5000 == 0:
            elapsed = time.time() - t0
            print(f"\r  [Phase 1.5] {i+1}/{len(pending)} {_fmt_elapsed(elapsed)}          ", end="", flush=True)

    # Save checkpoint
    _save_entities(data)

    print()
    elapsed = time.time() - t0
    data.timings["phase1b_treesitter"] = elapsed
    _print(f"[Phase 1.5] Parsed {parsed} files, {skipped} skipped in {_fmt_elapsed(elapsed)}")

    from .treesitter_parser import _report_missing_langs
    _report_missing_langs()


# ===================================================================
# Phase 2: File Summaries (concurrent, summary-only)
# ===================================================================


async def phase2_file_summaries(data: PipelineData, client: genai.Client):
    """Generate file summaries via Gemini LLM (summary-only, entities handled by tree-sitter).

    - Saves each file summary to disk immediately (no memory buildup)
    - Processes in batches of BATCH_SIZE to limit concurrent memory
    - Retries failed files up to MAX_PHASE_RETRIES rounds with cooldown
    """
    BATCH_SIZE = 500
    MAX_PHASE_RETRIES = 3
    RETRY_COOLDOWNS = [30, 90, 180]  # escalating cooldowns between retry rounds

    t0 = time.time()
    _throttle.reset()
    total = len(data.file_list)

    pending = [fp for fp in data.file_list if fp not in data.file_summaries]

    if not pending:
        _print(f"[Phase 2] All {total} files already summarized (resumed), skipping")
        data.timings["phase2_file_summaries"] = 0.0
        return

    _print(f"[Phase 2] Summarizing {len(pending)}/{total} files (concurrency={config.GEMINI_CONCURRENCY}, batch={BATCH_SIZE})")

    sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)
    phase_start = time.time()

    # Progress tracking — separate counters for main pass and retries
    progress_done = 0
    progress_total = len(pending)
    progress_lock = asyncio.Lock()

    # Prepare output dirs once
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    files_dir = os.path.join(out_root, "summaries", "files")
    os.makedirs(files_dir, exist_ok=True)

    # Track failed files for retry
    failed_files: list[str] = []
    failed_lock = asyncio.Lock()

    def _save_one_summary(fp: str, summary: str):
        """Save a single file summary to disk immediately."""
        rel = os.path.relpath(fp, data.target_dir)
        doc_name = rel.replace(os.sep, "_") + ".md"
        with open(os.path.join(files_dir, doc_name), "w", encoding="utf-8") as f:
            f.write(summary)

    async def _chunked_summarize(fp: str, content: str) -> str | None:
        """Map-reduce summarization for files too large for a single call.

        Uses tree-sitter to split at structural boundaries (class/method/field)
        so each chunk is a complete code unit. Falls back to character splitting
        for unsupported languages. Adaptively re-splits chunks that still fail.
        """
        from .treesitter_parser import chunk_by_structure

        chunks = chunk_by_structure(fp, content)
        if chunks is None:
            chunks = _chunk_content(content)  # fallback for unsupported languages
        fname = os.path.basename(fp)
        total_lines = content.count("\n") + 1

        chunk_summaries = []
        for i, chunk in enumerate(chunks):
            cp = FILE_CHUNK_SUMMARY_PROMPT.format(
                file_name=fname, chunk_index=i + 1,
                total_chunks=len(chunks), content=chunk)
            try:
                cs = await _generate(client, cp)
                chunk_summaries.append(f"### チャンク {i+1}/{len(chunks)}\n{cs}")
            except Exception as e:
                if "400" in str(e) and "INVALID_ARGUMENT" in str(e):
                    # Chunk still too large — try tree-sitter sub-split, fallback to chars
                    sub_chunks = chunk_by_structure(fp, chunk, max_chars=len(chunk) // 2)
                    if sub_chunks is None:
                        sub_chunks = _chunk_content(chunk, chunk_size=len(chunk) // 3 + 1)
                    sub_parts = []
                    for j, sc in enumerate(sub_chunks):
                        scp = FILE_CHUNK_SUMMARY_PROMPT.format(
                            file_name=fname, chunk_index=f"{i+1}.{j+1}",
                            total_chunks=f"{len(chunks)}(sub)", content=sc)
                        sub_parts.append(await _generate(client, scp))
                    chunk_summaries.append(f"### チャンク {i+1}/{len(chunks)}\n" + "\n".join(sub_parts))
                else:
                    raise

        merged_text = "\n\n".join(chunk_summaries)
        merge_prompt = FILE_MERGE_SUMMARY_PROMPT.format(
            file_name=fname, total_chunks=len(chunks),
            total_lines=total_lines, chunk_summaries=merged_text)
        return await _generate(client, merge_prompt)

    async def summarize_one(fp: str):
        """Summarize one file via LLM and save to disk immediately.

        Tries full file first. Falls back to map-reduce chunking on 400 errors.
        """
        nonlocal progress_done
        async with sem:
            content = _read_source_file(fp)
            if not content.strip():
                async with progress_lock:
                    progress_done += 1
                return

            fname = os.path.basename(fp)
            summary = None

            try:
                # Try full file — Gemini 3 Flash supports up to 50MB input
                prompt = FILE_SUMMARY_PROMPT.format(file_name=fname, content=content)
                summary = await _generate(client, prompt)
            except Exception as e:
                err_str = str(e)
                if "400" in err_str and "INVALID_ARGUMENT" in err_str:
                    # File too large for single call — fall back to map-reduce
                    try:
                        summary = await _chunked_summarize(fp, content)
                    except Exception as e2:
                        rel_path = os.path.relpath(fp, data.target_dir)
                        _log_error(f"[Phase 2] chunked summary also failed: {rel_path}: {e2}")
                        async with failed_lock:
                            failed_files.append(fp)
                else:
                    rel_path = os.path.relpath(fp, data.target_dir)
                    _log_error(f"[Phase 2] summary failed: {rel_path}: {e}")
                    async with failed_lock:
                        failed_files.append(fp)

            if summary:
                data.file_summaries[fp] = summary
                _save_one_summary(fp, summary)

            async with progress_lock:
                progress_done += 1
                elapsed = time.time() - phase_start
                bar_len = 30
                filled = int(bar_len * progress_done / progress_total)
                bar = "█" * filled + "░" * (bar_len - filled)
                pct = progress_done / progress_total * 100
                rel = os.path.relpath(fp, data.target_dir)
                if len(rel) > 55:
                    rel = "..." + rel[-52:]
                throttle_info = f" t={_throttle.current_delay:.1f}s" if _throttle.current_delay > 0 else ""
                print(f"\r  {bar} {pct:5.1f}% ({progress_done}/{progress_total}) {_fmt_elapsed(elapsed)}{throttle_info} | {rel}          ", end="", flush=True)

    # Process in batches to limit memory
    for batch_start in range(0, len(pending), BATCH_SIZE):
        batch = pending[batch_start : batch_start + BATCH_SIZE]
        await asyncio.gather(*[summarize_one(fp) for fp in batch])

    # Retry failed files with cooldown between rounds
    for retry_round in range(1, MAX_PHASE_RETRIES + 1):
        if not failed_files:
            break
        retry_batch = failed_files.copy()
        failed_files.clear()
        cooldown = RETRY_COOLDOWNS[min(retry_round - 1, len(RETRY_COOLDOWNS) - 1)]
        print()
        _print(f"  [Phase 2] Waiting {cooldown}s for rate limit recovery...")
        await asyncio.sleep(cooldown)
        # Reset progress for retry round
        progress_done = 0
        progress_total = len(retry_batch)
        _print(f"  [Phase 2] Retry round {retry_round}: {len(retry_batch)} files")
        for batch_start in range(0, len(retry_batch), BATCH_SIZE):
            batch = retry_batch[batch_start : batch_start + BATCH_SIZE]
            await asyncio.gather(*[summarize_one(fp) for fp in batch])

    remaining_failures = len(failed_files)

    print()  # newline after progress bar
    elapsed = time.time() - t0
    data.timings["phase2_file_summaries"] = elapsed
    msg = f"[Phase 2] Completed: {len(data.file_summaries)} summaries in {_fmt_elapsed(elapsed)}"
    if remaining_failures:
        msg += f" ({remaining_failures} failed — see error_log.txt)"
    _print(msg)


# ===================================================================
# Phase 3: Dir Summaries (parallel by depth level, bottom-up)
# ===================================================================


async def phase3_dir_summaries(data: PipelineData, client: genai.Client):
    MAX_PHASE_RETRIES = 3
    RETRY_COOLDOWNS = [30, 90, 180]
    t0 = time.time()
    _throttle.reset()
    total = len(data.dir_queue)
    skipped = len(data.dir_summaries)

    if skipped == total:
        _print(f"[Phase 3] All {total} dirs already summarized (resumed), skipping")
        data.timings["phase3_dir_summaries"] = 0.0
        return

    if skipped > 0:
        _print(f"[Phase 3] Resuming: {skipped}/{total} dirs already done")

    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    dirs_out = os.path.join(out_root, "summaries", "dirs")
    os.makedirs(dirs_out, exist_ok=True)
    done = skipped
    failed_dirs: list[str] = []
    failed_lock = asyncio.Lock()
    progress_lock = asyncio.Lock()
    sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)

    async def _summarize_dir(dp: str) -> None:
        nonlocal done
        if dp in data.dir_summaries:
            return

        async with sem:
            node = data.dir_tree.get(dp, {"files": [], "subdirs": []})

            child_file_parts = []
            for fp in node["files"]:
                s = data.file_summaries.get(fp, "")
                if s:
                    child_file_parts.append(f"**{os.path.basename(fp)}**: {_truncate(s, 1500)}")

            child_dir_parts = []
            for sdp in node["subdirs"]:
                s = data.dir_summaries.get(sdp, "")
                if s:
                    child_dir_parts.append(f"**{os.path.basename(sdp)}**: {_truncate(s, 1500)}")

            if not child_file_parts and not child_dir_parts:
                async with progress_lock:
                    done += 1
                return

            prompt = DIR_SUMMARY_PROMPT.format(
                dir_name=os.path.relpath(dp, os.path.dirname(data.target_dir)),
                child_file_summaries="\n\n".join(child_file_parts) or "(なし)",
                child_dir_summaries="\n\n".join(child_dir_parts) or "(なし)",
            )

            try:
                summary = await _generate(client, prompt)
                data.dir_summaries[dp] = summary
                rel = os.path.relpath(dp, os.path.dirname(data.target_dir))
                doc_name = rel.replace(os.sep, "_") + ".md"
                with open(os.path.join(dirs_out, doc_name), "w", encoding="utf-8") as f:
                    f.write(summary)
            except Exception as e:
                _log_error(f"[Phase 3] dir summary failed: {os.path.basename(dp)}: {e}")
                async with failed_lock:
                    failed_dirs.append(dp)

            async with progress_lock:
                done += 1
                elapsed = time.time() - t0
                pct = done / total * 100
                print(f"\r  [Phase 3] {pct:5.1f}% ({done}/{total}) {_fmt_elapsed(elapsed)} | {os.path.basename(dp)}          ", end="", flush=True)

    # Group dirs by depth level — same depth can run in parallel
    depth_levels: list[list[str]] = []
    dir_depth: dict[str, int] = {}
    for dp in data.dir_queue:
        rel = os.path.relpath(dp, data.target_dir)
        depth = rel.count(os.sep)
        dir_depth[dp] = depth

    max_depth = max(dir_depth.values()) if dir_depth else 0
    for d in range(max_depth, -1, -1):  # bottom-up: deepest first
        level = [dp for dp in data.dir_queue if dir_depth.get(dp) == d]
        if level:
            depth_levels.append(level)

    _print(f"[Phase 3] Summarizing {total - skipped} dirs across {len(depth_levels)} depth levels (concurrency={config.GEMINI_CONCURRENCY})")

    for level_idx, level_dirs in enumerate(depth_levels):
        pending = [dp for dp in level_dirs if dp not in data.dir_summaries]
        if not pending:
            continue
        await asyncio.gather(*[_summarize_dir(dp) for dp in pending])

    # Retry failed dirs with cooldown
    for retry_round in range(1, MAX_PHASE_RETRIES + 1):
        if not failed_dirs:
            break
        retry_batch = failed_dirs.copy()
        failed_dirs.clear()
        print()
        cooldown = RETRY_COOLDOWNS[min(retry_round - 1, len(RETRY_COOLDOWNS) - 1)]
        _print(f"  [Phase 3] Waiting {cooldown}s for rate limit recovery...")
        await asyncio.sleep(cooldown)
        _print(f"  [Phase 3] Retry round {retry_round}: {len(retry_batch)} dirs")
        await asyncio.gather(*[_summarize_dir(dp) for dp in retry_batch])

    print()
    elapsed = time.time() - t0
    data.timings["phase3_dir_summaries"] = elapsed
    msg = f"[Phase 3] Completed: {len(data.dir_summaries)}/{total} dirs in {_fmt_elapsed(elapsed)}"
    if failed_dirs:
        msg += f" ({len(failed_dirs)} failed — see error_log.txt)"
    _print(msg)


# ===================================================================
# Phase 4: Topic Extraction (concurrent fan-out + merge)
# ===================================================================


def _collect_subtree_files(
    dir_path: str,
    dir_tree: dict,
    file_summaries: dict[str, str],
    result: list[str],
):
    node = dir_tree.get(dir_path, {"files": [], "subdirs": []})
    for fp in node["files"]:
        s = file_summaries.get(fp, "")
        if s:
            result.append(f"**{os.path.basename(fp)}**: {s[:300]}")
    for sub in node["subdirs"]:
        _collect_subtree_files(sub, dir_tree, file_summaries, result)


def _coerce_topic_list(parsed: Any) -> list[dict]:
    """Normalize an LLM topic-JSON response into a list of topic dicts.

    Tolerates the model returning a bare list, a wrapper object such as
    {"topics": [...]} / {"modules": [...]}, or a single topic object. Returns
    [] for anything unusable — this prevents a malformed (non-array) response
    from silently wiping out the topic list.
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


def _dedup_topics(topics: list[dict], limit: int | None = None) -> list[dict]:
    """Dedupe topics by name (order-preserving); optionally cap to *limit*."""
    seen: set[str] = set()
    out: list[dict] = []
    for t in topics:
        name = t.get("name", "") if isinstance(t, dict) else ""
        if name and name not in seen:
            seen.add(name)
            out.append(t)
    return out[:limit] if limit else out


async def phase4_topics(data: PipelineData, client: genai.Client):
    t0 = time.time()
    _throttle.reset()

    if data.topics:
        _print(f"[Phase 4] {len(data.topics)} topics already extracted (resumed), skipping")
        data.timings["phase4_topics"] = 0.0
        return

    # Get top-level dirs
    root_node = data.dir_tree.get(data.target_dir, {"subdirs": []})
    top_dirs = root_node.get("subdirs", [data.target_dir])

    # Build per-dir summary texts
    chunks: list[tuple[str, str]] = []  # (chunk_dir, summaries_text)
    for td in top_dirs:
        subtree_summaries: list[str] = []
        _collect_subtree_files(td, data.dir_tree, data.file_summaries, subtree_summaries)
        if not subtree_summaries:
            continue
        summaries_text = "\n\n".join(subtree_summaries)
        if len(summaries_text) > 200000:
            summaries_text = summaries_text[:200000] + "\n\n... [TRUNCATED]"
        chunks.append((td, summaries_text))

    # If no subdirs, process root as single chunk
    if not chunks:
        all_summaries = [
            f"**{os.path.basename(fp)}**: {s[:300]}"
            for fp, s in data.file_summaries.items() if s
        ]
        chunks.append((data.target_dir, "\n\n".join(all_summaries)))

    _print(f"[Phase 4] Fan-out: {len(chunks)} topic extraction chunks")

    sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)

    async def extract_chunk(chunk_dir: str, summaries_text: str) -> list[dict]:
        async with sem:
            prompt = TOPIC_EXTRACTION_PROMPT.format(summaries=summaries_text)
            try:
                text = await _generate(client, prompt)
                return await _parse_json_response(text, client=client)
            except Exception as e:
                _log_error(f"[Phase 4] topic extraction failed: {os.path.basename(chunk_dir)}: {e}")
                return []

    chunk_results = await asyncio.gather(*[
        extract_chunk(cd, st) for cd, st in chunks
    ])

    # Flatten (tolerate chunks that returned a wrapper object instead of a list)
    all_chunk_topics: list[dict] = []
    for topics in chunk_results:
        all_chunk_topics.extend(_coerce_topic_list(topics))

    # Merge
    if not all_chunk_topics:
        data.topics = []
    elif len(all_chunk_topics) <= 15:
        data.topics = _dedup_topics(all_chunk_topics)
    else:
        topics_json = json.dumps(all_chunk_topics, ensure_ascii=False, indent=2)
        merge_prompt = TOPIC_MERGE_PROMPT.format(topics_json=topics_json[:100000])
        merged_topics: list[dict] = []
        try:
            text = await _generate(client, merge_prompt)
            merged_topics = _coerce_topic_list(await _parse_json_response(text, client=client))
        except Exception as e:
            _log_error(f"[Phase 4] topic merge failed: {e}")
        if not merged_topics:
            # Merge produced no usable list (e.g. the model emitted a non-array
            # object). Fall back to deterministic dedup so topics are never
            # silently lost — this is what zeroed out topics before.
            _log_error("[Phase 4] topic merge produced no usable list; "
                       "falling back to deduped chunk topics")
            merged_topics = _dedup_topics(all_chunk_topics, limit=15)
        data.topics = merged_topics

    # Final guard: downstream phases require data.topics to be a list.
    if not isinstance(data.topics, list):
        data.topics = _dedup_topics(all_chunk_topics, limit=15)

    # Save topics immediately for resume
    if data.topics:
        out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
        topics_dir = os.path.join(out_root, "topics")
        os.makedirs(topics_dir, exist_ok=True)
        with open(os.path.join(topics_dir, "topic_tree.json"), "w", encoding="utf-8") as f:
            json.dump(data.topics, f, indent=2, ensure_ascii=False)

    elapsed = time.time() - t0
    data.timings["phase4_topics"] = elapsed
    _print(f"[Phase 4] Extracted {len(data.topics)} topics in {elapsed:.1f}s")


# ===================================================================
# Phase 5: Topic Summaries (concurrent)
# ===================================================================


async def phase5_topic_summaries(data: PipelineData, client: genai.Client):
    MAX_PHASE_RETRIES = 3
    RETRY_COOLDOWNS = [30, 90, 180]
    t0 = time.time()
    _throttle.reset()

    if not data.topics:
        _print("[Phase 5] No topics to summarize")
        data.timings["phase5_topic_summaries"] = 0.0
        return

    pending = [t for t in data.topics if t.get("name", "") not in data.topic_summaries]
    if not pending:
        _print(f"[Phase 5] All {len(data.topics)} topic summaries already written (resumed), skipping")
        data.timings["phase5_topic_summaries"] = 0.0
        return

    _print(f"[Phase 5] Writing {len(pending)}/{len(data.topics)} topic summaries")

    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    topics_dir = os.path.join(out_root, "topics")
    os.makedirs(topics_dir, exist_ok=True)

    sem = asyncio.Semaphore(config.GEMINI_CONCURRENCY)
    p5_done = 0
    p5_total = len(pending)
    failed_topics: list[dict] = []
    failed_lock = asyncio.Lock()

    # Build lookup dict to avoid O(n*m) file search
    basename_to_path: dict[str, str] = {}
    for fp in data.file_list:
        basename_to_path.setdefault(os.path.basename(fp), fp)

    async def write_one_summary(topic: dict) -> None:
        nonlocal p5_done
        async with sem:
            tname = topic.get("name", "")
            linked = set(topic.get("linked_files", []))
            for sub in topic.get("subtopics", []):
                linked.update(sub.get("linked_files", []))

            file_contents_parts = []
            file_summary_parts = []
            for fname in sorted(linked):
                matched = basename_to_path.get(fname)
                if matched and os.path.isfile(matched):
                    try:
                        content = _read_source_file(matched)
                        file_contents_parts.append(f"### {fname}\n```\n{content[:15000]}\n```")
                    except Exception:
                        pass
                s = data.file_summaries.get(matched, "") if matched else ""
                if s:
                    file_summary_parts.append(f"**{fname}**: {_truncate(s, 500)}")

            template = TOPIC_SUMMARY_PROMPT
            prompt = template.format(
                topic_name=tname,
                file_contents="\n\n".join(file_contents_parts)[:200000],
                file_summaries="\n\n".join(file_summary_parts),
            )
            try:
                summary = await _generate(client, prompt)
                data.topic_summaries[tname] = summary
                safe_name = tname.lower().replace(" ", "_")
                safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
                with open(os.path.join(topics_dir, f"{safe_name}.md"), "w", encoding="utf-8") as f:
                    f.write(summary)
                p5_done += 1
                elapsed = time.time() - t0
                print(f"\r  [Phase 5] {p5_done}/{p5_total} {_fmt_elapsed(elapsed)} | {tname}          ", end="", flush=True)
            except Exception as e:
                p5_done += 1
                _log_error(f"[Phase 5] topic summary failed: {tname}: {e}")
                async with failed_lock:
                    failed_topics.append(topic)

    await asyncio.gather(*[write_one_summary(t) for t in pending])

    # Retry failed topics with cooldown
    for retry_round in range(1, MAX_PHASE_RETRIES + 1):
        if not failed_topics:
            break
        retry_batch = failed_topics.copy()
        failed_topics.clear()
        print()
        cooldown = RETRY_COOLDOWNS[min(retry_round - 1, len(RETRY_COOLDOWNS) - 1)]
        _print(f"  [Phase 5] Waiting {cooldown}s for rate limit recovery...")
        await asyncio.sleep(cooldown)
        _print(f"  [Phase 5] Retry round {retry_round}: {len(retry_batch)} topics")
        await asyncio.gather(*[write_one_summary(t) for t in retry_batch])

    print()
    elapsed = time.time() - t0
    data.timings["phase5_topic_summaries"] = elapsed
    msg = f"[Phase 5] Completed: {len(data.topic_summaries)}/{len(data.topics)} topics in {_fmt_elapsed(elapsed)}"
    if failed_topics:
        msg += f" ({len(failed_topics)} failed — see error_log.txt)"
    _print(msg)


# ===================================================================
# Phase 6: Index Assembly
# ===================================================================


def phase6_index(data: PipelineData):
    t0 = time.time()
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)

    # Save file summaries
    _save_summaries(data.target_dir, data.file_summaries, data.dir_summaries)

    # Save topics
    topics_dir = os.path.join(out_root, "topics")
    os.makedirs(topics_dir, exist_ok=True)
    with open(os.path.join(topics_dir, "topic_tree.json"), "w", encoding="utf-8") as f:
        json.dump(data.topics, f, indent=2, ensure_ascii=False)

    for t in data.topics:
        tname = t.get("name", "")
        summary = data.topic_summaries.get(tname, "")
        if summary:
            safe_name = tname.lower().replace(" ", "_")
            safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
            with open(os.path.join(topics_dir, f"{safe_name}.md"), "w", encoding="utf-8") as f:
                f.write(summary)

    # Build reasoning_index.json
    def build_node(path):
        node_info = data.dir_tree.get(path, {"files": [], "subdirs": []})
        children = []
        for fp in node_info["files"]:
            children.append({
                "path": os.path.relpath(fp, data.target_dir),
                "type": "file",
                "name": os.path.basename(fp),
                "summary": data.file_summaries.get(fp, "")[:500],
            })
        for dp in node_info["subdirs"]:
            children.append(build_node(dp))
        return {
            "path": os.path.relpath(path, os.path.dirname(data.target_dir)),
            "type": "directory",
            "name": os.path.basename(path),
            "summary": data.dir_summaries.get(path, "")[:500],
            "children": children,
        }

    summary_tree = build_node(data.target_dir)

    topic_tree = []
    for t in data.topics:
        entry = {
            "name": t.get("name", ""),
            "summary": data.topic_summaries.get(t.get("name", ""), "")[:500],
            "linked_files": t.get("linked_files", []),
            "subtopics": t.get("subtopics", []),
        }
        topic_tree.append(entry)

    index_data = {"summary_tree": summary_tree, "topic_tree": topic_tree}
    json_path = os.path.join(out_root, "reasoning_index.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2, ensure_ascii=False)

    # Build index.md
    repo_name = os.path.basename(data.target_dir)
    md_lines = [f"# {repo_name} ドキュメント\n"]
    md_lines.append("## はじめに\n")
    md_lines.append(f"{summary_tree.get('summary', '')}\n")
    md_lines.append("## モジュールドキュメント\n")
    for t in topic_tree:
        safe_name = t["name"].lower().replace(" ", "_")
        safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
        first_line = t.get("summary", "")[:150].split("\n")[0]
        md_lines.append(f"*   **[{t['name']}](topics/{safe_name}.md)**: {first_line}")
    md_lines.append("")

    # Component table
    md_lines.append("## コンポーネント一覧\n")
    md_lines.append("| ファイル | 概要 |")
    md_lines.append("|------|---------|")
    count = 0
    for fp, s in data.file_summaries.items():
        if count >= 200:
            md_lines.append(f"| ... | 他 {len(data.file_summaries) - 200} ファイル |")
            break
        name = os.path.relpath(fp, data.target_dir)
        first_line = s.split("\n")[0][:80] if s else ""
        md_lines.append(f"| {name} | {first_line} |")
        count += 1

    index_md_path = os.path.join(out_root, "index.md")
    with open(index_md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    # Metadata
    metadata = {
        "generation_info": {
            "timestamp": datetime.now().isoformat(),
            "model": config.MODEL,
            "generator": "pipeline_v2 (google-genai)",
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

    elapsed = time.time() - t0
    data.timings["phase6_index"] = elapsed
    _print(f"[Phase 6] Index assembled in {elapsed:.1f}s")


# ===================================================================
# Phase 1.6: Java LSP Semantic Resolution (Eclipse JDT LS)
# ===================================================================


def _call_blocking(fn, *args):
    """Call a blocking function that itself uses asyncio.run(); offload to a
    worker thread when an event loop is already running (run_pipeline path)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


def phase1c_semantic_resolution(data: PipelineData):
    """Phase 1.6: resolve Java call sites to exact symbols via jdtls.

    Runs at the top of the graph pipeline so every CLI path gets it. When the
    LSP is disabled or a project cannot be analyzed, affected files degrade to
    nodes-only (no call/inherit edges) and Japanese setup guidance is printed.
    """
    t0 = time.time()
    from . import resolution as resolution_mod

    java_files = [fp for fp in data.file_list if fp.endswith(".java")]
    if not java_files:
        data.resolution = None
        data.timings["phase1c_semantic_resolution"] = 0.0
        return

    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    prev = None
    prev_path = os.path.join(out_root, "resolution.json")
    if os.path.exists(prev_path):
        try:
            with open(prev_path, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = None

    if not config.JAVA_LSP_ENABLED:
        _print("[Phase 1.6] JAVA_LSP_ENABLED=false — セマンティック解決を行いません。")
        _print("            ノードは生成されますが、呼び出し/継承エッジは生成されません(誤エッジ防止)。")

    data.resolution = _call_blocking(
        resolution_mod.run_resolution, data.target_dir, java_files, out_root)

    res = data.resolution or {}
    fallback_slns = [s for s in res.get("solutions", []) if s.get("status") != "resolved"]
    fallback_file_count = (sum(len(s.get("files", [])) for s in fallback_slns)
                           + len(res.get("fallback_files", [])))
    if config.JAVA_LSP_ENABLED and fallback_file_count:
        _print(f"[Phase 1.6] 解析できなかった Java ファイル: {fallback_file_count} 件 — "
               "ノードのみ生成し、呼び出しエッジは生成しません。")
        _print(resolution_mod.GUIDANCE_JA)

    resolution_changed = prev is None or (
        prev.get("declarations") != res.get("declarations")
        or prev.get("sites") != res.get("sites"))
    if resolution_changed:
        ckpt = os.path.join(out_root, "graph_checkpoint.json")
        if os.path.exists(ckpt):
            os.remove(ckpt)
            _print("[Phase 1.6] 解決結果が変化したため graph_checkpoint.json を破棄し、"
                   "Phase 8-10 を再実行します。")

    n_resolved = sum(1 for s in res.get("sites", []) if s.get("status") == "resolved")
    _print(f"[Phase 1.6] 解決済みサイト {n_resolved}/{len(res.get('sites', []))}、"
           f"宣言 {len(res.get('declarations', []))} 件 "
           f"({time.time() - t0:.1f}s)")
    data.timings["phase1c_semantic_resolution"] = time.time() - t0


def _resolution_active(data: PipelineData) -> bool:
    return bool(data.resolution and (data.resolution.get("lsp") or {}).get("enabled"))


def _resolved_file_set(data: PipelineData) -> set[str]:
    """Absolute paths of .java files covered by an LSP-resolved project."""
    out: set[str] = set()
    if not data.resolution:
        return out
    for sln in data.resolution.get("solutions", []):
        if sln.get("status") == "resolved":
            for rf in sln.get("files", []):
                out.add(os.path.join(data.target_dir, rf))
    return out


def _simple_type_name(symbol: str) -> str:
    """`iryo.data.ChartArchive$Compactor` → `Compactor` (nested types use `$`)."""
    gi = symbol.find("<")
    head = symbol if gi == -1 else symbol[:gi]
    cut = max(head.rfind("."), head.rfind("$"))
    return symbol[cut + 1:] if cut != -1 else symbol


def _member_display_name(symbol: str, container: str) -> str:
    """`iryo.kernel.Logger.<init>(String)` (container `iryo.kernel.Logger`) → `<init>`."""
    tail = symbol
    if container and symbol.startswith(container):
        tail = symbol[len(container):]
    tail = tail.lstrip("./")
    for sep in ("(", " {"):
        i = tail.find(sep)
        if i != -1:
            tail = tail[:i]
    return tail.strip() or symbol


# ===================================================================
# Spanner Graph Helpers
# ===================================================================


def _make_id(*parts: str) -> str:
    """Deterministic ID: prefix + sha256 of joined parts, truncated to 16 hex chars."""
    raw = "|".join(parts)
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"{config.ID_PREFIX}_{h}"


def _get_spanner_db():
    """Return a Spanner database handle."""
    client = spanner.Client(project=config.GCP_PROJECT, disable_builtin_metrics=True)
    instance = client.instance(config.SPANNER_INSTANCE)
    return instance.database(config.SPANNER_DATABASE)


def _batch_insert(db, table: str, columns: list[str], rows: list[list], max_retries: int = 5):
    """Insert rows in batches via mutations with retry on transient errors."""
    from google.api_core.exceptions import Aborted, ServiceUnavailable, DeadlineExceeded

    for i in range(0, len(rows), config.SPANNER_BATCH_SIZE):
        batch = rows[i : i + config.SPANNER_BATCH_SIZE]
        for attempt in range(max_retries):
            try:
                with db.batch() as txn:
                    txn.insert_or_update(table=table, columns=columns, values=batch)
                break
            except (Aborted, ServiceUnavailable, DeadlineExceeded) as e:
                if attempt == max_retries - 1:
                    _log_error(f"[Spanner] {table} batch {i} failed after {max_retries} attempts: {e}")
                    raise
                time.sleep(2 ** attempt + random.uniform(0, 1))


# ===================================================================
# Phase 8: Write Graph Nodes
# ===================================================================


def phase8_write_nodes(data: PipelineData):
    t0 = time.time()

    if not data.file_list and not data.extracted_entities:
        _print("[Phase 8] No data to write, skipping")
        data.timings["phase8_write_nodes"] = 0.0
        return

    db = _get_spanner_db()

    # -- File nodes (from scan — deterministic, enriched with LLM summaries if available) --
    file_rows = []
    file_id_map: dict[str, str] = {}
    for fp in data.file_list:
        fid = _make_id("file", fp)
        file_id_map[fp] = fid
        ext = os.path.splitext(fp)[1]
        directory = os.path.dirname(os.path.relpath(fp, data.target_dir)) if data.target_dir else ""
        summary = data.file_summaries.get(fp, "")
        file_rows.append([fid, os.path.basename(fp), ext, directory, summary[:4000]])

    if file_rows:
        _batch_insert(db, "Files", ["file_id", "file_name", "extension", "directory", "summary"], file_rows)
        _print(f"  [Phase 8] Wrote {len(file_rows)} file nodes")

    # -- Class / Method nodes --
    # LSP mode: exact identity from Phase 1.6 declarations (artifact + fq symbol).
    # Files not covered by a resolved solution keep the legacy path-based identity.
    res_active = _resolution_active(data)
    resolved_files = _resolved_file_set(data) if res_active else set()

    id_owner: dict[str, tuple[str, str]] = {}

    def _claim_id(node_id: str, artifact: str, symbol: str):
        prev = id_owner.get(node_id)
        if prev is not None and prev != (artifact, symbol):
            raise RuntimeError(
                f"ノードID衝突を検出: {prev} と {(artifact, symbol)} が同じ ID {node_id} に写像されました")
        id_owner[node_id] = (artifact, symbol)

    class_rows = []
    class_id_map: dict[str, str] = {}
    method_rows = []
    method_id_map: dict[str, str] = {}

    # display-metadata lookup (kind/modifiers) from tree-sitter entities
    entity_kind: dict[tuple[str, str], tuple[str, str]] = {}
    for fp, ent in data.extracted_entities.items():
        for cls in ent.get("classes", []):
            if cls.get("name"):
                entity_kind[(fp, cls["name"])] = (cls.get("kind", "class"), cls.get("modifiers", ""))

    if res_active:
        decls = data.resolution.get("declarations", [])
        decl_by_key = {(d["artifact"], d["symbol"]): d for d in decls}

        def _type_id_for(artifact: str, container: str) -> str:
            # local functions sit under a member symbol — walk up to the type
            cur = container
            for _ in range(4):
                if not cur:
                    return ""
                d = decl_by_key.get((artifact, cur))
                if d is None:
                    return ""
                if d["kind"] == "type":
                    return class_id_map.get(f"{artifact}|{cur}", "")
                cur = d.get("container", "")
            return ""

        seen_class_keys: set[str] = set()
        seen_method_keys: set[str] = set()
        for d in decls:
            if d["kind"] != "type":
                continue
            key = f'{d["artifact"]}|{d["symbol"]}'
            cid = _make_id("class", d["artifact"], d["symbol"])
            _claim_id(cid, d["artifact"], d["symbol"])
            class_id_map[key] = cid
            if key in seen_class_keys:
                continue  # partial class: one node, first decl wins for display
            seen_class_keys.add(key)
            abs_fp = os.path.join(data.target_dir, d["file"])
            sname = _simple_type_name(d["symbol"])
            kind, mods = entity_kind.get((abs_fp, sname.split("<")[0]), ("class", ""))
            class_rows.append([
                cid, sname, file_id_map.get(abs_fp, ""), kind, mods,
                f'{d["symbol"]}: {kind}',
                d.get("namespace", ""), d["symbol"], d["artifact"], d["file"], d.get("line"),
            ])
        for d in decls:
            if d["kind"] == "type":
                continue
            key = f'{d["artifact"]}|{d["symbol"]}'
            mid = _make_id("method", d["artifact"], d["symbol"])
            _claim_id(mid, d["artifact"], d["symbol"])
            method_id_map[key] = mid
            if key in seen_method_keys:
                continue
            seen_method_keys.add(key)
            abs_fp = os.path.join(data.target_dir, d["file"])
            method_rows.append([
                mid, _member_display_name(d["symbol"], d.get("container", "")),
                _type_id_for(d["artifact"], d.get("container", "")),
                file_id_map.get(abs_fp, ""),
                d["symbol"], "", "", d["symbol"],
                d.get("namespace", ""), d["symbol"], d["artifact"], d["kind"],
                d["file"], d.get("line"),
            ])

    # legacy identity for files without LSP resolution (or LSP disabled)
    for fp, ent in data.extracted_entities.items():
        if res_active and fp in resolved_files:
            continue
        for cls in ent.get("classes", []):
            cname = cls.get("name", "")
            if not cname:
                continue
            cid = _make_id("class", fp, cname)
            class_id_map[f"{fp}|{cname}"] = cid
            class_rows.append([
                cid, cname, file_id_map.get(fp, ""),
                cls.get("kind", "class"), cls.get("modifiers", ""),
                f"{cname}: {cls.get('kind', 'class')}",
                None, None, None, None, None,
            ])
            for meth in cls.get("methods", []):
                mname = meth.get("name", "")
                if not mname:
                    continue
                mid = _make_id("method", fp, cname, mname)
                method_id_map[f"{fp}|{cname}|{mname}"] = mid
                sig = f"{meth.get('return_type', 'void')} {cname}.{mname}({meth.get('parameters', '')})"
                method_rows.append([
                    mid, mname, cid, file_id_map.get(fp, ""),
                    sig, meth.get("modifiers", ""),
                    meth.get("return_type", ""),
                    f"{cname}.{mname}",
                    None, None, None, None, None, None,
                ])

    if class_rows:
        _batch_insert(db, "Classes",
                      ["class_id", "name", "file_id", "kind", "modifiers", "summary",
                       "namespace", "fq_symbol", "artifact", "decl_file", "decl_line"],
                      class_rows)
        _print(f"  [Phase 8] Wrote {len(class_rows)} class nodes")

    if method_rows:
        _batch_insert(db, "Methods",
                      ["method_id", "name", "class_id", "file_id", "signature", "modifiers",
                       "return_type", "summary",
                       "namespace", "fq_symbol", "artifact", "member_kind", "decl_file", "decl_line"],
                      method_rows)
        _print(f"  [Phase 8] Wrote {len(method_rows)} method nodes")

    # -- Module nodes (from topics) --
    module_rows = []
    module_id_map: dict[str, str] = {}
    for topic in data.topics:
        tname = topic.get("name", "")
        if not tname:
            continue
        tid = _make_id("module", tname)
        module_id_map[tname] = tid
        summary = data.topic_summaries.get(tname, "")
        module_rows.append([tid, tname, summary[:4000]])

    if module_rows:
        _batch_insert(db, "Modules", ["module_id", "name", "summary"], module_rows)
        _print(f"  [Phase 8] Wrote {len(module_rows)} module nodes")

    # -- Directory nodes (from scan — deterministic, enriched with LLM summaries if available) --
    dir_rows = []
    dir_id_map: dict[str, str] = {}
    for dp in data.dir_queue:
        did = _make_id("dir", dp)
        dir_id_map[dp] = did
        summary = data.dir_summaries.get(dp, "")
        dir_rows.append([did, os.path.basename(dp), summary[:4000]])

    if dir_rows:
        _batch_insert(db, "Directories", ["dir_id", "name", "summary"], dir_rows)
        _print(f"  [Phase 8] Wrote {len(dir_rows)} directory nodes")

    # Persist ID maps
    data.file_id_map = file_id_map
    data.class_id_map = class_id_map
    data.method_id_map = method_id_map
    data.module_id_map = module_id_map
    data.dir_id_map = dir_id_map

    elapsed = time.time() - t0
    data.timings["phase8_write_nodes"] = elapsed
    _print(f"[Phase 8] Wrote {len(file_rows)} files, {len(class_rows)} classes, "
           f"{len(method_rows)} methods, {len(module_rows)} modules, {len(dir_rows)} dirs in {elapsed:.1f}s")


# ===================================================================
# Phase 9: Write Graph Edges
# ===================================================================


def phase9_write_edges(data: PipelineData):
    t0 = time.time()

    if not data.file_id_map:
        _print("[Phase 9] No nodes written, skipping edges")
        data.timings["phase9_write_edges"] = 0.0
        return

    db = _get_spanner_db()

    # Build lookups (skip ambiguous basenames to avoid wrong deps)
    basename_to_fp: dict[str, str] = {}
    _bn_counts: dict[str, int] = {}
    for fp in data.file_list:
        bn = os.path.splitext(os.path.basename(fp))[0]
        _bn_counts[bn] = _bn_counts.get(bn, 0) + 1
        basename_to_fp[bn] = fp
    for bn, cnt in _bn_counts.items():
        if cnt > 1:
            del basename_to_fp[bn]  # ambiguous — skip

    # Semantic mode: edges come only from Phase 1.6 resolved sites. The old
    # global first-wins name maps produced wrong edges for overloads and
    # same-named members across projects — they are gone. Without resolution,
    # call/inherit edges are suppressed entirely (nodes-only degradation).
    res_active = _resolution_active(data)
    resolved_files = _resolved_file_set(data) if res_active else set()

    counts: dict[str, int] = {}

    # -- FileDependsOn --
    dep_rows = []
    seen_deps: set[tuple[str, str]] = set()

    for fp, ent in data.extracted_entities.items():
        src_id = data.file_id_map.get(fp, "")
        if not src_id:
            continue
        for imp in ent.get("imports", []):
            imp_name = imp.rsplit(".", 1)[-1] if "." in imp else imp
            imp_name = imp_name.rsplit("/", 1)[-1] if "/" in imp_name else imp_name
            target_fp = basename_to_fp.get(imp_name)
            if target_fp and target_fp != fp:
                tgt_id = data.file_id_map.get(target_fp, "")
                if tgt_id and (src_id, tgt_id) not in seen_deps:
                    seen_deps.add((src_id, tgt_id))
                    eid = _make_id("dep", src_id, tgt_id)
                    dep_rows.append([eid, src_id, tgt_id])

    # Same-package implicit dependencies (uses extracted entity data, no disk I/O)
    package_files: dict[str, list[tuple[str, set[str], set[str]]]] = {}
    for fp, ent in data.extracted_entities.items():
        pkg = ent.get("namespace", "") or os.path.dirname(fp)
        defined_names = {c.get("name", "") for c in ent.get("classes", []) if c.get("name")}
        # Collect referenced names: base classes, interfaces, method call targets
        referenced_names: set[str] = set()
        for cls in ent.get("classes", []):
            referenced_names.update(cls.get("base_classes", []))
            referenced_names.update(cls.get("interfaces", []))
            for meth in cls.get("methods", []):
                for call in meth.get("calls", []):
                    # Extract class part from "Obj.Method" patterns
                    if "." in call:
                        referenced_names.add(call.split(".")[0])
        if pkg not in package_files:
            package_files[pkg] = []
        package_files[pkg].append((fp, defined_names, referenced_names))

    for pkg, pkg_members in package_files.items():
        if len(pkg_members) < 2:
            continue
        classname_to_file: dict[str, str] = {}
        for fp, defined, _ in pkg_members:
            for cn in defined:
                classname_to_file[cn] = fp
        for fp, own_classes, refs in pkg_members:
            src_id = data.file_id_map.get(fp, "")
            if not src_id:
                continue
            for ref_name in refs:
                defining_fp = classname_to_file.get(ref_name)
                if defining_fp and defining_fp != fp:
                    tgt_id = data.file_id_map.get(defining_fp, "")
                    if tgt_id and (src_id, tgt_id) not in seen_deps:
                        seen_deps.add((src_id, tgt_id))
                        eid = _make_id("dep", src_id, tgt_id)
                        dep_rows.append([eid, src_id, tgt_id])

    counts["FileDependsOn"] = len(dep_rows)

    # -- ClassInherits (LSP mode only; unique-name rule prevents wrong parents) --
    inherit_rows = []
    if res_active:
        decls = data.resolution.get("declarations", [])
        types_by_stripped: dict[str, list[dict]] = {}
        type_by_file_name: dict[tuple[str, str], dict] = {}
        file_name_symbols: dict[tuple[str, str], set] = {}
        for d in decls:
            if d["kind"] != "type":
                continue
            stripped = _simple_type_name(d["symbol"]).split("<")[0]
            types_by_stripped.setdefault(stripped, []).append(d)
            type_by_file_name.setdefault((d["file"], stripped), d)
            file_name_symbols.setdefault((d["file"], stripped), set()).add(
                (d["artifact"], d["symbol"]))
        for fp, ent in data.extracted_entities.items():
            if fp not in resolved_files:
                continue
            rel = os.path.relpath(fp, data.target_dir)
            for cls in ent.get("classes", []):
                cname = (cls.get("name") or "").split("<")[0]
                child = type_by_file_name.get((rel, cname))
                if child is None:
                    continue
                if len(file_name_symbols.get((rel, cname), set())) != 1:
                    continue  # 同一ファイル内の同名型(別 namespace)— 子の同定が曖昧なため生成しない
                child_id = data.class_id_map.get(f'{child["artifact"]}|{child["symbol"]}', "")
                if not child_id:
                    continue
                # 継承種別は構文どおりに決める(名前ヒューリスティックは使わない)。
                # tree-sitter エンティティの base_classes = extends 節(superclass)、
                # interfaces = implements 節(super_interfaces)。インタフェースの
                # extends 節はパーサが interfaces 側へ入れるため、子がインタフェース
                # のときだけ "extends" に戻す(interface extends interface)。
                iface_kind = "extends" if cls.get("kind") == "interface" else "implements"
                bases = ([(b, "extends") for b in cls.get("base_classes", [])]
                         + [(b, iface_kind) for b in cls.get("interfaces", [])])
                for base, kind in bases:
                    stripped = base.split("<")[0].rsplit(".", 1)[-1]
                    cands = types_by_stripped.get(stripped, [])
                    # one decl per file — count unique symbols
                    uniq = {(c["artifact"], c["symbol"]) for c in cands}
                    if len(uniq) != 1:
                        continue  # same-named types in several projects / external base — skip, never guess
                    parent = cands[0]
                    parent_id = data.class_id_map.get(f'{parent["artifact"]}|{parent["symbol"]}', "")
                    if parent_id and parent_id != child_id:
                        eid = _make_id("inherits", child_id, parent_id)
                        inherit_rows.append([eid, child_id, parent_id, kind])
    else:
        _print("  [Phase 9] セマンティック解決なし — ClassInherits/MethodCalls は生成しません(誤エッジ防止)")

    counts["ClassInherits"] = len(inherit_rows)

    # -- MethodCalls (LSP mode only; exact resolved targets) --
    call_rows = []
    if res_active:
        decl_keys: set[tuple[str, str]] = set()
        sym_artifacts: dict[str, set[str]] = {}
        for d in data.resolution.get("declarations", []):
            decl_keys.add((d["artifact"], d["symbol"]))
            sym_artifacts.setdefault(d["symbol"], set()).add(d["artifact"])
        ambiguous_syms = {(x.get("artifact"), x.get("symbol"))
                          for x in data.resolution.get("ambiguous_symbols", [])}
        seen_call_edges: set[str] = set()
        skipped_calls = 0
        for site in data.resolution.get("sites", []):
            if site.get("status") != "resolved":
                continue
            tgt = site.get("target") or {}
            enclosing = site.get("enclosing")
            if not enclosing or not tgt or tgt.get("kind") == "type":
                continue  # type-level targets (new X() to implicit ctor, DI type args)
            # 呼び出し元の帰属は resolution.json v2 の enclosing_artifact
            # (サイトのファイルが属する Maven artifact)を第一情報とする。
            # 同名シンボルが複数 artifact にあっても取り違えない。
            enc_artifact = site.get("enclosing_artifact")
            if enc_artifact and (enc_artifact, enclosing) in decl_keys:
                caller_artifact = enc_artifact
            else:
                # 防御的フォールバック(v1 形式や artifact 不明のサイト):
                # シンボルが全 artifact で一意なときだけ帰属させる
                arts = sym_artifacts.get(enclosing, set())
                if len(arts) != 1:
                    skipped_calls += 1
                    continue
                caller_artifact = next(iter(arts))
            if (caller_artifact, enclosing) in ambiguous_syms:
                skipped_calls += 1  # duplicate member symbol — attribution unsafe
                continue
            caller_id = data.method_id_map.get(f"{caller_artifact}|{enclosing}", "")
            edge_targets = [(tgt, site.get("resolved_via") or "lsp")]
            st = site.get("static_target")
            if st and st.get("symbol") and st.get("symbol") != tgt.get("symbol"):
                edge_targets.append((st, "lsp"))
            for t, via in edge_targets:
                if (t.get("artifact"), t.get("symbol")) in ambiguous_syms:
                    skipped_calls += 1
                    continue
                callee_id = data.method_id_map.get(f'{t.get("artifact")}|{t.get("symbol")}', "")
                if not caller_id or not callee_id:
                    skipped_calls += 1
                    continue
                eid = _make_id("calls", caller_id, callee_id)
                if eid in seen_call_edges:
                    continue
                seen_call_edges.add(eid)
                call_rows.append([
                    eid, caller_id, callee_id,
                    _member_display_name(t["symbol"], ""),
                    t["symbol"], site.get("site_kind", ""), via,
                    site.get("file", ""), site.get("line"),
                ])
        if skipped_calls:
            _print(f"  [Phase 9] 解決済みでもエッジ化しなかった呼び出し: {skipped_calls} 件"
                   "(リポジトリ外ターゲット・トップレベル文など)")

    counts["MethodCalls"] = len(call_rows)

    # -- FileDefinesClass / ClassDefinesMethod --
    fdc_rows = []
    cdm_rows = []
    seen_fdc: set[str] = set()
    seen_cdm: set[str] = set()

    if res_active:
        decls = data.resolution.get("declarations", [])
        decl_by_key = {(d["artifact"], d["symbol"]): d for d in decls}

        def _containing_type_id(artifact: str, container: str) -> str:
            cur = container
            for _ in range(4):
                if not cur:
                    return ""
                d = decl_by_key.get((artifact, cur))
                if d is None:
                    return ""
                if d["kind"] == "type":
                    return data.class_id_map.get(f"{artifact}|{cur}", "")
                cur = d.get("container", "")
            return ""

        for d in decls:
            abs_fp = os.path.join(data.target_dir, d["file"])
            fid = data.file_id_map.get(abs_fp, "")
            if d["kind"] == "type":
                cid = data.class_id_map.get(f'{d["artifact"]}|{d["symbol"]}', "")
                if fid and cid:
                    eid = _make_id("fdc", fid, cid)
                    if eid not in seen_fdc:
                        seen_fdc.add(eid)
                        fdc_rows.append([eid, fid, cid])
            else:
                mid = data.method_id_map.get(f'{d["artifact"]}|{d["symbol"]}', "")
                cid = _containing_type_id(d["artifact"], d.get("container", ""))
                if cid and mid:
                    eid = _make_id("cdm", cid, mid)
                    if eid not in seen_cdm:
                        seen_cdm.add(eid)
                        cdm_rows.append([eid, cid, mid])

    for fp, ent in data.extracted_entities.items():
        if res_active and fp in resolved_files:
            continue
        fid = data.file_id_map.get(fp, "")
        for cls in ent.get("classes", []):
            cname = cls.get("name", "")
            cid = data.class_id_map.get(f"{fp}|{cname}", "")
            if not cid:
                continue
            if fid:
                eid = _make_id("fdc", fid, cid)
                if eid not in seen_fdc:
                    seen_fdc.add(eid)
                    fdc_rows.append([eid, fid, cid])
            for meth in cls.get("methods", []):
                mname = meth.get("name", "")
                mid = data.method_id_map.get(f"{fp}|{cname}|{mname}", "")
                if mid:
                    eid = _make_id("cdm", cid, mid)
                    if eid not in seen_cdm:
                        seen_cdm.add(eid)
                        cdm_rows.append([eid, cid, mid])

    counts["FileDefinesClass"] = len(fdc_rows)
    counts["ClassDefinesMethod"] = len(cdm_rows)

    # -- FileBelongsToModule --
    fbm_rows = []
    # Reuse basename_to_fp for file lookups
    for topic in data.topics:
        tname = topic.get("name", "")
        mid = data.module_id_map.get(tname, "")
        if not mid:
            continue
        for fname in topic.get("linked_files", []):
            fp = basename_to_fp.get(os.path.splitext(fname)[0])
            if fp:
                fid = data.file_id_map.get(fp, "")
                if fid:
                    eid = _make_id("fbm", fid, mid)
                    fbm_rows.append([eid, fid, mid])

    counts["FileBelongsToModule"] = len(fbm_rows)

    # -- DirContainsFile --
    dcf_rows = []
    for dp, info in data.dir_tree.items():
        did = data.dir_id_map.get(dp, "")
        if not did:
            continue
        for fp in info.get("files", []):
            fid = data.file_id_map.get(fp, "")
            if fid:
                eid = _make_id("dcf", did, fid)
                dcf_rows.append([eid, did, fid])
    counts["DirContainsFile"] = len(dcf_rows)

    # -- Write all 7 edge types in parallel --
    from concurrent.futures import ThreadPoolExecutor

    edge_writes = [
        ("FileDependsOn", ["edge_id", "source_file", "target_file"], dep_rows),
        ("ClassInherits", ["edge_id", "child_class", "parent_class", "kind"], inherit_rows),
        ("MethodCalls", ["edge_id", "caller_method", "callee_method", "callee_name",
                         "target_symbol", "site_kind", "resolved_via", "call_file", "call_line"], call_rows),
        ("FileDefinesClass", ["edge_id", "file_id", "class_id"], fdc_rows),
        ("ClassDefinesMethod", ["edge_id", "class_id", "method_id"], cdm_rows),
        ("FileBelongsToModule", ["edge_id", "file_id", "module_id"], fbm_rows),
        ("DirContainsFile", ["edge_id", "dir_id", "file_id"], dcf_rows),
    ]

    def _write_edge(args):
        table, columns, rows = args
        if rows:
            _batch_insert(db, table, columns, rows)
            _print(f"  [Phase 9] Wrote {len(rows)} {table} edges")

    with ThreadPoolExecutor(max_workers=7) as pool:
        list(pool.map(_write_edge, edge_writes))

    elapsed = time.time() - t0
    data.timings["phase9_write_edges"] = elapsed
    total_edges = sum(counts.values())
    _print(f"[Phase 9] Wrote {total_edges} edges in {elapsed:.1f}s: {counts}")


# ===================================================================
# Phase 10: Generate Embeddings
# ===================================================================


def phase10_generate_embeddings(data: PipelineData):
    t0 = time.time()

    if not data.file_id_map:
        _print("[Phase 10] No nodes to embed, skipping")
        data.timings["phase10_embeddings"] = 0.0
        return

    import vertexai
    from vertexai.language_models import TextEmbeddingModel

    vertexai.init(project=config.GCP_PROJECT, location="us-central1")
    model = TextEmbeddingModel.from_pretrained(config.EMBED_MODEL)
    db = _get_spanner_db()

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    embed_lock = threading.Lock()

    def _embed_and_write(items: list[tuple[str, str]], table: str, id_col: str, label: str) -> int:
        """Embed items in concurrent batches with retry. Reports failed IDs."""
        total_batches = (len(items) + config.EMBED_BATCH_SIZE - 1) // config.EMBED_BATCH_SIZE
        embedded = 0
        all_failed: list[str] = []
        failed_lock_inner = threading.Lock()

        def _do_batch(batch_items: list[tuple[str, str]], batch_idx: int) -> tuple[int, list[str]]:
            texts = [item[1] for item in batch_items]
            for attempt in range(5):
                try:
                    embeddings = model.get_embeddings(texts)
                    rows = [[item_id, emb.values] for (item_id, _), emb in zip(batch_items, embeddings)]
                    with db.batch() as txn:
                        txn.update(table=table, columns=[id_col, "embedding"], values=rows)
                    return len(batch_items), []
                except Exception as e:
                    err_str = str(e)
                    if ("429" in err_str or "Resource exhausted" in err_str) and attempt < 4:
                        time.sleep(2 ** attempt * 2 + random.uniform(0, 1))
                        continue
                    _log_error(f"[Phase 10] {label} batch {batch_idx} failed after {attempt+1} attempts: {e}")
                    return 0, [item_id for item_id, _ in batch_items]
            return 0, [item_id for item_id, _ in batch_items]

        with ThreadPoolExecutor(max_workers=config.EMBED_CONCURRENCY) as pool:
            futures = {}
            for i in range(0, len(items), config.EMBED_BATCH_SIZE):
                batch = items[i : i + config.EMBED_BATCH_SIZE]
                futures[pool.submit(_do_batch, batch, i)] = i

            done_count = 0
            for future in as_completed(futures):
                count, failed_ids = future.result()
                with embed_lock:
                    embedded += count
                    if failed_ids:
                        all_failed.extend(failed_ids)
                    done_count += 1
                    if done_count % 100 == 0 or done_count == total_batches:
                        elapsed = time.time() - t0
                        print(f"\r  [Phase 10] {label}: {done_count}/{total_batches} batches ({embedded} embedded) {_fmt_elapsed(elapsed)}          ", end="", flush=True)

        print()
        if all_failed:
            _log_error(f"[Phase 10] {label}: {len(all_failed)} items failed embedding: {all_failed[:20]}")
            _print(f"  [Phase 10] WARNING: {len(all_failed)} {label} failed embedding — see error_log.txt")
        return embedded

    # -- File embeddings --
    file_items = []
    for fp, summary in data.file_summaries.items():
        fid = data.file_id_map.get(fp, "")
        if fid and summary:
            file_items.append((fid, _truncate(summary, 2000)))

    updated_files = _embed_and_write(file_items, "Files", "file_id", "files")
    _print(f"  [Phase 10] Embedded {updated_files}/{len(file_items)} files")

    # -- Class embeddings --
    # LSP mode: build items from the resolution declarations so EVERY class
    # node gets exactly one embedding (nested/partial/duplicate-named types
    # are invisible or ambiguous in the entity loop); legacy identity covers
    # the remaining fallback files.
    class_items = []
    seen_class_ids: set[str] = set()

    if _resolution_active(data):
        members_by_type: dict[tuple[str, str], list[str]] = {}
        for d in data.resolution.get("declarations", []):
            if d.get("kind") != "type":
                members_by_type.setdefault(
                    (d["artifact"], d.get("container", "")), []).append(
                    _member_display_name(d["symbol"], d.get("container", "")))
        for d in data.resolution.get("declarations", []):
            if d.get("kind") != "type":
                continue
            cid = data.class_id_map.get(f'{d["artifact"]}|{d["symbol"]}', "")
            if not cid or cid in seen_class_ids:
                continue
            seen_class_ids.add(cid)
            abs_fp = os.path.join(data.target_dir, d["file"])
            file_summary = data.file_summaries.get(abs_fp, "")
            members = members_by_type.get((d["artifact"], d["symbol"]), [])
            text = (f'{d["symbol"]} ({d["artifact"]}): members={", ".join(members)}. '
                    f"{file_summary[:500]}")
            class_items.append((cid, _truncate(text, 2000)))

    for fp, ent in data.extracted_entities.items():
        file_summary = data.file_summaries.get(fp, "")
        for cls in ent.get("classes", []):
            cname = cls.get("name", "")
            cid = data.class_id_map.get(f"{fp}|{cname}", "")
            if not cid or cid in seen_class_ids:
                continue
            seen_class_ids.add(cid)
            method_names = [m.get("name", "") for m in cls.get("methods", [])]
            text = f"{cname} ({cls.get('kind', 'class')}): methods={', '.join(method_names)}. {file_summary[:500]}"
            class_items.append((cid, _truncate(text, 2000)))

    updated_classes = _embed_and_write(class_items, "Classes", "class_id", "classes")
    _print(f"  [Phase 10] Embedded {updated_classes}/{len(class_items)} classes")

    # -- Module embeddings --
    module_items = []
    for tname, summary in data.topic_summaries.items():
        mid = data.module_id_map.get(tname, "")
        if mid and summary:
            module_items.append((mid, _truncate(summary, 2000)))

    updated_modules = _embed_and_write(module_items, "Modules", "module_id", "modules")

    _print(f"  [Phase 10] Embedded {updated_modules}/{len(module_items)} modules")

    elapsed = time.time() - t0
    data.timings["phase10_embeddings"] = elapsed
    total_embedded = updated_files + updated_classes + updated_modules
    _print(f"[Phase 10] Generated {total_embedded} embeddings in {_fmt_elapsed(elapsed)}")


# ===================================================================
# Orchestrator
# ===================================================================


async def run_docs_pipeline(target_dir: str) -> PipelineData:
    """Run documentation generation pipeline (Phases 1-6)."""
    client = genai.Client(
        vertexai=True,
        project=config.GCP_PROJECT,
        location=config.GCP_REGION,
    )
    data = PipelineData(target_dir=os.path.abspath(target_dir))

    phase1_scan(data)
    phase1b_treesitter_entities(data)
    await phase2_file_summaries(data, client)
    await phase3_dir_summaries(data, client)
    await phase4_topics(data, client)
    await phase5_topic_summaries(data, client)
    phase6_index(data)

    return data


def run_graph_pipeline(data: PipelineData):
    """Run Spanner graph generation pipeline (Phases 1.6, 8-10) with checkpointing."""
    phase1c_semantic_resolution(data)
    last_phase = _load_graph_checkpoint(data)

    if last_phase not in ("phase8", "phase9", "phase10"):
        phase8_write_nodes(data)
        _save_graph_checkpoint(data, "phase8")
    else:
        _print(f"[Resume] Skipping Phase 8 (already completed)")

    if last_phase not in ("phase9", "phase10"):
        phase9_write_edges(data)
        _save_graph_checkpoint(data, "phase9")
    else:
        _print(f"[Resume] Skipping Phase 9 (already completed)")

    if last_phase != "phase10":
        phase10_generate_embeddings(data)
        _save_graph_checkpoint(data, "phase10")
    else:
        _print(f"[Resume] Skipping Phase 10 (already completed)")


async def run_pipeline(target_dir: str) -> PipelineData:
    """Run full pipeline: docs + graph (Phases 1-10)."""
    data = await run_docs_pipeline(target_dir)
    run_graph_pipeline(data)
    return data
