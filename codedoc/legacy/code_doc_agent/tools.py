import json
import os
from collections import defaultdict
from datetime import datetime

from google.adk.tools import ToolContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_EXTENSIONS = {
    ".java", ".py", ".ts", ".js", ".tsx", ".jsx",
    ".go", ".rs", ".cpp", ".c", ".h", ".hpp",
    ".rb", ".kt", ".scala", ".swift", ".cs",
    ".php", ".lua", ".dart", ".ex", ".exs",
    ".razor", ".cshtml", ".fs", ".fsx", ".vb",
}

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".idea", ".vscode", "build", "dist", "target",
    "bin", "obj", ".gradle", ".mvn", "venv", ".venv",
    "Migrations", "wwwroot", "Properties",
}

OUTPUT_DIR = "output_docs"


def _output_root(tool_context: ToolContext) -> str:
    return os.path.join(os.getcwd(), OUTPUT_DIR)


# ===========================================================================
# Phase 1 — Scanner
# ===========================================================================

def scan_and_build_tree(path: str, tool_context: ToolContext) -> dict:
    """Scan a directory recursively, find all source files, and build the directory tree structure for bottom-up summarization.

    Args:
        path: The absolute or relative path to the directory to scan.

    Returns:
        dict: Status with file count, file list, and directory processing queue.
    """
    if not os.path.isdir(path):
        return {"status": "error", "error_message": f"Directory not found: {path}"}

    abs_path = os.path.abspath(path)
    source_files = []

    for root, dirs, files in os.walk(abs_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext in SOURCE_EXTENSIONS:
                source_files.append(os.path.join(root, f))

    # Build directory tree: map each dir to its direct children (files + subdirs)
    dir_children = defaultdict(lambda: {"files": [], "subdirs": set()})
    all_dirs = set()

    for fp in source_files:
        parent = os.path.dirname(fp)
        dir_children[parent]["files"].append(fp)
        all_dirs.add(parent)

        # Register parent chain up to abs_path
        current = parent
        while current != abs_path and current != os.path.dirname(current):
            parent_of_current = os.path.dirname(current)
            dir_children[parent_of_current]["subdirs"].add(current)
            all_dirs.add(parent_of_current)
            current = parent_of_current

    # Include the root scan dir
    all_dirs.add(abs_path)

    # Serializable dir_tree
    dir_tree = {}
    for d in all_dirs:
        dir_tree[d] = {
            "files": dir_children[d]["files"],
            "subdirs": sorted(dir_children[d]["subdirs"]),
        }

    # Compute bottom-up order (leaf dirs first, root last)
    # A directory can only be processed after all its subdirectories
    dir_queue = []
    processed = set()

    def visit(d):
        if d in processed:
            return
        for sub in dir_tree.get(d, {}).get("subdirs", []):
            visit(sub)
        dir_queue.append(d)
        processed.add(d)

    for d in sorted(all_dirs):
        visit(d)

    # Store everything in state
    tool_context.state["file_list"] = source_files
    tool_context.state["target_dir"] = abs_path
    tool_context.state["dir_tree"] = dir_tree
    tool_context.state["dir_queue"] = dir_queue
    tool_context.state["summarized_files"] = []
    tool_context.state["file_summaries"] = {}
    tool_context.state["summarized_dirs"] = []
    tool_context.state["dir_summaries"] = {}
    tool_context.state["topics"] = []
    tool_context.state["summarized_topics"] = []
    tool_context.state["topic_summaries"] = {}

    return {
        "status": "success",
        "total_files": len(source_files),
        "files": source_files,
        "total_directories": len(dir_queue),
        "dir_processing_order": dir_queue,
    }


# ===========================================================================
# Phase 2 — File Summaries
# ===========================================================================

def read_source_file(file_path: str) -> dict:
    """Read the contents of a source code file.

    Args:
        file_path: The absolute path to the source file to read.

    Returns:
        dict: Status and the file contents.
    """
    if not os.path.isfile(file_path):
        return {"status": "error", "error_message": f"File not found: {file_path}"}

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return {"status": "error", "error_message": str(e)}

    max_lines = 10000
    truncated = len(lines) > max_lines
    content = "".join(lines[:max_lines])
    if truncated:
        content += f"\n\n... [TRUNCATED: {len(lines)} lines, showing first {max_lines}]"

    return {
        "status": "success",
        "file_path": file_path,
        "file_name": os.path.basename(file_path),
        "extension": os.path.splitext(file_path)[1],
        "line_count": len(lines),
        "content": content,
    }


def get_next_unsummarized_file(tool_context: ToolContext) -> dict:
    """Get the next source file that has not been summarized yet.

    Returns:
        dict: The next file path, or all_done status.
    """
    file_list = tool_context.state.get("file_list", [])
    done = tool_context.state.get("summarized_files", [])

    for fp in file_list:
        if fp not in done:
            tool_context.state["current_file_path"] = fp
            return {
                "status": "success",
                "file_path": fp,
                "file_name": os.path.basename(fp),
                "remaining": len(file_list) - len(done) - 1,
            }

    return {"status": "all_done", "total_summarized": len(done)}


def save_file_summary(file_path: str, summary: str, tool_context: ToolContext) -> dict:
    """Save a file-level summary to disk and mark the file as summarized.

    Args:
        file_path: The source file that was summarized.
        summary: The summary text to save.

    Returns:
        dict: Status and output path.
    """
    out_root = _output_root(tool_context)
    out_dir = os.path.join(out_root, "summaries", "files")
    os.makedirs(out_dir, exist_ok=True)

    target_dir = tool_context.state.get("target_dir", "")
    rel = os.path.relpath(file_path, target_dir)
    doc_name = rel.replace(os.sep, "_") + ".md"
    doc_path = os.path.join(out_dir, doc_name)

    with open(doc_path, "w", encoding="utf-8") as f:
        f.write(summary)

    # Update state
    done = tool_context.state.get("summarized_files", [])
    if file_path not in done:
        done.append(file_path)
        tool_context.state["summarized_files"] = done

    sums = tool_context.state.get("file_summaries", {})
    sums[file_path] = summary
    tool_context.state["file_summaries"] = sums

    total = len(tool_context.state.get("file_list", []))
    return {
        "status": "success",
        "doc_path": doc_path,
        "progress": f"{len(done)}/{total} files summarized",
    }


# ===========================================================================
# Phase 3 — Directory Summaries (bottom-up)
# ===========================================================================

def get_next_directory(tool_context: ToolContext) -> dict:
    """Get the next directory to summarize in bottom-up order (leaves first).

    Returns:
        dict: The next directory path, or all_done status.
    """
    dir_queue = tool_context.state.get("dir_queue", [])
    done = tool_context.state.get("summarized_dirs", [])

    for d in dir_queue:
        if d not in done:
            tool_context.state["current_dir_path"] = d
            target_dir = tool_context.state.get("target_dir", "")
            return {
                "status": "success",
                "dir_path": d,
                "dir_name": os.path.relpath(d, os.path.dirname(target_dir)),
                "remaining": len(dir_queue) - len(done) - 1,
            }

    return {"status": "all_done", "total_summarized": len(done)}


def get_child_summaries(dir_path: str, tool_context: ToolContext) -> dict:
    """Get summaries of all direct children (files and subdirectories) of a directory.

    Args:
        dir_path: The directory whose children summaries to retrieve.

    Returns:
        dict: Child file summaries and child directory summaries.
    """
    dir_tree = tool_context.state.get("dir_tree", {})
    file_sums = tool_context.state.get("file_summaries", {})
    dir_sums = tool_context.state.get("dir_summaries", {})

    node = dir_tree.get(dir_path, {"files": [], "subdirs": []})

    child_file_summaries = []
    for fp in node["files"]:
        s = file_sums.get(fp, "")
        child_file_summaries.append({
            "file_path": fp,
            "file_name": os.path.basename(fp),
            "summary": s,
        })

    child_dir_summaries = []
    for dp in node["subdirs"]:
        s = dir_sums.get(dp, "")
        child_dir_summaries.append({
            "dir_path": dp,
            "dir_name": os.path.basename(dp),
            "summary": s,
        })

    return {
        "status": "success",
        "dir_path": dir_path,
        "child_files": child_file_summaries,
        "child_dirs": child_dir_summaries,
    }


def save_directory_summary(dir_path: str, summary: str, tool_context: ToolContext) -> dict:
    """Save a directory-level summary to disk and mark the directory as summarized.

    Args:
        dir_path: The directory that was summarized.
        summary: The summary text to save.

    Returns:
        dict: Status and output path.
    """
    out_root = _output_root(tool_context)
    out_dir = os.path.join(out_root, "summaries", "dirs")
    os.makedirs(out_dir, exist_ok=True)

    target_dir = tool_context.state.get("target_dir", "")
    rel = os.path.relpath(dir_path, os.path.dirname(target_dir))
    doc_name = rel.replace(os.sep, "_") + ".md"
    doc_path = os.path.join(out_dir, doc_name)

    with open(doc_path, "w", encoding="utf-8") as f:
        f.write(summary)

    # Update state
    done = tool_context.state.get("summarized_dirs", [])
    if dir_path not in done:
        done.append(dir_path)
        tool_context.state["summarized_dirs"] = done

    sums = tool_context.state.get("dir_summaries", {})
    sums[dir_path] = summary
    tool_context.state["dir_summaries"] = sums

    total = len(tool_context.state.get("dir_queue", []))
    return {
        "status": "success",
        "doc_path": doc_path,
        "progress": f"{len(done)}/{total} directories summarized",
    }


# ===========================================================================
# Phase 4 — Topic Extraction
# ===========================================================================

def get_all_summaries(tool_context: ToolContext) -> dict:
    """Get the complete summary tree: all file summaries and directory summaries.

    Returns:
        dict: All file and directory summaries organized hierarchically.
    """
    file_sums = tool_context.state.get("file_summaries", {})
    dir_sums = tool_context.state.get("dir_summaries", {})
    target_dir = tool_context.state.get("target_dir", "")

    file_entries = []
    for fp, s in file_sums.items():
        file_entries.append({
            "file_path": fp,
            "file_name": os.path.basename(fp),
            "relative_path": os.path.relpath(fp, target_dir),
            "summary": s,
        })

    dir_entries = []
    for dp, s in dir_sums.items():
        dir_entries.append({
            "dir_path": dp,
            "dir_name": os.path.basename(dp),
            "relative_path": os.path.relpath(dp, os.path.dirname(target_dir)),
            "summary": s,
        })

    return {
        "status": "success",
        "file_summaries": file_entries,
        "directory_summaries": dir_entries,
        "total_files": len(file_entries),
        "total_dirs": len(dir_entries),
    }


def save_topics(topics_json: str, tool_context: ToolContext) -> dict:
    """Save the extracted topic tree with file links.

    Args:
        topics_json: A JSON string representing the topic tree. Expected format:
            [
              {
                "name": "Topic Name",
                "linked_files": ["file1.java", "file2.java"],
                "subtopics": [
                  {"name": "Subtopic Name", "linked_files": ["file1.java"]}
                ]
              }
            ]

    Returns:
        dict: Status and topic count.
    """
    try:
        topics = json.loads(topics_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "error_message": f"Invalid JSON: {e}"}

    tool_context.state["topics"] = topics
    tool_context.state["summarized_topics"] = []

    # Also save to disk
    out_root = _output_root(tool_context)
    os.makedirs(os.path.join(out_root, "topics"), exist_ok=True)
    topic_path = os.path.join(out_root, "topics", "topic_tree.json")
    with open(topic_path, "w", encoding="utf-8") as f:
        json.dump(topics, f, indent=2)

    return {
        "status": "success",
        "topic_count": len(topics),
        "saved_to": topic_path,
    }


# ===========================================================================
# Phase 5 — Topic Summaries
# ===========================================================================

def get_next_topic(tool_context: ToolContext) -> dict:
    """Get the next topic that needs a detailed summary written.

    Returns:
        dict: The next topic with its subtopics and linked files, or all_done.
    """
    topics = tool_context.state.get("topics", [])
    done = tool_context.state.get("summarized_topics", [])

    for topic in topics:
        name = topic.get("name", "")
        if name not in done:
            tool_context.state["current_topic_name"] = name
            return {
                "status": "success",
                "topic": topic,
                "remaining": len(topics) - len(done) - 1,
            }

    return {"status": "all_done", "total_summarized": len(done)}


def read_topic_files(topic_name: str, tool_context: ToolContext) -> dict:
    """Read the source files linked to a specific topic and its subtopics.

    Args:
        topic_name: The name of the topic whose linked files to read.

    Returns:
        dict: The contents of all linked files.
    """
    topics = tool_context.state.get("topics", [])
    target_dir = tool_context.state.get("target_dir", "")

    # Find the topic
    topic = None
    for t in topics:
        if t.get("name") == topic_name:
            topic = t
            break

    if not topic:
        return {"status": "error", "error_message": f"Topic not found: {topic_name}"}

    # Collect all linked file names (from topic + subtopics)
    linked = set(topic.get("linked_files", []))
    for sub in topic.get("subtopics", []):
        linked.update(sub.get("linked_files", []))

    # Resolve file names to full paths and read contents
    file_list = tool_context.state.get("file_list", [])
    file_contents = []

    for fname in sorted(linked):
        # Find matching file path
        matched = None
        for fp in file_list:
            if os.path.basename(fp) == fname or fp == fname or fp.endswith(fname):
                matched = fp
                break

        if matched and os.path.isfile(matched):
            try:
                with open(matched, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                file_contents.append({
                    "file_name": fname,
                    "file_path": matched,
                    "content": content[:20000],  # Limit size
                })
            except Exception:
                continue

    # Also include file summaries for context
    file_sums = tool_context.state.get("file_summaries", {})
    file_summary_list = []
    for fname in sorted(linked):
        for fp, s in file_sums.items():
            if os.path.basename(fp) == fname or fp.endswith(fname):
                file_summary_list.append({"file_name": fname, "summary": s})
                break

    return {
        "status": "success",
        "topic_name": topic_name,
        "topic": topic,
        "file_contents": file_contents,
        "file_summaries": file_summary_list,
    }


def save_topic_summary(topic_name: str, summary: str, tool_context: ToolContext) -> dict:
    """Save a detailed topic summary to disk and mark it as complete.

    Args:
        topic_name: The name of the topic.
        summary: The detailed summary text.

    Returns:
        dict: Status and output path.
    """
    out_root = _output_root(tool_context)
    out_dir = os.path.join(out_root, "topics")
    os.makedirs(out_dir, exist_ok=True)

    # Sanitize topic name for filename
    safe_name = topic_name.lower().replace(" ", "_")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
    doc_path = os.path.join(out_dir, f"{safe_name}.md")

    with open(doc_path, "w", encoding="utf-8") as f:
        f.write(summary)

    # Update state
    done = tool_context.state.get("summarized_topics", [])
    if topic_name not in done:
        done.append(topic_name)
        tool_context.state["summarized_topics"] = done

    sums = tool_context.state.get("topic_summaries", {})
    sums[topic_name] = summary
    tool_context.state["topic_summaries"] = sums

    total = len(tool_context.state.get("topics", []))
    return {
        "status": "success",
        "doc_path": doc_path,
        "progress": f"{len(done)}/{total} topic summaries written",
    }


# ===========================================================================
# Phase 6 — Assemble Reasoning Index
# ===========================================================================

def write_reasoning_index(tool_context: ToolContext) -> dict:
    """Assemble the final Reasoning Index: reasoning_index.json and index.md.

    Returns:
        dict: Status and paths of output files.
    """
    out_root = _output_root(tool_context)
    os.makedirs(out_root, exist_ok=True)

    target_dir = tool_context.state.get("target_dir", "")
    file_sums = tool_context.state.get("file_summaries", {})
    dir_sums = tool_context.state.get("dir_summaries", {})
    dir_tree = tool_context.state.get("dir_tree", {})
    topics = tool_context.state.get("topics", [])
    topic_sums = tool_context.state.get("topic_summaries", {})

    # Build hierarchical summary tree
    def build_node(path):
        node_info = dir_tree.get(path, {"files": [], "subdirs": []})
        children = []
        for fp in node_info["files"]:
            children.append({
                "path": os.path.relpath(fp, target_dir),
                "type": "file",
                "name": os.path.basename(fp),
                "summary": file_sums.get(fp, ""),
            })
        for dp in node_info["subdirs"]:
            children.append(build_node(dp))
        return {
            "path": os.path.relpath(path, os.path.dirname(target_dir)),
            "type": "directory",
            "name": os.path.basename(path),
            "summary": dir_sums.get(path, ""),
            "children": children,
        }

    summary_tree = build_node(target_dir)

    # Build topic tree with summaries
    topic_tree = []
    for t in topics:
        entry = {
            "name": t.get("name", ""),
            "summary": topic_sums.get(t.get("name", ""), ""),
            "linked_files": t.get("linked_files", []),
            "subtopics": [],
        }
        for sub in t.get("subtopics", []):
            entry["subtopics"].append({
                "name": sub.get("name", ""),
                "linked_files": sub.get("linked_files", []),
            })
        topic_tree.append(entry)

    # Write reasoning_index.json
    index_data = {
        "summary_tree": summary_tree,
        "topic_tree": topic_tree,
    }
    json_path = os.path.join(out_root, "reasoning_index.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2)

    # Write index.md (human-readable, in Japanese)
    repo_name = os.path.basename(target_dir)
    repo_summary = summary_tree.get("summary", "")

    md_lines = [f"# {repo_name} ドキュメント\n"]
    md_lines.append("## はじめに\n")
    md_lines.append(f"{repo_summary}\n")

    # Architecture overview with Mermaid diagram
    md_lines.append("## アーキテクチャ概要\n")
    md_lines.append("### 高レベルコンポーネント図\n")
    md_lines.append("```mermaid")
    md_lines.append("graph TD")
    # Build diagram from topics
    for i, t in enumerate(topic_tree):
        safe_id = f"M{i}"
        md_lines.append(f"    {safe_id}[\"{t['name']}\"]")
    # Connect topics that share files
    for i, t1 in enumerate(topic_tree):
        for j, t2 in enumerate(topic_tree):
            if j > i:
                shared = set(t1.get("linked_files", [])) & set(t2.get("linked_files", []))
                if shared:
                    md_lines.append(f"    M{i} --> M{j}")
    md_lines.append("```\n")

    # Module dependencies with cross-references
    md_lines.append("### モジュールドキュメント\n")
    md_lines.append("本システムは以下のモジュールで構成されています:\n")
    for t in topic_tree:
        safe_name = t["name"].lower().replace(" ", "_")
        safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
        md_lines.append(f"*   **[{t['name']}](topics/{safe_name}.md)**: {t['summary'][:150].split(chr(10))[0]}")
    md_lines.append("")

    # Component table
    md_lines.append("## コンポーネント一覧\n")
    md_lines.append("| ファイル | モジュール | 概要 |")
    md_lines.append("|------|--------|---------|")
    for fp, s in file_sums.items():
        name = os.path.relpath(fp, target_dir)
        first_line = s.split("\n")[0][:80] if s else ""
        # Find which topic this file belongs to
        file_basename = os.path.basename(fp)
        module = ""
        for t in topic_tree:
            if file_basename in t.get("linked_files", []):
                module = t["name"]
                break
        md_lines.append(f"| {name} | {module} | {first_line} |")

    # Directory summaries
    md_lines.append("\n## ディレクトリ構成\n")
    for dp, s in dir_sums.items():
        name = os.path.relpath(dp, os.path.dirname(target_dir))
        md_lines.append(f"**{name}**\n{s[:300]}\n")

    index_md_path = os.path.join(out_root, "index.md")
    with open(index_md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    # Write metadata.json
    generated_files = ["index.md", "reasoning_index.json", "metadata.json"]
    # List topic files
    topics_dir = os.path.join(out_root, "topics")
    if os.path.isdir(topics_dir):
        for fn in sorted(os.listdir(topics_dir)):
            generated_files.append(f"topics/{fn}")
    # List summary files
    sfiles_dir = os.path.join(out_root, "summaries", "files")
    if os.path.isdir(sfiles_dir):
        for fn in sorted(os.listdir(sfiles_dir)):
            generated_files.append(f"summaries/files/{fn}")
    sdirs_dir = os.path.join(out_root, "summaries", "dirs")
    if os.path.isdir(sdirs_dir):
        for fn in sorted(os.listdir(sdirs_dir)):
            generated_files.append(f"summaries/dirs/{fn}")

    metadata = {
        "generation_info": {
            "timestamp": datetime.now().isoformat(),
            "main_model": "gemini-3.5-flash",
            "generator": "reasoning_index_pipeline",
            "repo_path": target_dir,
        },
        "statistics": {
            "total_files": len(file_sums),
            "total_directories": len(dir_sums),
            "total_topics": len(topics),
            "total_topic_summaries": len(topic_sums),
        },
        "files_generated": generated_files,
    }
    metadata_path = os.path.join(out_root, "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    return {
        "status": "success",
        "json_path": json_path,
        "index_md_path": index_md_path,
        "metadata_path": metadata_path,
    }


# ===========================================================================
# Loop Control
# ===========================================================================

def exit_loop(tool_context: ToolContext) -> dict:
    """Signal that the current loop should stop.

    Returns:
        dict: Confirmation of loop exit.
    """
    tool_context.actions.escalate = True
    return {"status": "loop_exit", "message": "Exiting loop."}
