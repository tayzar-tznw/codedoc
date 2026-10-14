# Reasoning Index System — アーキテクチャドキュメント

> **重要 / 注意**: 本ドキュメントは **legacy** トラックである `code_doc_agent`（ADK ベースの 6 フェーズドキュメント生成パイプライン）と `vais_query_agent`（Vertex AI Search ベースのクエリエージェント）のアーキテクチャを記述しています。これらは現在 `legacy/` 配下にアーカイブされており、新規開発では使用されません。
>
> **現行システム**は graph-RAG 構成です。ドキュメント生成は `graph_generator/`（`python -m graph_generator`）、クエリは Spanner コードグラフに対する `graph_query_agent`（`mcp_server/` および `webapp/` のチャットから利用）が担当します。現行アーキテクチャの詳細は **[`graph_generator/manual.md`](graph_generator/manual.md)** を参照してください。

## はじめに

Reasoning Index Systemは、Google ADK（Agent Development Kit）を使用して構築されたマルチエージェントシステムです。ソースコードを再帰的にスキャンし、階層的な要約ツリーとトピックツリーを構築することで、コードベースの包括的なドキュメントを自動生成します。

生成されたドキュメントはVertex AI Searchにインデックスされ、セマンティック検索によるクエリ応答を実現します。

## システム全体図

```mermaid
graph TB
    subgraph Input["入力"]
        SC[ソースコード]
    end

    subgraph Builder["code_doc_agent (インデックス構築)"]
        direction TB
        P1[Phase 1: Scanner]
        P2[Phase 2: ファイル要約ループ]
        P3[Phase 3: ディレクトリ要約ループ]
        P4[Phase 4: トピック抽出]
        P5[Phase 5: トピック要約ループ]
        P6[Phase 6: インデックス組立]

        P1 --> P2 --> P3 --> P4 --> P5 --> P6
    end

    subgraph Output["output_docs/"]
        FS[summaries/files/*.md]
        DS[summaries/dirs/*.md]
        TM[topics/*.md]
        RI[reasoning_index.json]
        IX[index.md]
        MT[metadata.json]
    end

    subgraph Cloud["Google Cloud"]
        VS[(Vertex AI Search\nデータストア)]
    end

    subgraph Query["vais_query_agent (クエリ応答)"]
        QA[Agent + VertexAiSearchTool]
    end

    SC --> P1
    P2 --> FS
    P3 --> DS
    P5 --> TM
    P6 --> RI
    P6 --> IX
    P6 --> MT

    Output -->|setup_vertex_search.py| VS
    VS -->|セマンティック検索| QA
    User((ユーザー)) -->|質問| QA
    QA -->|回答| User
```

## パイプライン詳細 — `code_doc_agent`

SequentialAgentがルートエージェントとして、6つのフェーズを順番に実行します。各LoopAgentフェーズは、全対象の処理が完了するまでサブエージェントを繰り返し実行します。

```mermaid
graph LR
    subgraph Phase1["Phase 1: Scanner"]
        S1[scan_and_build_tree]
    end

    subgraph Phase2["Phase 2: ファイル要約"]
        FP[file_picker_agent] --> FSA[file_summarizer_agent]
        FSA -->|未処理あり| FP
        FP -->|全完了| EXIT2[exit_loop]
    end

    subgraph Phase3["Phase 3: ディレクトリ要約"]
        DP[dir_picker_agent] --> DSA[dir_summarizer_agent]
        DSA -->|未処理あり| DP
        DP -->|全完了| EXIT3[exit_loop]
    end

    subgraph Phase4["Phase 4: トピック抽出"]
        TE[topic_extractor_agent]
    end

    subgraph Phase5["Phase 5: トピック要約"]
        TP[topic_picker_agent] --> TW[topic_writer_agent]
        TW -->|未処理あり| TP
        TP -->|全完了| EXIT5[exit_loop]
    end

    subgraph Phase6["Phase 6: インデックス組立"]
        IW[index_writer_agent]
    end

    Phase1 --> Phase2 --> Phase3 --> Phase4 --> Phase5 --> Phase6
```

### Phase 1: Scanner — ディレクトリ走査

```mermaid
sequenceDiagram
    participant U as ユーザー
    participant SA as scanner_agent
    participant T as scan_and_build_tree

    U->>SA: ディレクトリパスを入力
    SA->>T: scan_and_build_tree(path)
    T->>T: os.walk() で再帰走査
    T->>T: ソースファイル収集 (.java, .py, .ts...)
    T->>T: ディレクトリツリー構築
    T->>T: ボトムアップ順序計算 (リーフ→ルート)
    T-->>SA: file_list, dir_tree, dir_queue
    Note over T: state に保存
```

### Phase 2: ファイル要約 — LoopAgent

```mermaid
sequenceDiagram
    participant FP as file_picker_agent
    participant FS as file_summarizer_agent
    participant Tools as ツール関数
    participant State as セッション状態

    loop 各ファイルに対して
        FP->>Tools: get_next_unsummarized_file()
        Tools->>State: file_list と summarized_files を比較
        alt 未処理ファイルあり
            Tools-->>FP: file_path を返却
            FP->>FS: current_file_path を出力
            FS->>Tools: read_source_file(path)
            Tools-->>FS: ファイル内容
            FS->>FS: LLMが要約を生成
            FS->>Tools: save_file_summary(path, summary)
            Tools->>State: summarized_files に追加
        else 全ファイル処理済み
            Tools-->>FP: status: all_done
            FP->>Tools: exit_loop()
            Tools->>Tools: escalate = true
        end
    end
```

### Phase 3: ディレクトリ要約 — ボトムアップ処理

```mermaid
sequenceDiagram
    participant DP as dir_picker_agent
    participant DS as dir_summarizer_agent
    participant Tools as ツール関数

    Note over DP,Tools: リーフディレクトリから処理開始

    loop dir_queue の各ディレクトリに対して
        DP->>Tools: get_next_directory()
        alt 未処理ディレクトリあり
            Tools-->>DP: dir_path を返却
            DP->>DS: current_dir_path を出力
            DS->>Tools: get_child_summaries(dir_path)
            Tools-->>DS: 子ファイル要約 + 子ディレクトリ要約
            DS->>DS: LLMが上位要約を生成
            DS->>Tools: save_directory_summary(dir_path, summary)
        else 全ディレクトリ処理済み
            Tools-->>DP: status: all_done
            DP->>Tools: exit_loop()
        end
    end
```

### Phase 4: トピック抽出

```mermaid
sequenceDiagram
    participant TE as topic_extractor_agent
    participant Tools as ツール関数

    TE->>Tools: get_all_summaries()
    Tools-->>TE: 全ファイル要約 + 全ディレクトリ要約
    TE->>TE: LLMが機能モジュールを特定
    Note over TE: 抽象的な層 (Service Layer) ではなく<br/>機能モジュール (Owner Management) を抽出
    TE->>TE: サブトピック分解 + ファイルリンク設定
    TE->>Tools: save_topics(topics_json)
    Tools-->>TE: topic_tree.json に保存
```

### Phase 5: トピック要約 — ドキュメント生成

```mermaid
sequenceDiagram
    participant TP as topic_picker_agent
    participant TW as topic_writer_agent
    participant Tools as ツール関数

    loop 各トピックに対して
        TP->>Tools: get_next_topic()
        alt 未処理トピックあり
            Tools-->>TP: トピック情報を返却
            TP->>TW: current_topic_name を出力
            TW->>Tools: read_topic_files(topic_name)
            Tools-->>TW: 関連ファイルの内容 + 要約
            TW->>TW: LLMがドキュメント生成
            Note over TW: Mermaidクラス図<br/>シーケンス図<br/>コンポーネント表<br/>相互参照リンク
            TW->>Tools: save_topic_summary(topic_name, summary)
        else 全トピック処理済み
            TP->>Tools: exit_loop()
        end
    end
```

## 状態管理

ADKの`ToolContext.state`を通じて、エージェント間でデータを共有します。

```mermaid
graph TD
    subgraph Phase1State["Phase 1 で設定"]
        FL[file_list<br/>全ソースファイル]
        TD_S[target_dir<br/>ルートパス]
        DT[dir_tree<br/>ディレクトリ階層]
        DQ[dir_queue<br/>ボトムアップ順序]
    end

    subgraph Phase2State["Phase 2 で蓄積"]
        SF[summarized_files<br/>処理済みファイル一覧]
        FS_S[file_summaries<br/>パス→要約マップ]
    end

    subgraph Phase3State["Phase 3 で蓄積"]
        SD[summarized_dirs<br/>処理済みディレクトリ]
        DS_S[dir_summaries<br/>パス→要約マップ]
    end

    subgraph Phase4State["Phase 4 で設定"]
        TP_S[topics<br/>トピック構造リスト]
    end

    subgraph Phase5State["Phase 5 で蓄積"]
        ST[summarized_topics<br/>処理済みトピック]
        TS_S[topic_summaries<br/>名前→要約マップ]
    end

    FL --> SF
    DQ --> SD
    FS_S --> DS_S
    FS_S --> TP_S
    DS_S --> TP_S
    TP_S --> ST
    TP_S --> TS_S
```

## ループ制御パターン

各LoopAgentは同じパターンで制御されます。

```mermaid
stateDiagram-v2
    [*] --> PickNext: ループ開始
    PickNext --> Process: 未処理あり
    Process --> PickNext: 次のイテレーション
    PickNext --> Exit: status = all_done
    Exit --> [*]: escalate = true

    state PickNext {
        [*] --> CallGetNext
        CallGetNext --> CheckStatus
        CheckStatus --> ReturnPath: success
        CheckStatus --> CallExitLoop: all_done
    }

    state Process {
        [*] --> ReadData
        ReadData --> LLMGenerate
        LLMGenerate --> SaveResult
    }
```

## 出力構造

```mermaid
graph TD
    subgraph OutputDocs["output_docs/"]
        IX[index.md<br/>プロジェクト概要<br/>Mermaidアーキテクチャ図<br/>モジュールリンク<br/>コンポーネント表]
        RI[reasoning_index.json<br/>要約ツリー + トピックツリー<br/>機械可読構造化データ]
        MT[metadata.json<br/>タイムスタンプ<br/>モデル名<br/>統計情報]

        subgraph Summaries["summaries/"]
            subgraph Files["files/"]
                F1[Owner.java.md]
                F2[VetController.java.md]
                F3[...]
            end
            subgraph Dirs["dirs/"]
                D1[petclinic.md]
                D2[petclinic_owner.md]
                D3[...]
            end
        end

        subgraph Topics["topics/"]
            TT[topic_tree.json]
            T1[owner_management.md<br/>Mermaid図付き]
            T2[veterinarian_management.md<br/>Mermaid図付き]
            T3[...]
        end
    end
```

## クエリエージェント

```mermaid
sequenceDiagram
    participant U as ユーザー
    participant QA as vais_query_agent
    participant VS as Vertex AI Search
    participant DS as codedoc-index<br/>データストア

    U->>QA: コードに関する質問
    QA->>VS: VertexAiSearchTool<br/>(自動グラウンディング)
    VS->>DS: セマンティック検索
    DS-->>VS: 関連ドキュメント取得
    Note over DS: ファイル要約<br/>ディレクトリ要約<br/>トピックドキュメント<br/>reasoning_index.json
    VS-->>QA: グラウンディング済みコンテキスト
    QA->>QA: Gemini が回答を生成
    QA-->>U: 構造化された回答<br/>ファイル名・クラス名・メソッド参照付き
```

## データストアセットアップフロー

```mermaid
graph LR
    OD[output_docs/] -->|python setup_vertex_search.py| UP[アップロード処理]
    UP -->|42 ドキュメント| DS[(Vertex AI Search<br/>codedoc-index)]

    subgraph Upload["アップロード内容"]
        direction TB
        U1[ファイル要約 x30]
        U2[ディレクトリ要約 x5]
        U3[トピックドキュメント x5]
        U4[reasoning_index.json]
        U5[index.md]
    end

    UP --> Upload
```

## エージェントクラス図

```mermaid
classDiagram
    class SequentialAgent {
        +name: str
        +sub_agents: List[Agent]
    }

    class LoopAgent {
        +name: str
        +sub_agents: List[Agent]
        +max_iterations: int
    }

    class Agent {
        +name: str
        +model: str
        +instruction: str
        +tools: List[Tool]
        +output_key: str
    }

    class ToolContext {
        +state: Dict
        +actions: Actions
    }

    class Actions {
        +escalate: bool
    }

    SequentialAgent *-- Agent : sub_agents
    SequentialAgent *-- LoopAgent : sub_agents
    LoopAgent *-- Agent : sub_agents
    Agent --> ToolContext : ツール経由でアクセス
    ToolContext --> Actions : ループ制御
```

## 使用技術スタック

```mermaid
graph TB
    subgraph Framework["フレームワーク"]
        ADK[Google ADK<br/>Agent Development Kit]
    end

    subgraph Agents["エージェント種類"]
        SA[SequentialAgent<br/>順次実行]
        LA[LoopAgent<br/>反復実行]
        A[Agent / LlmAgent<br/>LLM駆動]
    end

    subgraph Model["LLMモデル"]
        GM[Gemini 3 Flash Preview]
    end

    subgraph Cloud["Google Cloud"]
        VAI[Vertex AI]
        VAS[Vertex AI Search<br/>Discovery Engine]
    end

    subgraph Tools["ツール"]
        FT[Function Tools<br/>カスタムPython関数]
        VAST[VertexAiSearchTool<br/>検索グラウンディング]
    end

    ADK --> Agents
    Agents --> Model
    Model --> VAI
    VAST --> VAS
    ADK --> Tools
```

## ファイル構成

| パス | 役割 |
|-----|------|
| `legacy/code_doc_agent/__init__.py` | パッケージ初期化 |
| `legacy/code_doc_agent/agent.py` | 6フェーズパイプライン定義（13エージェント） |
| `legacy/code_doc_agent/tools.py` | 17個のツール関数（スキャン・要約・トピック・インデックス） |
| `legacy/code_doc_agent/.env` | Vertex AI認証情報 |
| `legacy/vais_query_agent/__init__.py` | パッケージ初期化 |
| `legacy/vais_query_agent/agent.py` | VertexAiSearchTool使用のクエリエージェント |
| `legacy/vais_query_agent/.env` | データストアID設定 |
| `legacy/setup_vertex_search.py` | データストア作成 + ドキュメントアップロードスクリプト |
| `requirements.txt` | `google-adk`, `google-cloud-discoveryengine` |

## 実行方法（legacy）

> 以下は **legacy** トラックの実行手順です。アーカイブされたコードを起動する手順は `legacy/README.md` に集約されているため、実行時はそちらを参照してください。

```bash
# 1. インデックス構築（旧 ADK パイプライン）
adk run legacy.code_doc_agent
# → ソースコードのパスを入力

# 2. Vertex AI Searchにアップロード
python legacy/setup_vertex_search.py

# 3. クエリ応答（旧 VAIS クエリエージェント）
adk run legacy.vais_query_agent
# → 質問を入力（日本語可）
```
