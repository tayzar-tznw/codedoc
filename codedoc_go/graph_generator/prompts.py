"""Prompt templates for Gemini calls (the wiki track, spec §7)."""

# Part of every summary cache key (R30): bump it whenever a prompt changes.
PROMPT_VERSION = "go-wiki-2"

NODE_BATCH_PROMPT = """あなたは Go のソースコードアナリストです。以下のファイルを読み、ファイル全体と、一覧の各宣言を日本語で要約してください。

ファイル: {relpath}（パッケージ {package}）

```go
{source}
```

### 宣言とグラフ上の関係
{declarations}

次の JSON だけを返してください。"nodes" には上の一覧のすべての宣言を含めてください。キーには一覧の各行の「- 」と「（」の間にある qualname をそのまま使い、インポートパスやパッケージ名を前に付けないでください（「FeeCalculator.Calculate」は可、「example.com/iryo/billing.FeeCalculator.Calculate」や「billing.FeeCalculator.Calculate」は不可）:
{{"file": "ファイルの目的と役割（2〜4文）", "nodes": {{"<qualname>": "宣言の役割（1〜2文）"}}}}

CRITICAL: Return ONLY valid JSON."""

NODE_SINGLE_PROMPT = """あなたは Go のソースコードアナリストです。以下の宣言の役割を日本語で1〜2文に要約してください。

ファイル: {relpath}（パッケージ {package}）
宣言: {qualname}（{kind}）
{context}

```go
{source}
```

要約だけを書いてください。"""

FILE_FROM_NODES_PROMPT = """あなたは Go のソースコードアナリストです。以下はファイル {relpath}（パッケージ {package}）の宣言ごとの要約です。ファイル全体の目的と役割を日本語で2〜4文に要約してください。

{node_summaries}

要約だけを書いてください。"""

PACKAGE_SUMMARY_PROMPT = """あなたはソフトウェアアーキテクトです。Go パッケージ {import_path}（ディレクトリ {directory}、パッケージ名 {name}）の各ファイルの要約から、パッケージ全体の要約（1〜2段落）を日本語で書いてください。

### ファイルの要約:
{file_summaries}

以下の点を含めてください：
- パッケージの目的と、外から使われる主な型と関数
- ファイルの分担

要約だけを書いてください。"""

DIR_SUMMARY_PROMPT = """あなたはソフトウェアアーキテクトです。以下のディレクトリ内のファイル要約とサブディレクトリ要約を統合し、
このディレクトリの全体的な要約（2-3段落）を日本語で作成してください。

ディレクトリ: {dir_name}

### 子ファイルの要約:
{child_file_summaries}

### 子ディレクトリの要約:
{child_dir_summaries}

以下の点を含めてください：
- このディレクトリの全体的な目的
- 内部のファイルがどのように連携しているか
- 主要なコンポーネントとその相互作用
- このレベルで見られるパターンやアーキテクチャ上の判断

日本語で要約を記述してください。出力に要約以外余計なことを書かないでください。"""

TOPIC_EXTRACTION_PROMPT = """あなたはソフトウェアアーキテクトです。以下のファイル要約一覧から、コードベースの機能トピックを3-8個識別してください。

トピックは機能・特徴別に分類してください（アーキテクチャ層別ではなく）。
良い例: "診療報酬の計算", "受付と予約", "患者ポータル"
悪い例: "サービス層", "データアクセス層"

各行は「**リポジトリ相対パス**: 要約」の形式です。

### ファイル要約:
{summaries}

以下のJSON形式で返してください（日本語のトピック名）。"files" には上の一覧のリポジトリ相対パスをそのまま書いてください。ファイル名だけにはしないでください（doc.go のような同名ファイルが多数あります）:
[
  {{
    "name": "トピック名（日本語）",
    "summary": "トピックの要約（1文）",
    "files": ["libs/billing/fee.go", "libs/billing/doc.go"],
    "subtopics": [
      {{"name": "サブトピック名（日本語）", "files": ["libs/billing/fee.go"]}}
    ]
  }}
]

CRITICAL: Return ONLY valid JSON. Each file should appear in at least one topic. Copy each path exactly as listed."""

TOPIC_SUMMARY_PROMPT = """あなたはテクニカルライターです。以下のトピックについて、包括的なドキュメントを日本語で作成してください。

トピック名: {topic_name}

### 関連ファイルの内容:
{file_contents}

### ファイルの要約:
{file_summaries}

以下のセクションを含むMarkdownドキュメントを作成してください：

# {topic_name}

## はじめに
このトピックの役割とアプリケーション内での位置づけ。

## アーキテクチャと設計
使用されているアーキテクチャパターンを説明。

### コンポーネント関係図
```mermaid
classDiagram
    class TypeName {{
        +Type fieldName
        +MethodName() ReturnType
    }}
```

### トピックコンポーネント
| コンポーネント | 責務 |
|-----------|----------------|
| **TypeName** | 何をするか |

## データフロー
```mermaid
sequenceDiagram
    participant User
    participant Component
    User->>Component: アクション
```

## 主要コンポーネント
各重要な型と関数について説明。

## 依存関係
他トピックへの依存。

日本語で記述してください。型名・関数名・メソッド名はそのまま使用。"""

TOPIC_MERGE_PROMPT = """以下はディレクトリごとに抽出したトピックの一覧です（「番号. トピック名（ファイル数）: 要約」）。これらを3-15個のグローバルトピックに統合してください。
重複を除去し、関連するトピックを統合してください。トピックは機能・特徴別にしてください（アーキテクチャ層別ではなく）。

入力トピック:
{topics}

JSON配列形式で返してください（日本語のトピック名）。"members" には統合した入力トピックの番号を書いてください。すべての番号を少なくとも1つのトピックに含め、一覧にない番号は使わないでください。"subtopics" の "members" は、そのトピックの番号の一部です:
[{{"name": "トピック名", "summary": "トピックの要約（1〜2文）", "members": [1, 3], "subtopics": [{{"name": "サブトピック名", "members": [3]}}]}}]

CRITICAL: Return ONLY valid JSON."""
