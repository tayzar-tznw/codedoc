from google.adk.agents import Agent
from google.adk.agents.loop_agent import LoopAgent
from google.adk.agents.sequential_agent import SequentialAgent

from .tools import (
    scan_and_build_tree,
    read_source_file,
    get_next_unsummarized_file,
    save_file_summary,
    get_next_directory,
    get_child_summaries,
    save_directory_summary,
    get_all_summaries,
    save_topics,
    get_next_topic,
    read_topic_files,
    save_topic_summary,
    write_reasoning_index,
    exit_loop,
)

MODEL = "gemini-2.5-pro"

# ===================================================================
# Phase 1 — Scanner
# ===================================================================

scanner_agent = Agent(
    name="scanner_agent",
    model=MODEL,
    description="Scans a directory and builds the directory tree for processing.",
    instruction="""You are a scanner agent. When the user provides a directory path,
call scan_and_build_tree with that path.

Report the total files found, the total directories, and the bottom-up processing order.""",
    tools=[scan_and_build_tree],
    output_key="scan_result",
)

# ===================================================================
# Phase 2 — File Summaries
# ===================================================================

file_picker_agent = Agent(
    name="file_picker_agent",
    model=MODEL,
    description="Picks the next unsummarized file.",
    instruction="""You pick the next file to summarize.
        Step 1: Call get_next_unsummarized_file.
        Step 2: If status is "all_done", call exit_loop immediately.
        Step 3: If a file is returned, output ONLY its file_path. Nothing else.""",
    tools=[get_next_unsummarized_file, exit_loop],
    output_key="current_file",
)

file_summarizer_agent = Agent(
    name="file_summarizer_agent",
    model=MODEL,
    description="Reads a source file and produces a concise summary.",
    instruction="""You summarize source code files. ALL output MUST be written in Japanese (日本語).

STEP 1: Call read_source_file with file_path = {current_file_path}

STEP 2: Analyze the code and create a concise summary (2-4 paragraphs) in Japanese covering:
- ファイルの目的と役割
- 主要なクラスとその責務
- 重要なメソッド/関数とその動作
- 依存関係（インポート）と他ファイルとの関連
- 使用されているデザインパターン

STEP 3: You MUST call save_file_summary with:
- file_path: {current_file_path}
- summary: your summary text (in Japanese)

CRITICAL: You MUST call save_file_summary. Do not just output text.
CRITICAL: The summary MUST be written entirely in Japanese (日本語で記述すること).""",
    tools=[read_source_file, save_file_summary],
    output_key="file_summary_result",
)

file_summary_loop = LoopAgent(
    name="file_summary_loop",
    sub_agents=[file_picker_agent, file_summarizer_agent],
    max_iterations=500,
)

# ===================================================================
# Phase 3 — Directory Summaries (bottom-up)
# ===================================================================

dir_picker_agent = Agent(
    name="dir_picker_agent",
    model=MODEL,
    description="Picks the next directory to summarize in bottom-up order.",
    instruction="""You pick the next directory to summarize.

Step 1: Call get_next_directory.
Step 2: If status is "all_done", call exit_loop immediately.
Step 3: If a directory is returned, output ONLY its dir_path. Nothing else.""",
    tools=[get_next_directory, exit_loop],
    output_key="current_dir",
)

dir_summarizer_agent = Agent(
    name="dir_summarizer_agent",
    model=MODEL,
    description="Summarizes a directory based on its children summaries.",
    instruction="""You create directory-level summaries by synthesizing child summaries. ALL output MUST be written in Japanese (日本語).

STEP 1: Call get_child_summaries with dir_path = {current_dir_path}

STEP 2: Read the child file summaries and child directory summaries returned.
Create a higher-level summary (2-3 paragraphs) in Japanese that:
- このディレクトリ/モジュールの全体的な目的を説明する
- 内部のファイルがどのように連携しているかを説明する
- 主要なコンポーネントとその相互作用を強調する
- このレベルで見られるパターンやアーキテクチャ上の判断を記述する

STEP 3: You MUST call save_directory_summary with:
- dir_path: {current_dir_path}
- summary: your directory summary text (in Japanese)

CRITICAL: You MUST call save_directory_summary. Do not just output text.
CRITICAL: The summary MUST be written entirely in Japanese (日本語で記述すること).""",
    tools=[get_child_summaries, save_directory_summary],
    output_key="dir_summary_result",
)

dir_summary_loop = LoopAgent(
    name="dir_summary_loop",
    sub_agents=[dir_picker_agent, dir_summarizer_agent],
    max_iterations=200,
)

# ===================================================================
# Phase 4 — Topic Extraction
# ===================================================================

topic_extractor_agent = Agent(
    name="topic_extractor_agent",
    model=MODEL,
    description="Extracts topics and subtopics from the summary tree.",
    instruction="""You extract functional modules from a codebase summary tree. Module names MUST be in Japanese (日本語).

STEP 1: Call get_all_summaries to get all file and directory summaries.

STEP 2: Identify 3-8 functional MODULES of the codebase. A module is a group of
files that work together to provide a specific feature or capability.

IMPORTANT: Identify modules by FEATURE/FUNCTION, not by abstract architectural layer.
IMPORTANT: Module and subtopic names MUST be in Japanese.
GOOD examples: "オーナー管理", "獣医モジュール", "コアドメインモデル", "システム設定"
BAD examples: "サービス層", "データアクセス層", "コントローラー層"

For each module:
- Name it in Japanese after the feature it provides (e.g. "オーナー管理" not "ビジネスロジック")
- List ALL source files that belong to this module (entities, controllers, repositories, validators, etc.)
- Decompose into 2-4 subtopics (in Japanese) covering different aspects (e.g. "ドメインエンティティ", "データアクセス", "Webコントローラー", "バリデーション")
- Link each subtopic to its specific files

STEP 3: You MUST call save_topics with a JSON string in this exact format:
[
  {
    "name": "モジュール名（日本語）",
    "linked_files": ["File1.java", "File2.java", "File3.java"],
    "subtopics": [
      {"name": "サブトピック名（日本語）", "linked_files": ["File1.java"]}
    ]
  }
]

CRITICAL: You MUST call save_topics with valid JSON. Do not just output text.
Every topic and subtopic MUST have linked_files that reference actual file names from the summaries.
Each file should appear in at least one module. Prefer grouping by package/directory when possible.
CRITICAL: All names MUST be in Japanese (日本語で記述すること).""",
    tools=[get_all_summaries, save_topics],
    output_key="topic_extraction_result",
)

# ===================================================================
# Phase 5 — Topic Summaries
# ===================================================================

topic_picker_agent = Agent(
    name="topic_picker_agent",
    model=MODEL,
    description="Picks the next topic that needs a detailed summary.",
    instruction="""You pick the next topic to write a summary for.

Step 1: Call get_next_topic.
Step 2: If status is "all_done", call exit_loop immediately.
Step 3: If a topic is returned, output ONLY its name. Nothing else.""",
    tools=[get_next_topic, exit_loop],
    output_key="current_topic",
)

topic_writer_agent = Agent(
    name="topic_writer_agent",
    model=MODEL,
    description="Writes a detailed summary for a topic based on its linked files.",
    instruction="""You write professional module documentation with diagrams, tables, and cross-references.
ALL documentation MUST be written in Japanese (日本語).

STEP 1: Call read_topic_files with topic_name = {current_topic_name}

STEP 2: Based on the file contents and file summaries, create a comprehensive module
document in markdown (in Japanese) with ALL of the following sections:

# [モジュール名]

## はじめに
このモジュールの役割とアプリケーション内での位置づけを1-2段落で説明する。

## アーキテクチャと設計
使用されているアーキテクチャパターン（MVC、Repository等）を説明する。

### コンポーネント関係図
Mermaidクラス図で主要クラスとその関係を示す:
```mermaid
classDiagram
    class ClassName {
        +type fieldName
        +returnType methodName()
    }
    ClassA <|-- ClassB : extends
    ClassA "1" *-- "many" ClassC : contains
```

### モジュールコンポーネント
テーブルを含める:
| コンポーネント | 責務 |
|-----------|----------------|
| **ClassName** | 何をするか |

## データフロー
主要な処理のMermaidシーケンス図を含める:
```mermaid
sequenceDiagram
    participant User
    participant Controller
    participant Repository
    User->>Controller: アクション
    Controller->>Repository: クエリ
```

## 主要コンポーネント
各重要クラスについてリンク付きで説明する:
### [ClassName](../summaries/files/ClassName.java.md)
クラスの役割、主要メソッド、重要な動作を日本語で記述する。

## 依存関係
他モジュールへの依存をリンク付きで記述する:
- **[他モジュール](other_module.md)**: このモジュールがどのように依存しているか。

STEP 3: You MUST call save_topic_summary with:
- topic_name: {current_topic_name}
- summary: your complete markdown document (in Japanese)

CRITICAL: You MUST call save_topic_summary. Do not just output text.
Include Mermaid diagrams and tables - these are essential for quality documentation.
CRITICAL: ALL text content (headings, descriptions, explanations) MUST be in Japanese (日本語で記述すること).
Note: Class names, method names, and code identifiers should remain in their original language.""",
    tools=[read_topic_files, save_topic_summary],
    output_key="topic_summary_result",
)

topic_summary_loop = LoopAgent(
    name="topic_summary_loop",
    sub_agents=[topic_picker_agent, topic_writer_agent],
    max_iterations=100,
)

# ===================================================================
# Phase 6 — Assemble Reasoning Index
# ===================================================================

index_writer_agent = Agent(
    name="index_writer_agent",
    model=MODEL,
    description="Assembles the final Reasoning Index files.",
    instruction="""You finalize the Reasoning Index.

Call write_reasoning_index to assemble the final output files:
- reasoning_index.json (machine-readable structured index)
- index.md (human-readable overview)

Report the paths of the generated files.""",
    tools=[write_reasoning_index],
    output_key="index_result",
)

# ===================================================================
# Root Pipeline
# ===================================================================

root_agent = SequentialAgent(
    name="reasoning_index_pipeline",
    sub_agents=[
        scanner_agent,          # Phase 1: Scan & build tree
        file_summary_loop,      # Phase 2: Summarize each file
        dir_summary_loop,       # Phase 3: Summarize directories bottom-up
        topic_extractor_agent,  # Phase 4: Extract topics from summaries
        topic_summary_loop,     # Phase 5: Write detailed topic summaries
        index_writer_agent,     # Phase 6: Assemble final index
    ],
    description="Builds a Reasoning Index: summary tree + topic tree from source code.",
)
