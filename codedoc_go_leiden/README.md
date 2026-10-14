# CodeDoc (Go, Leiden トピック) — Go モジュールのコードグラフと日本語 Wiki を作る

Go モジュール（`go.mod`、`go.work`、`vendor/`）を読み、Cloud Spanner のコードグラフと Gemini による日本語 Wiki を作るツールの、導入・設定・実行・運用マニュアルです。この版（codedoc_go_leiden）は codedoc_go の兄弟で、トピックを LLM ではなく Leiden 法のコミュニティ検出で決めます。
内部構造は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、呼び出し先を確定する仕組みは [docs/LSP_RESOLUTION.md](docs/LSP_RESOLUTION.md)、グラフの中身は [docs/GRAPH_SCHEMA.md](docs/GRAPH_SCHEMA.md)、モジュールとリポジトリをまたぐ扱いは [docs/CROSS_PROJECT.md](docs/CROSS_PROJECT.md) に分けています。
本書は「どう使うか」だけを扱い、「なぜそうなっているか」は扱いません。

- [これは何か](#これは何か)
- [前提条件](#前提条件)
- [セットアップ](#セットアップ)
- [設定リファレンス](#設定リファレンス)
- [CLI リファレンス](#cli-リファレンス)
- [doctor と事前確認](#doctor-と事前確認)
- [Go ツールチェーン](#go-ツールチェーン)
- [オフラインとダウンロード](#オフラインとダウンロード)
- [ピアリポジトリ](#ピアリポジトリ)
- [出力](#出力)
- [鮮度とレジューム](#鮮度とレジューム)
- [webapp と MCP サーバーをローカルで動かす](#webapp-と-mcp-サーバーをローカルで動かす)
- [トラブルシューティング](#トラブルシューティング)
- [リポジトリ構成](#リポジトリ構成)

## これは何か

CodeDoc は Go のソースツリー（`.go`）を読み、2 つの成果物を作ります。

- **グラフ**: Cloud Spanner のプロパティグラフです。パッケージ・ファイル・型・関数とメソッドのノードと、確定できた呼び出し・埋め込み・インターフェースの実装・import のエッジを持ちます。
- **Wiki**: Gemini が書く日本語の要約（関数・型ごと、ファイル、パッケージ、ディレクトリ）と、トピック別のページ、検索用の埋め込みです。
- **トピック**: 確定した呼び出し・埋め込み・実装から Leiden 法で求めた、型と関数のまとまりです。メンバーは `--graph` が LLM なしで決め、`--wiki` の Gemini は名前とページを書くだけです。大きなトピックはサブトピックに分かれます。

グラフには `graph_query_agent` が GQL で問い合わせます。MCP サーバーと Web UI から自然言語で質問できます。

`compare-topics` は、同じリポジトリについて codedoc_go の LLM のトピックとこの版のトピックを比べた報告を作ります（GCP は使いません）。

呼び出し先は、同梱の Go プログラム gohelper が確定します。gohelper は go/packages と go/types でパッケージを読み込みます。言語サーバーは使いません。宣言と呼び出し位置の抽出は go/parser です。プロジェクトのコードは実行せず、解析対象のリポジトリには何も書きません。依存モジュールは既定でオフラインで扱い、ネットワークに出るのは `--download-modules` を付けたときだけです。

| ドキュメント | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 構成要素とデータの流れ、フェーズとトラック、チェックポイント、gohelper、問い合わせの経路、設計判断、Leiden トピック（Phase 4a、4b、5）、LLM トピックとの比較と結果 |
| [docs/LSP_RESOLUTION.md](docs/LSP_RESOLUTION.md) | Phase 1.6。グループの読み込み、位置の分類、インターフェース呼び出し、規則とゲート、`resolutions.json`、検証の数字 |
| [docs/GRAPH_SCHEMA.md](docs/GRAPH_SCHEMA.md) | Spanner のノード表とエッジ表（トピックの表を含む）、行の具体例、ID 規則、GQL クエリ集 |
| [docs/CROSS_PROJECT.md](docs/CROSS_PROJECT.md) | 1 つのリポジトリの中の複数モジュール、ピアリポジトリ、`crossref` によるリポジトリ間エッジ |

## 前提条件

| 項目 | 要件 |
|---|---|
| Python | ツールの実行に 3.12 以上。venv は uv で 3.13 を指定して作る |
| uv | venv の作成に使う |
| Leiden の実装 | graspologic-native 1.3.1 と networkx 3.7（`requirements.txt` で固定。numpy と scipy も入る）。import できなければ `analyze` と `doctor` は事前確認で止まり、`compare-topics` も止まる（終了コード 3）。別のアルゴリズムに切り替えることはない |
| Go ツールチェーン | go1.26.0 以上。下限は `gohelper/go.mod` の `go` 行で決まる。無ければ `setup go` が固定版の go1.27.2 を `.tools/go` に入れる（linux/amd64 のみ）。解析対象の `go` 行より古いツールチェーンでは、そのグループを解決しない（[Go ツールチェーン](#go-ツールチェーン)） |
| C コンパイラ | 不要。`GO_BUILD_CGO=1` のときだけ go コマンドが gcc と pkg-config を実行する |
| Google Cloud | プロジェクト、`gcloud` CLI、Application Default Credentials（`gcloud auth application-default login`）。`--wiki` / `--all`（Gemini）と Spanner への書き込みに使う。`analyze --graph --no-spanner`、`evaluate`、`trial`、`compare-topics` には要らない |
| Vertex AI | `aiplatform.googleapis.com` を有効化。Gemini（既定 `gemini-3.5-flash`）と埋め込みモデル `text-embedding-005` を使う |
| Cloud Spanner | `spanner.googleapis.com` を有効化。`setup spanner` は Enterprise エディション 100 PU のインスタンスを作る（プロパティグラフに必要。存在する間は課金される） |
| ネットワーク | 通常は不要。`--download-modules` のときだけ、モジュールプロキシ（既定 `https://proxy.golang.org`）とチェックサムデータベースに接続する |

## セットアップ

```bash
git clone <repository-url>
cd codedoc_go_leiden
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
source .venv/bin/activate

python -m graph_generator setup go        # go1.27.2 を .tools/go に入れる（手元の Go を使うなら不要）
python -m graph_generator doctor          # ツールチェーン、gohelper のビルド、Spanner、Gemini をまとめて確認

gcloud auth application-default login     # Gemini と Spanner を使うときだけ
gcloud services enable aiplatform.googleapis.com spanner.googleapis.com

python -m graph_generator init            # 対話で .env を生成（既存の .env は --force で上書き）
# または: cp .env.example .env して編集

python -m graph_generator setup spanner   # インスタンス + DB + テーブル + プロパティグラフ
```

- `python -m graph_generator` の各コマンドは `OUTPUT_DIR` をカレントディレクトリ基準で解決します。必ず `codedoc_go_leiden/` で実行します。venv を有効にしない場合は `python` の代わりに `.venv/bin/python` を使います。
- `.env` はリポジトリのルートに 1 つだけ置きます。パイプライン、`graph_query_agent`、webapp、MCP サーバー、`mcp_server/deploy.sh` はすべてこのファイルを読みます。`graph_query_agent/.env` は作らないでください。`adk run` がそちらを優先して読み、ルートの設定が効かなくなります。
- gohelper は初回の実行で自動的にビルドされます（`go build -mod=vendor -trimpath`。依存は `gohelper/vendor/` にあるのでネットワークは使わない）。置き場所は `OUTPUT_DIR/go/bin/gohelper-<Go の版>-<ソースのハッシュ>` です。ツールチェーンか gohelper のソースが変わったときだけ作り直します。
- `setup go` は go.dev から go1.27.2（linux-amd64）のアーカイブを取得し、コードに固定した SHA-256 と照合してから `.tools/go` に展開します。`GO_TOOLCHAIN` が未設定のとき、このツールチェーンが使われます。

既存の DB を新しいスキーマに追随させるだけなら次を使います。不足しているテーブルとカラムだけを足し、プロパティグラフを作り直します。データは消えません。

```bash
python -m graph_generator.setup_spanner_graph --migrate
```

## 設定リファレンス

設定はすべてルートの `.env` から `graph_generator/config.py` が読みます。「コード定数」と書いた行は `.env` では変えられません。空のキーは未設定として扱います。

**GCP**

| キー | 既定値 | 変わること |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | 未設定（ADC / gcloud の既定のプロジェクトを使う） | Gemini・埋め込み・Spanner の接続先プロジェクト。パイプラインと `graph_query_agent` が同じこの設定を読み、使うプロジェクトを起動時に表示する |
| `GOOGLE_CLOUD_LOCATION` | `global` | Gemini と埋め込みの Vertex AI ロケーション |
| `GOOGLE_GENAI_USE_VERTEXAI` | `true`（`.env.example`） | Gemini クライアントを Vertex AI 経由にする。未設定だと API キーを要求される |
| `GCE_METADATA_MTLS_MODE` | `none`（`.env.example`） | GCE 上で mTLS 起因の SSL エラーが出るときの回避策 |

**Gemini と Wiki**

| キー | 既定値 | 変わること |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.5-flash` | Phase 2、3、4b、5 のモデル。`graph_query_agent` のモデルも同じキーで決まる |
| `GEMINI_CONCURRENCY` | `100` | Gemini の同時リクエスト数。429 が続くなら 10〜30 に下げる |
| `MAX_OUTPUT_TOKENS` | `8192` | 1 リクエストの出力上限。thinking も消費するので、下げすぎると空応答になる |
| `WIKI_FILE_MAX_CHARS` | `40000` | ファイルのソースがこの文字数を超えると、Phase 2 は関数・型ごとに 1 回ずつ要約する |
| `WIKI_FILE_MAX_NODES` | `40` | ファイルの宣言がこの数を超えたときも同じく 1 つずつ要約する |
| `WIKI_SUMMARIZE_GENERATED` | `0` | `1` にすると生成コード（`// Code generated ... DO NOT EDIT.`）も Gemini で要約する。`0` ではドキュメントコメントと「生成コード」の注記だけで、Gemini を呼ばない |
| `WIKI_SUMMARY_CACHE` | 未設定（`OUTPUT_DIR/repos/<repo>/summaries`） | 要約キャッシュの置き場所。`<ディレクトリ>/<repo>/` を使う。codedoc_go と同じディレクトリを指すと、2 つの版で同じ要約を 2 度作らない |

**Spanner・グラフ**

| キー | 既定値 | 変わること |
|---|---|---|
| `SPANNER_INSTANCE` | `codedoc-instance` | 書き込み・検証・問い合わせ先のインスタンス |
| `SPANNER_DATABASE` | `codedoc-go-leiden` | 同データベース。codedoc_go の既定 `codedoc-go` を含め、ほかの CodeDoc の既定 DB とは別にしてある |
| `GRAPH_NAME` | `code_graph_a` | プロパティグラフ名。`ID_PREFIX` と組で 1 つの DB に複数のグラフを置ける |
| `ID_PREFIX` | `a` | 全ノード・エッジ ID の接頭辞。変えると別のグラフになる |
| `POSSIBLY_CALLS_MAX_CANDIDATES` | `5` | インターフェース呼び出しの実装候補と、曖昧な呼び出しの候補を `PossiblyCalls` に展開する上限。超えると張らない |
| `EMBED_CONCURRENCY` | `20` | Phase 10 で同時に投げる埋め込みバッチ数 |
| `SPANNER_BATCH_SIZE`（コード定数） | `5000` | 1 ミューテーションに詰める行数 |
| `EMBED_MODEL` / `EMBED_BATCH_SIZE`（コード定数） | `text-embedding-005` / `20` | 埋め込みモデルと 1 バッチのテキスト数 |

**トピック（Leiden）**

| キー | 既定値 | 変わること |
|---|---|---|
| `TOPIC_MIN` | `5` | レベル 1 のトピックの数の帯の下限。解像度はこの帯の中で選ぶ（[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#102-leiden-法と解像度)） |
| `TOPIC_MAX` | `25` | 同じ帯の上限 |
| `SUBTOPIC_MIN_ATOMS` | `12` | primary のメンバーがこの数より多いトピックを、サブトピックに分けてみる |
| `TOPIC_PROFILE` | `strict` | 原子のグラフに使う関係。`strict` か、`degree_norm`・`calls_only`・`no_implements`・`with_tests` を `+` でつないだもの（[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#101-射影)） |
| `TOPIC_SEEDS` | `42,43,44,45,46` | Leiden 法の種。0 から 2^64-1 までの、カンマ区切りの重ならない整数 |

- `TOPIC_MIN`、`TOPIC_MAX`、`SUBTOPIC_MIN_ATOMS` は整数で書きます。
- 整数でない値、帯の下限が上限より大きいなどの値の誤り、`TOPIC_PROFILE` と `TOPIC_SEEDS` の誤りは、どれも事前確認の `topic settings` の行になり、どのモードでも（`--wiki` と `doctor` を含む）止まります（終了コード 3）。行は、キー、値、書ける形を示します（例 `TOPIC_MIN must be an integer, not 'abc'`）。

**Go ツールチェーンとビルド構成**

| キー | 既定値 | 変わること |
|---|---|---|
| `GO_TOOLCHAIN` | 未設定 | 使う `go` のパス（GOROOT のディレクトリでもよい）。未設定なら `setup go` で入れた `.tools/go/bin/go`、それも無ければ `PATH` の `go`（[Go ツールチェーン](#go-ツールチェーン)） |
| `GO_BUILD_GOOS` | `linux` | 主ビルド構成の GOOS。ビルド制約（`//go:build`、`_linux.go` などのファイル名）の評価とパッケージの読み込みに使う |
| `GO_BUILD_GOARCH` | `amd64` | 主ビルド構成の GOARCH。使い方は `GO_BUILD_GOOS` と同じ |
| `GO_BUILD_TAGS` | 空 | 主ビルド構成のビルドタグ。カンマ区切り |
| `GO_BUILD_CGO` | `0` | `1` で `CGO_ENABLED=1`。go コマンドが gcc と pkg-config をリポジトリの指定で実行する |
| `GO_EXTRA_BUILDS` | 空 | 主ビルド構成が外したファイルを解決する追加の構成。`goos/goarch[+tag+tag]` を空白で区切る（例 `windows/amd64 darwin/arm64+e2e`）。追加の構成は cgo なしで、構成ごとに読み込みが 1 回増える |
| `GO_HELPER_TIMEOUT` | `1800` | gohelper の 1 回の実行（と go コマンドのダウンロード）の上限秒数 |

**モジュールの入手先**

| キー | 既定値 | 変わること |
|---|---|---|
| `GO_MODULES_DOWNLOAD` | `0` | `1` で毎回 `--download-modules` を付けたのと同じになる（[オフラインとダウンロード](#オフラインとダウンロード)） |
| `GO_USER_MODCACHE` | 未設定 | オフラインで読むユーザーのモジュールキャッシュ（`go env GOMODCACHE` の場所）。読むだけで書かない。未設定ならユーザーの環境で `go env GOMODCACHE` を調べる |
| `GO_PROXY` | `https://proxy.golang.org` | ダウンロードモードのモジュールプロキシ。`direct` と `off` の項目は取り除く（VCS からは取得しない） |
| `GO_SUMDB` | `sum.golang.org` | ダウンロードモードのチェックサムデータベース。`off` で問い合わせない。オフラインでは常に `off` |

**ピア・スキャン・出力**

| キー | 既定値 | 変わること |
|---|---|---|
| `PEER_REPOS` | 未設定 | ピアリポジトリ。`[name=]path` を `:`（Windows では `;`）で区切る（[ピアリポジトリ](#ピアリポジトリ)） |
| `OUTPUT_DIR` | `output_docs_pipeline` | 生成物、チェックポイント、gohelper、モジュールキャッシュの置き場。webapp も同じ変数を読む。解析対象のリポジトリの外を指す |
| `SOURCE_EXTENSIONS`（コード定数） | `.go` | スキャン対象の拡張子。どのディレクトリとファイルを飛ばすかは go コマンドの規則に従う（`testdata`、`_` か `.` で始まる名前、`vendor/` の下、go.mod の `ignore`） |

**サーバー（MCP、webapp）**

| キー | 既定値 | 変わること |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8080` | `python -m mcp_server` の待ち受け |
| `WEBAPP_REPO` | 未設定 | webapp が表示するリポジトリ名。未設定で `OUTPUT_DIR/repos/` にリポジトリが 1 つだけならそれを使う |

## CLI リファレンス

すべて `python -m graph_generator <command>` の形式です。

| コマンド | フラグ | 動作 |
|---|---|---|
| `init` | `--force` | 対話で `.env` を生成 |
| `setup go` | なし | 固定版の go1.27.2 を `.tools/go` に入れる。SHA-256 を照合してから展開する |
| `setup spanner` | `--instance` `--database` `--region`（既定 `asia-northeast1`） `--skip-instance` | インスタンス・DB・テーブル・プロパティグラフを作る。既存の DB は差分だけ足す |
| `doctor [target_dir]` | なし | 事前確認を表で表示する（[doctor と事前確認](#doctor-と事前確認)）。`target_dir` を渡すとそのリポジトリのモジュールとグループも調べる |
| `analyze [target_dir]` | `--all` / `--graph` / `--wiki` のどれか 1 つ（必須）、`--repo-name` `--repos` `--peer` `--download-modules` `--no-spanner` | 解析を実行する（下の表） |
| `validate` | なし | テーブル別の行数、リポジトリ別のノード数、孤立エッジ、リポジトリ間の結合度を表示 |
| `crossref` | `--dry-run` | 投入済みの全リポジトリからリポジトリ間エッジを導出し、`crossref_report.json` を書き、エッジを Spanner に書く。前回同じ書き込み先に書いて今回は導かなかったエッジは消す（記録は `crossref_rows.json`）。`--dry-run` は Spanner にも `crossref_rows.json` にも触れない（`crossref_report.json` は書く） |
| `evaluate` | `--fixture iryo-kanri-system\|go_layouts\|multi_repo\|all`（既定 `all`） `--env` `--dump-edges` `--quiet-misses` | フィクスチャに対する精度評価。GCP もネットワークも使わない。`--env` は同梱の合成プロキシ `test_codes/_proxy` から第三者モジュールを入れて評価する。`--dump-edges` は導出したエッジを作業ディレクトリの `edges.json` に書く。`--quiet-misses` は外れたケースの詳細を出さない |
| `trial target_dir` | `--repo-name`（必須） `--packages P...` `--download-modules` `--skip-oracle` | 実リポジトリでの試験。`--no-spanner` 相当のグラフトラックと、実行時オラクルによる精度確認（GCP 不要）。`--packages` はオラクルが単体テストを動かすパッケージ、`--download-modules` はダウンロードモードの回も走らせる、`--skip-oracle` は精度確認を飛ばす |
| `compare-topics` | `--llm DIR`（必須） `--leiden DIR`（必須） `--llm2 DIR` `--leiden2 DIR` `--features FILE` `--out DIR` | codedoc_go の LLM のトピックとこの版のトピックを比べ、`topic_comparison.json` と `topic_comparison.md` を書く。GCP は使わない（[compare-topics](#compare-topics)） |

`analyze` の 3 つのモード:

| モード | すること | 前提 |
|---|---|---|
| `--graph` | スキャン、抽出、解決、トピックのメンバーの決定（Phase 4a）、グラフのノードとエッジとトピックの表の書き込み。Gemini は呼ばない | Spanner のスキーマがある（`--no-spanner` なら不要）。Leiden の実装が入っている |
| `--wiki` | ノード・ファイル・パッケージ・ディレクトリの要約、トピックの名前とページ、要約と埋め込みの書き戻し | 同じファイルの内容で終わり、トピックを作れた `--graph` がある。Spanner の書き込み先も同じ（[鮮度とレジューム](#鮮度とレジューム)） |
| `--all` | `--graph` のあと、同じプロセスで `--wiki` | `--graph` と同じ。加えて Gemini の設定 |

フェーズの中身は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) にあります。

`analyze` のフラグ:

| フラグ | 意味 |
|---|---|
| `target_dir` | 解析対象ディレクトリ（1 リポジトリ）。`--repos` を使うときは渡さない |
| `--repo-name NAME` | グラフ上のリポジトリ名。既定はディレクトリ名。`--repos` とは一緒に使えない |
| `--repos MANIFEST` | 複数リポジトリを順に処理するマニフェスト。並んだリポジトリは互いにピアになる |
| `--peer [name=]path` | ピアリポジトリ。繰り返し指定できる（[ピアリポジトリ](#ピアリポジトリ)） |
| `--download-modules` | ダウンロードモード。読み込みに要るモジュールを `OUTPUT_DIR/go/modcache` に取得する。`GO_MODULES_DOWNLOAD=1` と同じ |
| `--no-spanner` | Spanner に書かず、行と要約を `OUTPUT_DIR` の下にだけ書く |

終了コード:

| コード | 意味 |
|---|---|
| `0` | 成功 |
| `1` | その他のエラー |
| `2` | 使い方の誤り（フラグ、ピアの指定、マニフェスト、存在しないディレクトリ） |
| `3` | 事前確認の失敗（[doctor と事前確認](#doctor-と事前確認)） |
| `4` | `--wiki` の前提を満たさない |

Spanner のインフラ操作はモジュール形式でも実行できます。

```bash
python -m graph_generator.setup_spanner_graph [--project P] [--region R] [--instance I] [--database D] [--skip-instance]
python -m graph_generator.setup_spanner_graph --migrate   # 不足テーブル・カラムの追加のみ
python -m graph_generator.setup_spanner_graph --verify    # テーブルとグラフの存在確認
python -m graph_generator.setup_spanner_graph --destroy   # DB とインスタンスを削除（確認入力あり）
```

### 実行レシピ

```bash
# まず確認（ツールチェーン、gohelper、対象のモジュールとグループ、オフラインで欠けるモジュール、Spanner、Gemini）
python -m graph_generator doctor /path/to/repo

# グラフだけ。Spanner を使わずに中身を見る
python -m graph_generator analyze /path/to/repo --graph --no-spanner

# グラフを Spanner へ。そのあと Wiki
python -m graph_generator analyze /path/to/repo --graph
python -m graph_generator analyze /path/to/repo --wiki
python -m graph_generator validate

# 1 回で両方
python -m graph_generator analyze /path/to/repo --all

# 依存モジュールを取得してから解決する（ネットワークが必要）
python -m graph_generator analyze /path/to/repo --graph --download-modules

# 複数リポジトリを 1 グラフへ（逐次。依存先をピアとして渡す）
python -m graph_generator analyze ./shared --graph --repo-name shared
python -m graph_generator analyze ./web --graph --repo-name web --peer shared=./shared
python -m graph_generator crossref

# 複数リポジトリを 1 グラフへ（マニフェスト）
python -m graph_generator analyze --repos repos.json --all
python -m graph_generator crossref

# トピックを codedoc_go の LLM のトピックと比べる（2 つの版の analyze --all の後。GCP は使わない）
python -m graph_generator compare-topics --llm ../codedoc_go/output_docs_pipeline/repos/<repo> --leiden output_docs_pipeline/repos/<repo>

# 回帰ゲート（GCP もネットワークも使わない）。抽出・解決・導出のコードを変えたら通す
python -m graph_generator evaluate
python -m graph_generator evaluate --env

# テスト。既定の実行は GCP も Go も使わない。本物の go と gohelper を使うテストは -m go で別に走らせる
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q -m go --no-cov
```

`evaluate` は選んだ全フィクスチャがすべてのゲートを満たしたときに終了コード 0 を、それ以外は 1 を返します。作業ディレクトリ `OUTPUT_DIR/eval_<fixture>`（`--env` では `eval_<fixture>_env`）は毎回作り直します。指標の意味は [docs/LSP_RESOLUTION.md](docs/LSP_RESOLUTION.md) の検証の節にあります。いずれかのリポジトリを再投入したら `crossref` も再実行します。

### `--repos` マニフェスト形式

JSON 配列です。要素は `{name?, path}` のオブジェクトか、パス文字列です。

```json
[
  {"name": "shared", "path": "./shared"},
  {"name": "web", "path": "./web"},
  "./api"
]
```

- `name` を省略するとディレクトリ名になります。別のパスでもディレクトリ名が同じなら同じリポジトリとみなされるので、明示を推奨します。
- 相対パスはコマンド実行時のカレントディレクトリ基準です。
- マニフェストのリポジトリは互いにピアになります。
- 全リポジトリを同じ `.env`（同じインスタンス・DB・`GRAPH_NAME`・`ID_PREFIX`）で投入します。

### compare-topics

同じリポジトリを、codedoc_go とこの版の両方で、同じファイル、同じビルド構成、同じモード（オフラインかダウンロードか）で `analyze --all` してから実行します。F1 なら、両方の版に同じディレクトリを渡します。

```bash
(cd ../codedoc_go && python -m graph_generator analyze test_codes/iryo-kanri-system --all --no-spanner)
python -m graph_generator analyze ../codedoc_go/test_codes/iryo-kanri-system --all --no-spanner
python -m graph_generator compare-topics \
    --llm ../codedoc_go/output_docs_pipeline/repos/iryo-kanri-system \
    --leiden output_docs_pipeline/repos/iryo-kanri-system \
    --features test_codes/iryo-kanri-system/ground_truth/features.json
```

| フラグ | 意味 |
|---|---|
| `--llm DIR` | codedoc_go の `OUTPUT_DIR/repos/<repo>` |
| `--leiden DIR` | この版の `OUTPUT_DIR/repos/<repo>` |
| `--llm2 DIR` | codedoc_go の 2 回目の実行。LLM のトピックの安定を測る。`--llm` と同じ入力でなければ止まる |
| `--leiden2 DIR` | この版の 2 回目の実行。省くと、その場で Leiden 法を計算し直して比べる。`--leiden` と同じ入力、同じトピックの設定でなければ止まる |
| `--features FILE` | レビューした機能の一覧 `{"features": [{"name", "files"}]}`。F1 には `test_codes/iryo-kanri-system/ground_truth/features.json` がある |
| `--out DIR` | 報告の置き場所。既定は `--leiden` のディレクトリ（`topics/` ではない） |

- 比べるときに Leiden 法を計算し直す（2 回目の実行の代わりと、`implements` を除いた比較）ので、Leiden の実装が要ります。
- 指標の意味は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#11-llm-トピックとの比較) にあります。

| 終了コード | 意味 |
|---|---|
| `0` | 報告を書いた |
| `2` | 使い方の誤り（ディレクトリでないパス、読めない `--features`） |
| `3` | Leiden の実装（graspologic-native、networkx）を import できない |
| `4` | 比べられない。2 つの実行のファイルのハッシュ、ビルド構成、モード、`resolutions.json` のどれかが違う。要るもの（codedoc_go の `topics/topic_tree.json`、この版の `topics/communities.json`、両方の `entities.json`、`units.json`、`resolutions.json`、この版の `graph_rows/`、`--llm2` と `--leiden2` を渡したときはその `topic_tree.json` と `communities.json`）が無い。この版の `graph_rows/` から `communities.json` と同じ射影を作れない |

## doctor と事前確認

`doctor` は事前確認の結果を 1 つの表に出し、問題があれば直し方も並べます。`analyze` も同じ確認を最初に行い、止めるべき問題があれば時間や料金を使う前に終了コード 3 で止まります。

```
  Check           Status  Detail
  --------------  ------  ----------------------------------------
  builds          ok      linux-amd64 (primary)
  toolchain       ok      go1.27.2 (<codedoc_go_leiden>/.tools/go/bin/go)
  helper          ok      <OUTPUT_DIR>/go/bin/gohelper-go1.27.2-<hash>
  memory          ok      <空きメモリ> available
  modules         ok      1 modules in 1 groups; modules offline (file-proxy)
  group m0        warn    module, 1 modules, status ok; 2 required modules are missing offline, so calls into them will be unresolved
                          fix: add --download-modules (or GO_MODULES_DOWNLOAD=1)
  spanner         ok      codedoc-instance/codedoc-go-leiden (graph code_graph_a, ID prefix a): schema present
  gemini          ok      gemini-3.5-flash in global, project <project>
  leiden backend  ok      graspologic-native 1.3.1, networkx 3.7
  topic settings  ok      band 5-25 topics, subtopics above 12 primary atoms, profile strict, seeds 42,43,44,45,46
```

| 区分 | 項目 |
|---|---|
| 止める（終了コード 3） | 使えるツールチェーンが無い、またはツールチェーンが gohelper の下限より古い。gohelper のビルドに失敗した。全グループの `go` 行がツールチェーンより新しい。`--no-spanner` なしで、Spanner のスキーマが無いか定義と違う（足りない表やカラム、型の違うカラム）。`--wiki` / `--all` で Gemini の設定が無い。Leiden の実装（graspologic-native、networkx）を import できない、`TOPIC_*` か `SUBTOPIC_MIN_ATOMS` の値が正しくない（どちらも `doctor` でも止める） |
| 警告だけ | オフラインで欠けるモジュール、縮退したグループ（`degraded`）、ツールチェーンより新しい `toolchain` 行、依存モジュールの新しい `go` 行、整合しない `vendor/` を持つグループ、空きメモリの不足、graspologic-native の版が固定した 1.3.1 と違う |

モジュール、グループ、ツールチェーンについての警告は `units.json` の `warnings` にも残ります。

## Go ツールチェーン

1 つのツールチェーンですべてを実行します。探す順番は `GO_TOOLCHAIN`、`setup go` で入れた `.tools/go/bin/go`、`PATH` の `go` です。

- 必要な版は go1.26.0 以上です（`gohelper/go.mod` の `go` 行）。古ければ `doctor` と `analyze` が止まり、`setup go` か `GO_TOOLCHAIN` を案内します。
- go コマンドはすべて `GOTOOLCHAIN=local` で動きます。別の版のツールチェーンを自動でダウンロードすることはありません。
- 解析対象の `go.work` と `go.mod` の `go` 行（vendor したモジュールでは `vendor/modules.txt` の `## go` 行も）をツールチェーンと比べます。新しい行があるグループは解決しません（status `toolchain`。サイトは `unresolved`、理由 `toolchain`）。警告には行の位置と直し方が出ます。
- `toolchain` 行が新しいだけなら警告にとどめ、グループは解決します。`GOTOOLCHAIN=local` の go コマンドはその行を無視して読み込めるためです。標準ライブラリの呼び出し先は、その版のツールチェーンで読んだ場合と違うことがあります。
- 依存モジュールの `go` 行が新しい場合は、キャッシュにある `.mod` から読み取れたときに警告します。go コマンドはそのグループの読み込み全体を拒むので、解決後にグループは status `error` になります。

直し方は、より新しい `go` を `GO_TOOLCHAIN` で指定することです。必要な版が go1.27.2 以下なら `setup go` でも足ります。

## オフラインとダウンロード

依存モジュールのソースをどこから得るかは 2 通りです。どちらでも、解析対象のリポジトリ（`.git/index` を含む）とユーザーのモジュールキャッシュには何も書きません。go コマンドが書くもの（`go.work.sum` など）は `OUTPUT_DIR/repos/<repo>/go/<group>/` の制御ディレクトリに置きます。

**オフライン（既定）**

- 整合した `vendor/` を持つグループは、それ以上何も要りません。
- それ以外は、ユーザーのモジュールキャッシュ（`GO_USER_MODCACHE`、未設定なら `go env GOMODCACHE`）を `file://` のプロキシとして読み、`OUTPUT_DIR/go/modcache` に展開します。展開したものは以後のリポジトリと実行で再利用します。
- 手に入らないモジュールへの呼び出しと、その型の値に対する呼び出しは `unresolved`（理由 `missing-module`）になります。別の呼び出し先にすり替わることはありません。
- `doctor` と `analyze` は、欠けるモジュールの数と名前をグループごとに出します（`units.json` の `missing_modules`）。ワークスペースのグループは、モジュールグラフの `.mod` が 1 つでも欠けると外部のパッケージがすべて読めなくなります。リポジトリ内のパッケージは読めます。

**ダウンロード（`--download-modules` または `GO_MODULES_DOWNLOAD=1`）**

- 読み込みの前に、グループごとに `go list -deps -test` で必要なモジュールだけを `OUTPUT_DIR/go/modcache` に取得します。`go mod download` より少ない量で済みます（2026-10-13 の計測で、etcd は 306 MB 対 3.7 GB、Prometheus は 1.76 GB 対 2.8 GB）。
- 取得元は、ユーザーのモジュールキャッシュ、次に `GO_PROXY` の順です。チェックサムは go.sum と `GO_SUMDB` で照合します。VCS のツールは実行しないので、非公開モジュール（`GOPRIVATE` の対象）は取得できません。
- 取得に失敗したモジュールは名前と理由を報告し、実行は続けます。単一モジュールのグループでは、そのモジュールのパッケージだけが `unresolved` になります。ワークスペースのグループでは、モジュールグラフの `.mod` が 1 つでも取れないと、外部のパッケージがすべて読めなくなります。
- 単一モジュールのグループは、制御ディレクトリにある go.mod のコピーで `-mod=mod` の取得を行い、go.sum に無い行を補います。取得のあと、go.mod のコピーは元の内容（リポジトリと同じ require と replace）に戻し、読み込みは開発者のビルドと同じ readonly で行います。
- そのため、リポジトリの go.mod が require していないモジュールを import するパッケージは、開発者のビルドと同じく読み込めず、その呼び出しは `unresolved` になります。go.mod が古く、go コマンドがモジュール全体を拒むとき（`updates to go.mod needed`）は、グループ全体が `unresolved` です。足りない require と、それを import するパッケージは `go.mod lacks requirements for:` で始まる警告に出ます。

## ピアリポジトリ

別のリポジトリのモジュールを require しているとき、そのリポジトリを「ピア」として渡すと、Phase 1.6 で呼び出し先をピアのソースまで確定できます。渡さなければ、その呼び出しはモジュールキャッシュの公開版を指す `external` か、オフラインで手に入らなければ `unresolved` になります。

- `analyze --repos repos.json`: マニフェストのリポジトリが互いにピアになります。
- `analyze <dir> --peer [name=]path`（繰り返し可）、または `PEER_REPOS`: 逐次投入のときに明示します。並ぶ順は `--repos` の他のリポジトリ、`--peer`、`PEER_REPOS` です。
- `name` の既定はディレクトリ名です。そのピアを投入した（これから投入する）ときのリポジトリ名と同じにします。`crossref` はこの名前でノードを探します。
- 1 つの名前を 2 つのパスに付けたとき（マニフェストの名前の重複を含む）、1 つのパスに 2 つの名前を付けたとき、取り込むリポジトリ自身をピアにしたとき、パスが空か存在しないときは、終了コード 2 で止まります。
- ピアのソースが使われるのは、グループのモジュールがそのモジュールパスを require しているときだけです。置き換えない場合（vendor したグループ、2 つのピアが同じパスを持つとき、リポジトリがすでに replace しているときなど）は警告に出ます。
- 投入が終わったら `crossref` を実行します。リポジトリ間のエッジはここで書かれます。

規則の詳細と結果の見え方は [docs/CROSS_PROJECT.md](docs/CROSS_PROJECT.md) にあります。

## 出力

`analyze` は `OUTPUT_DIR/repos/<repo>/` の下に書きます（リポジトリ名は `--repo-name` か対象ディレクトリ名）。リポジトリをまたいで共有する状態は `OUTPUT_DIR/go/` に、`crossref`、`evaluate`、`trial` の結果は `OUTPUT_DIR` 直下に書きます。

| パス（`OUTPUT_DIR` 基準） | 書く処理 | 内容 |
|---|---|---|
| `repos/<repo>/units.json` | Phase 1 | モジュール、グループ、ビルド構成、モード、モジュールの入手先、欠けるモジュール、ピア、スキップの件数、警告 |
| `repos/<repo>/entities.json` | Phase 1.5 | gohelper の抽出結果。ファイルごとの宣言、呼び出し位置、import |
| `repos/<repo>/resolutions.json` | Phase 1.6 | 呼び出し・import・埋め込み・実装の解決結果と統計。構造は [docs/LSP_RESOLUTION.md](docs/LSP_RESOLUTION.md) |
| `repos/<repo>/go/<group>/` | Phase 1.6 | グループの制御ディレクトリ。生成した `go.work` か go.mod のコピー、`go.work.sum` / go.sum、`vendor` へのリンク、gohelper の出力 `resolve-<build>.jsonl`。実行のたびに作り直す |
| `repos/<repo>/graph_rows/<Table>.jsonl` | Phase 8〜9 | ノード行とエッジ行、トピックの 5 つの表の行。Spanner に書くときも書く。`--wiki` がグラフの文脈をここから読む |
| `repos/<repo>/graph_meta.json` | Phase 8 | リポジトリ名、対象ディレクトリ、`id_scheme`、`id_prefix`、Spanner の書き込み先。`crossref` と `--wiki` が読む |
| `repos/<repo>/graph_prune.json` | Phase 8〜9 | 前回書いた行の主キー（書き込み先ごと）。Phase 9 が今回書かなかった行を消すのに使い、消し終えると消える（[鮮度とレジューム](#鮮度とレジューム)） |
| `repos/<repo>/graph_checkpoint.json` | Phase 8〜9 | 完了したフェーズ、ファイルのハッシュ、`resolutions.json` のハッシュ、コミュニティのハッシュ、Spanner の書き込み先 |
| `repos/<repo>/topics/communities.json` | Phase 4a | Leiden 法の設定と試行、選んだ解像度、レベルごとのトピックのメンバーと役割、指標（[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#105-phase-4a-の出力と失敗)） |
| `repos/<repo>/topics/projection.jsonl` | Phase 4a | 射影の台帳。原子の組ごとの関係、数、重み |
| `repos/<repo>/pipeline_data.pkl` | `--graph` | グラフトラックの状態。`--wiki` が読む |
| `repos/<repo>/summaries/nodes/<relpath>.json` | Phase 2 | ファイルとその関数・型の要約。キャッシュを兼ねる（`WIKI_SUMMARY_CACHE` を設定するとそちらにも書く） |
| `repos/<repo>/summaries/packages.json`、`summaries/dirs.json` | Phase 3 | パッケージとディレクトリの要約 |
| `repos/<repo>/topics/names.json` | Phase 4b | トピックの名前、名前の出どころ、呼び出しの統計 |
| `repos/<repo>/topics/named_communities.json` | Phase 4b | `names.json` が名前を付けたトピックの `communities.json`。次の Phase 4b は、これと対応付けて名前を引き継ぐ |
| `repos/<repo>/topics/<topic_id>.md`、`topics/pages.json`、`topics/metrics.json` | Phase 5 | トピック別のページ（末尾に識別子の確認）、ページの再利用のキーと確認の結果、確認の合計と構造の指標 |
| `repos/<repo>/topics/topic_tree.json` | Phase 6 | トピックとサブトピックの木。トピックごとのファイルと `class_ids` |
| `repos/<repo>/topics/stats.json` | Phase 4b〜5 | トピックの名前とページの呼び出し回数、トークン数、秒数 |
| `repos/<repo>/topic_comparison.json`、`topic_comparison.md` | `compare-topics` | 比較の全指標と英語の報告。`--out` で場所を変えられる |
| `repos/<repo>/embeddings/cache.sqlite` | Phase 10 | 埋め込みのキャッシュ。Spanner に書く実行だけが使う |
| `repos/<repo>/index.md`、`reasoning_index.json`、`metadata.json` | Phase 6 | プロジェクト概要、要約ツリーとトピックツリーの機械可読版、生成日時と統計 |
| `repos/<repo>/timing_report.json` | 各実行の終了時 | フェーズ別の所要時間と件数 |
| `repos/<repo>/error_log.txt` | 各フェーズ | 失敗したファイル・トピック・バッチの記録（追記） |
| `go/bin/gohelper-<Go の版>-<ハッシュ>` | 初回 | ビルドした gohelper |
| `go/modcache/` | Phase 1.6、ダウンロード | 分離したモジュールキャッシュ。ユーザーのキャッシュとは別 |
| `go/gocache/`、`go/gopath/`、`go/tmp/`、`go/xdg/` | go コマンド | ビルドキャッシュ、一時ファイル、telemetry を切った設定 |
| `crossref_report.json` | `crossref` | 統計、エッジにしなかった参照の内訳、照合しなかったモジュールキャッシュの相手 |
| `crossref_rows.json` | `crossref`（`--dry-run` 以外） | 前回書いたリポジトリ間エッジの主キーと書き込み先。次の `crossref` が、導かなくなったエッジを消すのに使う |
| `eval_<fixture>/`、`eval_<fixture>_env/` | `evaluate` | フィクスチャごとの作業ディレクトリ。毎回作り直す |
| `trial/<repo>/` | `trial` | オフラインとダウンロードの各回、オラクル、`summary.json`、`summary.md`。`OUTPUT_DIR` は対象リポジトリの外に置く |

## 鮮度とレジューム

中断したら同じコマンドをもう一度実行します。ソースを編集した後も同じです。グラフ側は内容のハッシュで変更を見つけ、古い位置情報と新しい解決結果を組み合わせません。

| フェーズ | 再開の単位 | やり直したいときに消すもの |
|---|---|---|
| 1 | なし。毎回スキャンし、全ファイルの SHA-256 を取り、`units.json` を書き直す | なし |
| 1.5 | `entities.json`（リポジトリ単位）。全ファイルについて、ハッシュ、抽出器と gohelper の版、主ビルド構成、所有する go.mod とモジュールパスが同じなら、全体を再利用する。1 つでも違えば全ファイルを抽出し直す。消えたファイルは落とす | `entities.json` |
| 1.6 | なし。`resolutions.json` は毎回作り直す。gohelper が読んだファイルのハッシュが `entities.json` と違うサイトは採らない。実行中に変わったファイルのサイトは `unresolved`（理由 `stale`） | なし |
| 4a | なし。`--graph` のたびに計算する（同じ入力なら同じメンバー）。前回の `topics/communities.json` は名前の引き継ぎにだけ使う | なし |
| 8〜9 | `graph_checkpoint.json`（フェーズ単位）。ID 方式、ファイルのハッシュ、`resolutions.json`、コミュニティ、Spanner の書き込み先のどれかが変われば Phase 8 から書き直す | `graph_checkpoint.json` |
| 2 | `summaries/nodes/<relpath>.json`（ファイル単位）。ファイルの SHA-256、グラフの文脈のハッシュ、プロンプトの版、モデルが同じなら再利用する | 該当の `.json` |
| 3 | `summaries/packages.json`、`summaries/dirs.json`。子の要約のハッシュが同じなら再利用する | 該当のファイル |
| 4b | `topics/names.json`。ID が同じトピックと、前回の Phase 4b が名前を付けたトピック（`topics/named_communities.json`）に Jaccard 係数 0.6 以上で対応付いたトピックは、名前を引き継ぐ。間に `--graph` が何回走ってもよい。ほかを Gemini に頼む | `topics/names.json` |
| 5 | `topics/pages.json`（ページ単位）。プロンプトとモデルが同じならページを再利用する。メンバーが変わったトピックは書き直す | `topics/pages.json` か該当の `.md` |
| 6 | なし。毎回書く | なし |
| 10 | `embeddings/cache.sqlite`（行単位）。埋め込む文字列、埋め込みモデル、次元数が同じならベクトルを再利用する | `embeddings/cache.sqlite` |

- `--wiki` は最初にスキャンし直し、`graph_checkpoint.json` のハッシュと比べます。違うファイルがあれば、変わったファイルの一覧と `--graph` を実行するコマンドを出して止まります（終了コード 4）。ほかに、終わった `--graph` が無い、ID 方式が違う、`--no-spanner` で作ったグラフに Spanner 書き込みで `--wiki` を走らせた、Spanner の書き込み先が違う、最後の `--graph` でトピックを作れなかった（Phase 4a が止まった）、`topics/communities.json` がチェックポイントと違う、`graph_rows/` から同じ射影を作れない、のときも止まります。
- `--wiki` は始める前に Gemini の呼び出し回数の見積もりを出します。小さな編集のあとの `--graph` と `--wiki` は、変わったファイルとその上のディレクトリ、根拠（メンバー、メンバーの要約、隣のトピックの名前）の変わったトピックの分だけ呼び出します。
- クリーンに作り直すときは `OUTPUT_DIR/repos/<repo>/` を丸ごと消します。
- Spanner への書き込みは upsert です。再投入では、前回書いた行のうち今回書かなかった行を消します。Phase 8 は前回の `graph_rows/*.jsonl` を消す前に、その主キーを `OUTPUT_DIR/repos/<repo>/graph_prune.json` に書き込み先ごとに記録します。Phase 9 は upsert の後で、今回書かなかったキーの行を消し（エッジ、消えたファイルの `FileBelongsToTopic`、ノードの順）、`[Phase 9] Removed N stale rows` を出して記録を消します。途中で止まった実行の記録は、同じ書き込み先への次の実行が引き継ぎます。消すのは今の `ID_PREFIX` の ID だけなので、同じデータベースに別の `ID_PREFIX` で並べたグラフの行には触れません。`GRAPH_NAME` だけが違うグラフは、同じ行を共有します。
- 記録するのは、`graph_meta.json` が同じリポジトリと同じ書き込み先（`SPANNER_INSTANCE` `SPANNER_DATABASE` `GRAPH_NAME` `ID_PREFIX`）を指し、書き込み先にそのリポジトリの行があるときだけです。別の書き込み先に書いた行と、`graph_rows/` を消したあとに再投入したときの前回の行は残ります。それも消したいときは `setup_spanner_graph --destroy` でデータベースを作り直すか、`GRAPH_NAME` と `ID_PREFIX` を変えて新しいグラフに投入します。
- `--wiki` のあとに `--graph` をやり直しても、Spanner の要約は doc コメントに戻りません。Phase 8 は行を doc コメントで書き直したあと、内容（SHA-256）が同じファイルのノードと、子の要約が変わらないパッケージ・ディレクトリに、要約キャッシュの要約を書き戻します。Gemini は呼びません。`graph_rows/*.jsonl` は doc コメントのままです。Phase 2 がそれを doc コメントとして読むためです。
- トピックの 5 つの表は、記録に関係なく、毎回そのリポジトリと `ID_PREFIX` の行を消してから入れ直します。書き込みが途中で止まったときは、次の実行で直ります。
- `--wiki` のあとに `--graph` をやり直しても、ID の変わらないトピック（primary のメンバーが同じトピック）の名前と要約は戻りません。`--graph` は `topics/names.json` の名前（ハブの名前に戻した名前を含む）と `topics/<id>.md` のページを書き戻し、Spanner に書くときは埋め込みのキャッシュからベクトルも戻します。Gemini も埋め込みの API も呼びません。メンバーの変わったトピックは、次の `--wiki` までハブの名前です（[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#104-idハブ名前の引き継ぎ)）。

## webapp と MCP サーバーをローカルで動かす

3 つの入口はどれも同じ `graph_query_agent`（Spanner に GQL を投げる ADK エージェント）を使います。エージェントはルートの `.env` から `GEMINI_MODEL` `SPANNER_INSTANCE` `SPANNER_DATABASE` `GRAPH_NAME` `GOOGLE_CLOUD_PROJECT` `GOOGLE_GENAI_USE_VERTEXAI` を読みます。

```bash
adk run graph_query_agent                              # ADK の REPL

uv pip install --python .venv/bin/python -r mcp_server/requirements.txt   # fastmcp はルートの requirements.txt に入っていない
python -m mcp_server                                   # MCP サーバー。http://127.0.0.1:8080/mcp（HOST / PORT で変更）

WEBAPP_REPO=<repo> uvicorn webapp.main:app --reload --port 8000   # FastAPI
cd webapp/frontend && npm install && npm run dev       # Vite 開発サーバー http://localhost:5173（/api を 8000 へ中継）
cd webapp/frontend && npm run build                    # 本番ビルド。以後は FastAPI が dist/ を配信
```

- webapp は `OUTPUT_DIR/repos/<repo>/` の Wiki を表示します。`OUTPUT_DIR` はパイプラインと同じルートのままにし、表示するリポジトリを `WEBAPP_REPO=<repo>` か URL の `?repo=<repo>` で選びます。リポジトリが 1 つだけなら指定は要りません。
- MCP のツールは `ask_codebase(question, session_id?)` ほか、セッション操作が 4 つです。
- Cloud Run へのデプロイは [mcp_server/README.md](mcp_server/README.md) を参照してください。`mcp_server/deploy.sh` の既定はサービス `codedoc-go-leiden-mcp`、イメージ `codedoc-go-leiden-mcp`（Artifact Registry のリポジトリ `codedoc`）、データベース `codedoc-go-leiden`、実行サービスアカウント `codedoc-mcp-sa` です。codedoc_go の既定（`codedoc-go-mcp`、`codedoc-go`）とは別の名前なので、片方を配置してももう片方を置き換えません。

### グラフエクスプローラー

webapp のサイドバーの「グラフ」（`/graph`）は、コードグラフを図で見るページです。読むのは `OUTPUT_DIR/repos/<repo>/graph_rows/*.jsonl` だけで、Spanner と Gemini は使いません。トピックの表も `--graph` が書くので、`--no-spanner` の `--graph` だけでトピック図まで開けます。要約は Wiki の要約キャッシュ（`summaries/`）から出します。

| 表示 | 内容 |
|---|---|
| 近傍 | 検索で選んだノードと、エッジでつながるノード（深さ 1 か 2）。ノードのダブルクリックか詳細パネルの「展開」で広げる |
| パッケージ | パッケージ間の import。線の太さは import するファイルの数、色はモジュール |
| ディレクトリ | ファイルを持つディレクトリの木。詳細パネルの「この下の木を表示」か URL の `?root=<ディレクトリ>` で、そのディレクトリから下を描く。上限で描けなかった子の数は「+N」で出す |
| トピック | レベル 1 のトピックの枠の中に、レベル 2 のサブトピック（`TopicContainsTopic`）を置く。`TopicDependsOn` は `weight` を線の太さにする。各トピックの円グラフは `ClassBelongsToTopic` の役割の内訳。「未分類」は右に離して置く |

- 1 回の応答は 300 ノードまでです。超えた分は表ごとの件数だけを出します。グラフ全体は描きません。
- エッジの表ごとの表示切り替え、テストと生成コードの非表示、2 ノード間の最短経路、PNG 保存があります。
- 詳細パネルは qualname、場所、モジュール、要約、表ごとのエッジ数を出し、Wiki の要約ページとトピックページ（サブトピックのページを含む）へリンクします。
- トピックのページ `/topic/<名前か ID>` はサブトピックのページも開きます。名前が親トピックと同じサブトピックは ID で開きます。
- API は `/api/graph/stats`、`/api/graph/search`、`/api/graph/node/<id>`、`/api/graph/neighbors/<id>`、`/api/graph/path`、`/api/graph/overview/<packages|directories|topics>` です。`graph_rows/` のファイルが変わると読み直します。
- etcd の `graph_rows/`（トピックの表を含めて約 7.9 万行）は、初回の読み込みが約 0.6 秒、以後の応答が 51 ミリ秒以内でした（2026-10-14 計測）。

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `no Go toolchain found` で止まる | `GO_TOOLCHAIN` が未設定で、`.tools/go` も `PATH` の `go` も無い | `python -m graph_generator setup go` を実行するか、`GO_TOOLCHAIN` に `go` のパスを書く |
| `GO_TOOLCHAIN=... is not a go binary` | パスが `go` の実行ファイル（か GOROOT）を指していない | `<GOROOT>/bin/go` を指す |
| ツールチェーンが `older than go1.26.0` と言われる | gohelper の下限より古い Go | `setup go` か、新しい Go を `GO_TOOLCHAIN` で指定する |
| `building the helper failed` | gohelper のソースかツールチェーンの問題。出力にコンパイラのエラーが出る | 出力を確かめる。`gohelper/vendor/` が揃っているか確かめる |
| グループが status `toolchain` になり、`"go 1.N" is newer than the toolchain` と警告が出る | 対象の `go` 行がツールチェーンより新しい | 警告の行が求める版以上の `go` を `GO_TOOLCHAIN` で指定する |
| `"toolchain go1.N.P" is newer than the toolchain` と警告が出る | `toolchain` 行だけが新しい | 解決は進む。標準ライブラリの呼び出し先を揃えたいときは、その版の `go` を `GO_TOOLCHAIN` で指定する |
| `dependency <module>@<version> says "go 1.N"` と警告が出る | 依存モジュールの `go` 行が新しい。読み込みはグループごと失敗する | 新しい `go` を `GO_TOOLCHAIN` で指定する |
| `required modules are missing offline` と警告が出る | ユーザーのモジュールキャッシュに無い | `--download-modules` を付ける。または `GO_USER_MODCACHE` で別のキャッシュを指す |
| ダウンロードで一部のモジュールが失敗する | プロキシに無い、非公開、ネットワークの問題 | 報告されたモジュールの呼び出しは `unresolved` のまま（ワークスペースのグループでは外部のパッケージ全体）。必要ならユーザーのキャッシュに入れてから `GO_USER_MODCACHE` で指す |
| グループが status `error` で、`go: inconsistent vendoring` と警告が出る | `vendor/modules.txt` が go.mod と合わない。go コマンドがビルドを拒む | 対象のリポジトリで `go mod vendor`（ワークスペースなら `go work vendor`）を実行する |
| グループが `degraded` と警告される | 生成した go.work がリポジトリのものと違う（存在しない `use` を外した、`go` 行を上げた） | 警告の内容を確かめる。開発者の手元でもビルドできない構成なので、リポジトリ側を直す |
| `go.mod lacks requirements for: ...` と警告が出る | ダウンロードモードで、リポジトリの go.mod に無い require が要ると分かった。開発者の readonly のビルドも同じところで失敗する | 対象のリポジトリで `go mod tidy` を実行する。それまで、警告に出たパッケージの呼び出しは `unresolved` のまま |
| `... lists <dir>, but the go command only uses the go.work found walking up from a module` | そのモジュールの上にない go.work が `use` している | 正常動作。go コマンドと同じく、上にたどって最初に見つかる go.work だけがグループを決める（[docs/CROSS_PROJECT.md](docs/CROSS_PROJECT.md)） |
| 事前確認の `leiden backend` が `error` | graspologic-native か networkx を import できない | `uv pip install --python .venv/bin/python -r requirements.txt` を実行する |
| 事前確認の `leiden backend` が `warn` | graspologic-native の版が固定した 1.3.1 と違う | `requirements.txt` の版を入れ直す |
| 事前確認の `topic settings` が `error` | `TOPIC_*` か `SUBTOPIC_MIN_ATOMS` の値が正しくない（整数でない、種が重なる、帯の下限が上限より大きい、プロファイルの項目が無い）。行がキー、値、書ける形を示す | `.env` の値を直すか、消して既定に戻す |
| 起動の直後に `ValueError: invalid literal for int()` で止まる | 整数の設定（`GEMINI_CONCURRENCY`、`GO_HELPER_TIMEOUT`、`WIKI_FILE_MAX_CHARS` など）に整数でない値がある | `.env` の値を整数に直す |
| `[Phase 4a] Stopped: ...` が出る | Leiden 法の実装か整合性の検査が失敗した。グラフはトピック無しで書かれ、`--wiki` は止まる | `error_log.txt` を見て原因を直し、`--graph` を実行し直す |
| `[Phase 4a] warning: N topics hold files of a group whose resolution failed` | 読み込みに失敗したグループのファイルを持つトピックがある（`communities.json` で `partial`） | グループの警告を直してから `--graph` を実行し直す |
| `[Phase 4a] warning: modularity check: ...` | 実装が返したモジュラリティと自前の計算が合わない | 試行は実装の値で選んでいる。graspologic-native の版を確かめる |
| `gohelper` の実行がタイムアウトする | 大きなリポジトリで `GO_HELPER_TIMEOUT` 内に終わらない | `GO_HELPER_TIMEOUT` を上げる |
| メモリが足りない | gohelper はグループの全パッケージを型付きで読み込む（2026-10-13 の計測で etcd 1.5〜1.7 GB、Prometheus 4.7〜5.3 GB） | 空きメモリのあるマシンで実行する。グループの数を減らすことはできない |
| `--wiki cannot run: ... run --graph first` | 終わった `--graph` が無い、ID 方式が違う、`graph_rows/` が無い | 表示されたコマンドで `--graph` を実行する |
| `--wiki cannot run: The last --graph run of repo ... has no Leiden communities` | 最後の `--graph` で Phase 4a が止まった | `error_log.txt` を見て原因を直し、`--graph` を実行し直す |
| `--wiki cannot run: ... communities.json does not match graph_checkpoint.json` か `graph_rows/ do not give the projection ...` | `--graph` の後に `topics/` か `graph_rows/` が変わった | `--graph` を実行し直す |
| `--wiki cannot run: N files differ from the graph` | `--graph` のあとにソースを編集した | 一覧のファイルを確かめ、`--graph`（か `--all`）を実行し直す |
| `--wiki cannot run: The graph was built with --no-spanner` | ローカルにだけ作ったグラフに、Spanner へ書く `--wiki` を走らせた | `--wiki --no-spanner` で走らせるか、`--no-spanner` なしで `--graph` を作り直す |
| `--wiki cannot run: The graph was written to Spanner ...` | `.env` の書き込み先がグラフを作ったときと違う | `SPANNER_INSTANCE` `SPANNER_DATABASE` `GRAPH_NAME` `ID_PREFIX` を戻すか、今の設定で `--graph` を作り直す |
| Gemini の 429 が続く、`[Throttle] Circuit breaker OPEN` が出る | 同時リクエストがクォータを超えている | `GEMINI_CONCURRENCY` を 10〜30 に下げる。失敗分は同じ実行内で再試行され、残りは次回に作られる |
| `empty model response ... consider raising MAX_OUTPUT_TOKENS` | thinking が出力枠を使い切った | `MAX_OUTPUT_TOKENS` を上げる |
| `N failed — see error_log.txt` | 個別のファイル・トピックの生成に失敗した | `error_log.txt` を確認し、同じコマンドを再実行する |
| `[Resume] graph_checkpoint.json uses another ID scheme` | 古い ID 方式のグラフのチェックポイント | 正常動作。Phase 8 から作り直し、旧 ID の行は Phase 9 の後に消える（前回の `graph_rows/` が残り、書き込み先が同じとき。[鮮度とレジューム](#鮮度とレジューム)） |
| `unresolved` の理由に `stale` がある | 解析中にファイルを編集した | 編集を終えてから再実行する |
| `compare-topics refused: the LLM and Leiden runs did not see the same input` | 2 つの実行のファイル、ビルド構成、モード、`resolutions.json` のどれかが違う | 同じコミットを、同じ設定で両方の版に処理させる |
| `compare-topics refused: ... is missing or unreadable` か `... lacks entities.json` | どちらかの版の `analyze` が終わっていない | 表示された版で `analyze --all` を実行する |
| `compare-topics refused: --llm2 ... is not a second run on the same input`（`--leiden2` も同じ） | 2 回目の実行のファイル、ビルド構成、モード、`resolutions.json` のどれか、`--leiden2` ならトピックの設定が 1 回目と違う | 同じコミットを同じ設定でもう一度処理させる |
| `compare-topics refused: ... has no finished --graph run` か `... not from the --graph run graph_checkpoint.json records` | この版の `--graph` が途中で止まったか、そのあとに `communities.json`、`resolutions.json`、`entities.json` が変わった | この版で `analyze --graph`（か `--all`）をやり直す |
| `crossref` が `no ingested repos under ...` を出す | `repos/` に投入済みのリポジトリが無い | 各リポジトリを `analyze` で投入してから実行する |
| `crossref` が `only one ingested repo` を出す、またはエッジを 1 本も書かない | 投入済みのリポジトリが 1 つだけか、ピアを渡さずに投入したので、リポジトリ間の相手が記録されていない | `crossref_report.json` を見る。依存先のリポジトリを投入し、[ピア](#ピアリポジトリ)として渡して再投入する |
| `crossref` が `deleted N <table> edges it no longer derives` を出す | 前回同じ書き込み先に書いたリポジトリ間エッジのうち、今回は導かなかったもの | 正常動作 |
| 事前確認の `spanner` が `columns missing` か `columns of another type` で止まる | データベースのスキーマが今の定義より古い | 足りないだけなら `setup spanner`（か `setup_spanner_graph --migrate`）を実行する。型が違うときは Spanner では変えられないので、`setup_spanner_graph --destroy` で消してから `setup spanner` を実行する。または `--no-spanner` を付ける |
| `crossref` が `skipping <repo>` を出す | 古い版か、別の `ID_PREFIX` で投入したリポジトリ | 今の設定でそのリポジトリを投入し直す |
| `crossref_report.json` に落とした参照が並ぶ | 持ち主が無い、持ち主が 2 つ、ノードが無い、未投入、解析中に変わったファイル（`stale`） | [docs/CROSS_PROJECT.md](docs/CROSS_PROJECT.md) の理由別の対処を見る |
| `validate` の `Nodes by repo` に意図しないリポジトリ名がある | `--repo-name` 省略でディレクトリ名が衝突した | `--repo-name` を明示して再投入する |
| `USER_PROJECT_DENIED`（Spanner と Gemini の呼び出しがすべて失敗し、`gcloud` 自体は動く。`doctor` の spanner 行にも出る） | ADC ファイルの quota project が削除されたか、そのプロジェクトで `serviceusage.services.use` の権限が無い | `gcloud auth application-default set-quota-project <project>` を実行するか、`GOOGLE_CLOUD_QUOTA_PROJECT=<project>` を設定する |
| チャットや MCP が `NOT_FOUND: Instance not found` を返す | エージェントが別のインスタンス・プロジェクトを見ている | 起動時の `[graph_query_agent] ... project:` の行と、最初の問い合わせで出る `Spanner project:` の行（ADC / gcloud から決まったプロジェクトには `(ADC / gcloud default)` が付く）を確認する。違えばルートの `.env` の `GOOGLE_CLOUD_PROJECT` `SPANNER_INSTANCE` `SPANNER_DATABASE` `GRAPH_NAME` を直す。`graph_query_agent/.env` があれば消す |
| チャットが API キーを要求する | `GOOGLE_GENAI_USE_VERTEXAI` が未設定 | `.env` に `GOOGLE_GENAI_USE_VERTEXAI=true` を入れる |
| webapp が `repo ... not found` か `no repo selected` を返す | `OUTPUT_DIR/repos/` に複数のリポジトリがあり、どれを出すか決まらない | `WEBAPP_REPO=<repo>` で起動するか、`?repo=<repo>` を付ける |

## リポジトリ構成

```
codedoc_go_leiden/
├── README.md                      # 本書
├── CLAUDE.md                      # コーディングエージェント向けの作業ガイド（英語）
├── docs/                          # ARCHITECTURE / LSP_RESOLUTION / GRAPH_SCHEMA / CROSS_PROJECT
├── .env.example                   # 設定テンプレート
├── requirements.txt               # 依存パッケージ（全固定）
├── pytest.ini                     # tests/ の設定（カバレッジ 85% ゲート、go と network のマーカー）
├── Dockerfile                     # webapp のコンテナイメージ
├── gohelper/                      # Go のヘルパー（extract、resolve、version）。golang.org/x/tools を vendor
├── graph_generator/               # パイプライン本体
│   ├── __main__.py                #   CLI
│   ├── config.py                  #   .env の読み込みと定数
│   ├── pipeline.py                #   フェーズ、鮮度、チェックポイント、事前確認、Spanner 書き込み
│   ├── go_model.py                #   共有のデータ型と語彙
│   ├── go_units.py                #   スキャン、go.mod / go.work / modules.txt、ビルド制約、グループ、制御ディレクトリ
│   ├── go_toolchain.py            #   ツールチェーン、環境変数、gohelper のビルド、setup go
│   ├── go_modcache.py             #   モジュールの入手先とダウンロード
│   ├── go_extract.py              #   Phase 1.5: gohelper extract と entities.json
│   ├── resolution.py              #   Phase 1.6 の取りまとめと resolutions.json
│   ├── go_classify.py             #   位置の分類と宣言の対応付け
│   ├── go_gates.py                #   ゲート
│   ├── graph_rows.py              #   ノード行とエッジ行の導出
│   ├── node_summaries.py          #   Phase 2: ノード要約とキャッシュ
│   ├── prompts.py                 #   Gemini 用プロンプト（日本語）
│   ├── crossref.py                #   crossref: リポジトリ間エッジ
│   ├── evaluate.py                #   evaluate: フィクスチャ評価
│   ├── trial.py                   #   trial: 実リポジトリでの試験
│   ├── topics/                    #   Leiden トピック（射影、Leiden 法、名前、ページ、比較の指標）
│   ├── topics_phase.py            #   トピックのフェーズとパイプラインのつなぎ、compare-topics
│   ├── topics_schema.py           #   トピックの列と表の追加
│   └── setup_spanner_graph.py     #   スキーマ定義（NODE_SPEC / EDGE_SPEC）、DDL、マイグレーション、destroy
├── graph_query_agent/             # ADK エージェント（agent.py、config.py）
├── mcp_server/                    # FastMCP サーバー（専用の requirements.txt）、Cloud Run デプロイ
├── webapp/                        # FastAPI バックエンドと Vite/React フロントエンド
├── scripts/                       # ドキュメントの検査（doccheck）、GQL 例の検査、検証表の行の作成、codedoc_go からの同期
├── test_codes/                    # 評価フィクスチャ（iryo-kanri-system / go_layouts / multi_repo）、合成プロキシ、正解データの道具
├── tests/                         # pytest（GCP と gohelper をフェイクに差し替えて実行）
├── sample_prompt.txt              # 質問例
└── output_docs_pipeline/          # OUTPUT_DIR の既定値
```
