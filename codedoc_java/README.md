# CodeDoc — Java リポジトリの日本語 Wiki とコードグラフを生成する

CodeDoc は Java (Maven) のソースツリーを読み、Gemini で日本語の Wiki ドキュメントを生成し、Cloud Spanner にコードグラフ (ファイル・クラス・メソッド・呼び出し・継承) を書き込むパイプラインです。呼び出しエッジは Eclipse JDT Language Server (jdtls) がコンパイラの意味論で解決した呼び出しだけから作り、継承エッジは jdtls で同定した型宣言に基底名が一意に一致する場合だけ作ります。呼び出し先を名前で推測することはしません。生成したグラフには Google ADK のエージェント (`graph_query_agent`) が GQL で問い合わせ、webapp と MCP サーバーから利用できます。

この README は導入・設定・実行・運用の手引きです。仕組みや設計理由は `docs/` に分けています。

| ドキュメント | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 構成要素とデータの流れ。フェーズ表、実行モード、チェックポイント設計、クエリスタック |
| [docs/LSP_RESOLUTION.md](docs/LSP_RESOLUTION.md) | Phase 1.6。jdtls で呼び出し先・継承先を厳密に解決する仕組みと、誤エッジを出さないためのゲート |
| [docs/GRAPH_SCHEMA.md](docs/GRAPH_SCHEMA.md) | Spanner に入るテーブル、ID の作り方、ソースから行への対応、GQL の書き方 |
| [docs/CROSS_PROJECT.md](docs/CROSS_PROJECT.md) | 複数の Maven アーティファクトに同名シンボルがあるときの識別方法と、その検証手順 |

## 前提条件

| 項目 | 要件 |
|---|---|
| Python | 3.12 以上 (`.python-version` は 3.14、開発 venv は 3.13、Dockerfile は 3.12)。依存は `requirements.txt` にピン留め |
| Google Cloud | プロジェクト作成済み。Vertex AI API (`aiplatform.googleapis.com`) と Cloud Spanner API (`spanner.googleapis.com`) 有効。`gcloud auth application-default login` 済み |
| Cloud Spanner | Enterprise エディションのインスタンス。`setup spanner` が 100 PU で作成する (既定リージョン `asia-northeast1`) |
| JDK 21 | jdtls の実行に必須。既定の場所は `/usr/lib/jvm/java-21-openjdk-amd64`。別の場所なら `JAVA21_HOME` で指定 |
| Maven 3.9 以上 | 解析対象の各プロジェクトに `pom.xml` があり、依存ライブラリが `mvn install` (または deploy) 済みであること |
| jdtls | Phase 1.6 用。スナップショット tarball を展開して使う。動作確認は 1.61.0 |
| Node.js | webapp のフロントエンドを起動する場合のみ (Vite 8 / React 19) |

jdtls の導入例:

```bash
curl -LO https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz
mkdir -p ~/tools/jdt-language-server
tar xzf jdt-language-server-latest.tar.gz -C ~/tools/jdt-language-server
ls ~/tools/jdt-language-server/bin/jdtls
```

パイプラインは `JAVA_LSP_CMD` (既定 `jdtls`) をシェルを介さずに起動します。PATH に無い場合は `.env` に絶対パスで書きます。`~` は展開されません。起動時の `JAVA_HOME` には `JAVA21_HOME` が入るため、システム既定の `java` が 21 未満でも構いません。

## セットアップ

```bash
git clone <repository-url>
cd codedoc_java
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 設定ファイル。対話生成するか、テンプレートをコピーして編集する
python -m graph_generator init          # リポジトリルートで実行する
cp .env.example .env                    # 手動で作る場合

# Spanner のインスタンス・データベース・テーブル・プロパティグラフを作成
python -m graph_generator setup spanner
python -m graph_generator setup spanner --skip-instance   # 既存インスタンスを使う場合
```

`.env` はリポジトリルートの 1 つだけです。`graph_generator` / `graph_query_agent` / `webapp` / `mcp_server` の各 `config.py` が同じファイルを読みます。`init` は `GOOGLE_CLOUD_PROJECT`、`GOOGLE_GENAI_USE_VERTEXAI=true`、`SPANNER_INSTANCE`、`SPANNER_DATABASE` だけを書きます。他のキーは既定値のままです。

パイプラインはカレントディレクトリ直下の `OUTPUT_DIR` に書き出します。コマンドはリポジトリルートで実行してください。

## 設定リファレンス

既定値は `graph_generator/config.py` の値です。`.env.example` が別の値を置いている場合は併記します。コード内の変数名が環境変数名と違うものは 3 つです (`GOOGLE_CLOUD_PROJECT` → `GCP_PROJECT`、`GOOGLE_CLOUD_LOCATION` → `GCP_REGION`、`GEMINI_MODEL` → `MODEL`)。

### Google Cloud

| キー | 既定値 | 変わること |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | `development-459201` (テンプレートは `your-project-id`) | Gemini、Spanner、埋め込みを呼ぶプロジェクト。`init` が必ず書く |
| `GOOGLE_CLOUD_LOCATION` | `global` | Gemini クライアントのロケーション。埋め込み (Phase 10) は `us-central1` 固定 |
| `GOOGLE_GENAI_USE_VERTEXAI` | (テンプレート `true`) | ADK エージェント (webapp / MCP / `adk run`) が Vertex AI 経由で Gemini を呼ぶための指定。無いと API キーを要求される。パイプライン本体は常に Vertex AI を使う |
| `GCE_METADATA_MTLS_MODE` | (テンプレート `none`) | GCE 上でメタデータサーバーの mTLS 接続に失敗する場合の回避 |

### Gemini

| キー | 既定値 | 変わること |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.5-flash` (テンプレートは `gemini-3.7-flash`) | Phase 2〜5 の生成と `graph_query_agent` の両方のモデル |
| `GEMINI_CONCURRENCY` | `100` | Gemini 同時呼び出し数 (Semaphore)。レート制限が頻発するなら 10〜30 に下げる |
| `MAX_OUTPUT_TOKENS` | `8192` | 1 回の生成の出力上限。思考トークンもここから消費されるため、低いと空応答になる |

### Spanner

| キー | 既定値 | 変わること |
|---|---|---|
| `SPANNER_INSTANCE` | `codedoc` | 書き込み先・問い合わせ先インスタンス |
| `SPANNER_DATABASE` | `codedoc-java` | 書き込み先・問い合わせ先データベース |
| `GRAPH_NAME` | `code_graph_a` | `graph_query_agent` が GQL の先頭に付けるグラフ名。`setup spanner` が作るグラフ名は `code_graph_a` 固定 (`spanner_setup.py`) |
| `ID_PREFIX` | `a` | ノード ID とエッジ ID の接頭辞。同じ DB に別プロジェクトを入れるときに分ける |
| `SPANNER_BATCH_SIZE` | `5000` (固定) | 1 バッチの行数。環境変数では変えられない |

### Java LSP (Phase 1.6)

| キー | 既定値 | 変わること |
|---|---|---|
| `JAVA_LSP_ENABLED` | `true` | `false` にすると Phase 1.6 を行わず、ノードだけを生成する (呼び出し・継承エッジなし) |
| `JAVA_LSP_CMD` | `jdtls` | jdtls 起動スクリプト。PATH に無ければ絶対パス |
| `JAVA_LSP_CONCURRENCY` | `4` | 同時に起動する jdtls セッション数。1 セッションあたりアイドル時 RSS 約 1.2 GB |
| `JAVA_LSP_TIMEOUT_S` | `300` | プロジェクトのロード完了を待つ上限 (秒) |
| `JAVA_LSP_MAVEN_SETTINGS` | (空) | m2e に渡す `settings.xml`。空なら利用者の `~/.m2` を使う |
| `JAVA21_HOME` | `/usr/lib/jvm/java-21-openjdk-amd64` | jdtls 起動時の `JAVA_HOME`。`resolution.py` が読む |

### 出力・埋め込み・スキャン

| キー | 既定値 | 変わること |
|---|---|---|
| `OUTPUT_DIR` | `output_docs_pipeline` | 生成物の出力先。webapp も同じ値を読む |
| `EMBED_CONCURRENCY` | `20` | 埋め込み生成の同時バッチ数 |
| `EMBED_MODEL` | `text-embedding-005` (固定) | 埋め込みモデル |
| `EMBED_BATCH_SIZE` | `20` (固定) | 1 回の埋め込み API 呼び出しに載せる件数 |
| `SOURCE_EXTENSIONS` | `.java .jsp .kt .groovy .scala .gradle` (固定) | スキャン対象。構文解析と Phase 1.6 の対象は `.java` のみ。他の拡張子は Files ノードと LLM 要約だけで、Classes / Methods にはならない |
| `SKIP_DIRS` | `.git .svn .hg .idea .vscode build dist target out .gradle .mvn node_modules generated` (固定) | 走査しないディレクトリ。`.` で始まるディレクトリも常に除外 |

### サーバー (webapp / MCP)

| キー | 既定値 | 変わること |
|---|---|---|
| `HOST` | `0.0.0.0` | MCP サーバーの待受アドレス |
| `PORT` | `8080` | MCP サーバーの待受ポート |

## CLI リファレンス

すべて `python -m graph_generator <command>` の形です。

| コマンド | 引数・フラグ | 実行内容 |
|---|---|---|
| `init` | `--force` (既存 `.env` を上書き) | 対話で `.env` を生成 |
| `setup spanner` | `--instance` `--database` `--region` (既定 `asia-northeast1`) `--skip-instance` | インスタンス (Enterprise, 100 PU) とデータベース (13 テーブル + グラフ `code_graph_a`) を作成。DB が既にあればグラフ定義だけ再作成する |
| `analyze <target_dir>` | | Phase 1〜6 (Wiki) の後、`pipeline_data.pkl` を経由して Phase 1.6, 8〜10 (グラフ) を実行 |
| `generate wiki <target_dir>` | | Phase 1〜6 のみ。終了時に `pipeline_data.pkl` を保存 |
| `generate graph <target_dir>` | | Phase 1.6, 8〜10 のみ。同じ対象の `pipeline_data.pkl` があれば読み、無ければ Phase 1 + 1.5 を実行して既存の要約を読み込む |
| `upload graph <target_dir>` | | `generate graph` の別名 |
| `validate` | | 12 テーブルの行数と孤立エッジ 3 種 (FileDependsOn / ClassInherits / MethodCalls) を確認 |
| `score <corpus_dir>` | `--manifest` `--resolution` | `resolution.json` を正解マニフェストと照合し 4 ゲートを判定。終了コード 0 合格 / 1 不合格 / 2 入力ファイル無し |

`python -m graph_generator.spanner_setup --verify` でテーブルとグラフの存在確認、`--destroy` でインスタンスごと削除できます (確認入力あり)。この直接呼び出しは `.env` のプロジェクト・インスタンス・DB 名とリージョン `asia-northeast1` を既定とし、`--project` `--region` `--instance` `--database` `--skip-instance` で上書きできます。

### レシピ

```bash
# 1. 初回。設定 → Spanner 作成 → フル実行 → 検証
python -m graph_generator init
python -m graph_generator setup spanner
python -m graph_generator analyze /path/to/java/repo
python -m graph_generator validate

# 2. Wiki だけ欲しい
python -m graph_generator generate wiki /path/to/java/repo

# 3. Wiki は生成済みで、グラフだけ作る (2 の後に実行すると pkl を再利用する)
python -m graph_generator generate graph /path/to/java/repo

# 4. DB を切り替えた後などにグラフを再投入する (3 と同じ処理)
python -m graph_generator upload graph /path/to/java/repo

# 5. Spanner 側の整合性を見る
python -m graph_generator validate

# 6. 採点 (下の注意を読むこと)
python -m graph_generator score test_codes/iryo-kanri-system
```

`score` は現時点で受け入れゲートとして使えません。同梱コーパスの正解マニフェスト (`ground_truth/expected_resolutions.json`) が移植前の C# 版 (version 2、106 ケース、`.cs` パス、`assembly` キー) のままで、`scorer.py` が前提とする Java 版 (version 3、`artifact` + `via` = `source` / `maven` / `jdk`) になっていないためです。実行すると全件未回答で終了コード 1 になります。移植までは SemanticProbe (下記) と統合テストが検証を担います。`ground_truth/generate_manifest.py` は現行マニフェストを上書きするので実行しないでください。

### 同梱コーパスで Phase 1.6 を確認する

`test_codes/iryo-kanri-system` は 12 個の独立 Maven プロジェクト (57 `.java`) からなる医療管理システムのコーパスです。プロジェクト間依存はすべてローカルフィード `_feed/` の jar を通ります。

```bash
C=test_codes/iryo-kanri-system
rm -rf "$C/_feed" "$C/_m2"        # クリーンビルドする場合のみ
for p in iryo-kernel iryo-domain iryo-data iryo-billing iryo-pharmacy iryo-scheduling iryo-reporting \
         iryo-legacy-claims iryo-interop reception-desk admin-console patient-portal; do
  "$C/$p/build.sh" || break        # 依存される側が先。順序を変えると意図的に失敗する
done

python -m graph_generator generate graph "$C"    # resolution.json を出力し、Spanner へ書き込む
"$C/ground_truth/SemanticProbe/build.sh"          # 「SEMANTIC PROBE PASSED」が出れば成功
```

CLI でコーパス全体を指定すると `ground_truth/SemanticProbe` も 13 番目の Maven プロジェクトとして解析対象に入ります (61 ファイル)。ドキュメントに載せている実測値は製品コード 12 プロジェクトだけを対象にしたものです。

### テスト

```bash
.venv/bin/python -m pytest -q -m "not integration"   # ユニット。jdtls 不要
.venv/bin/python -m pytest -q -m integration         # 実 jdtls + ビルド済みコーパス。約 40 秒
```

統合テストは `JAVA_LSP_CMD` → PATH の `jdtls` → `/home/admin/tools/jdt-language-server/bin/jdtls` の順に探し、見つからなければ skip します。期待される件数と実測日は [docs/LSP_RESOLUTION.md](docs/LSP_RESOLUTION.md) の検証表にあります。

## 出力

`OUTPUT_DIR` (既定 `output_docs_pipeline/`) の中身:

| パス | 生成フェーズ | 用途 |
|---|---|---|
| `index.md` | 6 | Wiki の入口。モジュール一覧とコンポーネント表 (最大 200 ファイル) |
| `reasoning_index.json` | 6 | ディレクトリ木 + トピック木の構造化データ (要約は先頭 500 字) |
| `metadata.json` | 6 | 生成時刻、モデル、件数 |
| `summaries/files/*.md` | 2 | ファイル要約。名前は相対パスの `/` を `_` に置換 (`src/main/java/App.java` → `src_main_java_App.java.md`) |
| `summaries/dirs/*.md` | 3 | ディレクトリ要約 |
| `topics/topic_tree.json` | 4 | トピック (モジュール) 一覧 |
| `topics/*.md` | 5 | トピック別ドキュメント (Mermaid 図付き) |
| `entities.json` | 1.5 | tree-sitter が抽出したクラス・メソッド・import。Phase 1.5 のチェックポイント |
| `resolution.json` | 1.6 | jdtls による宣言一覧と呼び出しサイトの解決結果。構造は [docs/LSP_RESOLUTION.md](docs/LSP_RESOLUTION.md) |
| `graph_checkpoint.json` | 8〜10 | 完了フェーズと ID マップ。Phase 8〜10 のレジューム用 |
| `pipeline_data.pkl` | `generate wiki` 終了時 | `PipelineData` 全体の pickle。`generate graph` が読む |
| `timing_report.json` | 終了時 | フェーズ別所要時間と件数 |
| `error_log.txt` | 2〜5, 8〜10 | 失敗の記録 (追記モード) |

## レジュームとクリーン再実行

中断したら同じコマンドをもう一度実行します。ディスク上の生成物が読み戻され、未処理分だけが実行されます。空のファイルは再生成の対象になります。

やり直したい範囲に応じて消すもの:

| 消すもの | やり直される処理 |
|---|---|
| `summaries/files/<name>.md` | そのファイルの要約 (Phase 2) |
| `summaries/dirs/` | ディレクトリ要約 (Phase 3) |
| `topics/topic_tree.json` | トピック抽出とトピック要約 (Phase 4, 5) |
| `topics/<name>.md` | そのトピックの要約 (Phase 5) |
| `entities.json` | tree-sitter 抽出 (Phase 1.5) |
| `graph_checkpoint.json` | Spanner 書き込みと埋め込み (Phase 8〜10)。Phase 1.6 は毎回実行され、解決結果が前回から変わっていれば自動で消える |
| `pipeline_data.pkl` | `generate graph` が Phase 1 + 1.5 からやり直す |
| `error_log.txt` | 何も。ログを空にしたいときに消す |
| `OUTPUT_DIR` 全体 | すべて |

Spanner の行は `insert_or_update` で上書きされるだけで削除はされません。入力から消えたクラスの行は残ります。まっさらなグラフが必要なら新しいデータベース (または別の `ID_PREFIX`) を使います。

## DB 切替手順

```bash
# 1. .env の SPANNER_DATABASE (必要なら SPANNER_INSTANCE) を変える
# 2. 新 DB にテーブルとグラフを作る
python -m graph_generator setup spanner --skip-instance
# 3. グラフチェックポイントを消す (消さないと Phase 8〜10 がスキップされ、新 DB が空のまま)
rm output_docs_pipeline/graph_checkpoint.json
# 4. 再投入
python -m graph_generator upload graph /path/to/java/repo
```

`graph_checkpoint.json` は書き込み先 DB を記録しません。手順 3 を忘れると前回の「phase10 完了」がそのまま信じられます。`setup spanner` は既存 DB にテーブルを追加しないので、スキーマを変えたときも新しい DB を作ります。

## webapp と MCP サーバーをローカルで動かす

どちらも `graph_query_agent` を同一プロセスで動かし、Spanner のグラフに問い合わせます。`setup spanner` と `analyze` (または `upload graph`) が済んでいることが前提です。webapp の Python 依存はルートの `requirements.txt` に含まれています。`webapp/requirements.txt` は Dockerfile 用で、ルートより古いピンなのでローカルの venv には入れません。

```bash
# webapp (FastAPI)。生成済みドキュメントの閲覧とチャット
uvicorn webapp.main:app --reload --port 8000      # http://localhost:8000

# フロントエンド開発サーバー (/api を 8000 番へプロキシ)
cd webapp/frontend && npm install && npm run dev  # http://localhost:5173
npm run build                                     # dist/ ができると FastAPI が / で配信する

# MCP サーバー (streamable-http)。http://127.0.0.1:8080/mcp
pip install -r mcp_server/requirements.txt         # fastmcp はルートの requirements.txt に無い
python -m mcp_server

# エージェント単体
adk run graph_query_agent
```

webapp の主な API は `GET /api/docs/{index,tree,topics,metadata}`、`GET /api/docs/topics/{name}`、`GET /api/docs/summaries/{files|dirs}/{name}`、`POST /api/chat`、`POST /api/chat/stream` (SSE) です。MCP のツールは `ask_codebase(question, session_id?)` とセッション管理 4 種です。Cloud Run へのデプロイは [mcp_server/README.md](mcp_server/README.md) を参照してください。

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `resolution.json` の `solutions[].reason` が `FileNotFoundError: ... 'jdtls'`。全プロジェクトが `fallback` | jdtls が PATH に無い。または `~` 付きのパスを書いた | `.env` の `JAVA_LSP_CMD` に絶対パスを書く。接続系エラーは再試行されないので直してから再実行 |
| jdtls が起動直後に終了する (`LspConnectionError` / `LspTimeout`) | JDK 21 が無い。`JAVA21_HOME` の場所が違う | `ls $JAVA21_HOME/bin/java` を確認し、21 の場所を `.env` に書く |
| `reason` が `LspTimeout: project not ready within 300s` | ロードが間に合わない。初回は m2e が Maven プラグインを取得する。同時起動の競合 | 並行実行後に 1 回だけ直列で再試行される。恒常的なら `JAVA_LSP_TIMEOUT_S` を増やすか `JAVA_LSP_CONCURRENCY` を下げる。依存 jar がビルド済みか確認 |
| `unresolved` / `external` が多い。ライブラリを直したのに古いシグネチャに束縛される | 依存 jar が未ビルド。または `~/.m2` や隔離リポジトリに古い jar が残っている | 依存される側から `mvn install`。コーパスでは該当ライブラリの `build.sh` を先に実行する (`_m2` と `~/.m2` の両方から自分の成果物を退避してから deploy する)。隔離リポジトリを使うなら `JAVA_LSP_MAVEN_SETTINGS` を指定 |
| コーパスのビルドが `jp.iryo:iryo-kernel:jar:1.0.0` を解決できない | 依存される側より先にビルドした | コーパス README の順序でビルドする |
| プロジェクト直下に `.project` / `.classpath` / `.settings/` ができる | jdtls (m2e) がインポート時に書く | 対象リポジトリの `.gitignore` に入れる |
| jdtls が OOM で落ちる | 1 セッション約 1.2 GB (`-Xmx700m` はコード側で固定) | `JAVA_LSP_CONCURRENCY` を下げる |
| MethodCalls / ClassInherits が 1 本も無い | `JAVA_LSP_ENABLED=false`、`pom.xml` が無い、全プロジェクトが fallback | `resolution.json` の `lsp.enabled`、`solutions[].status`、`fallback_files` を見る |
| `empty model response ... consider raising MAX_OUTPUT_TOKENS` | 思考トークンが出力予算を使い切った | `MAX_OUTPUT_TOKENS` を増やす (例 16384) |
| `[Throttle] Circuit breaker OPEN` が繰り返し出る | Vertex AI のクォータ不足 | `GEMINI_CONCURRENCY` を 10〜30 に下げる。スロットルは自動で減速するが、頻発するなら並行数自体を下げる |
| Phase 10 で Files / Modules の埋め込みが 0 件 | 要約が無い (`generate graph` だけ実行した) | 先に `generate wiki` を実行する。Classes は宣言情報から埋め込まれる |
| 「保存済み pipeline_data.pkl は別ターゲットのものです」 | 前回と違う対象を指定した | 正常。スキャンからやり直される |
| DB を切り替えたのに新 DB が空 | `graph_checkpoint.json` が残っている | 上の DB 切替手順 3 |
| webapp / MCP で `NOT_FOUND: Instance not found` | `.env` の Spanner 名が実体と違う | `SPANNER_INSTANCE` / `SPANNER_DATABASE` を確認。無ければ `setup spanner` |
| webapp / MCP が API キーを要求する | `GOOGLE_GENAI_USE_VERTEXAI=true` が無い | `.env` に追加 |
| GCE 上で SSL / mTLS のエラー | メタデータサーバーの mTLS | `GCE_METADATA_MTLS_MODE=none` |

## リポジトリ構成

```
codedoc_java/
├── .env.example                  # 設定テンプレート (全モジュール共通の .env)
├── requirements.txt              # Python 依存 (ピン留め)
├── pytest.ini                    # testpaths=tests、integration マーカー
├── Dockerfile                    # webapp + graph_query_agent のコンテナ (Cloud Run 用)
├── CLAUDE.md                     # 作業ルールとコーパスの保守ルール
├── graph_generator/              # パイプライン本体
│   ├── __main__.py               # CLI
│   ├── config.py                 # 設定 (.env + 既定値)
│   ├── pipeline.py               # 全フェーズと実行モード
│   ├── treesitter_parser.py      # Phase 1.5: tree-sitter-java によるエンティティ抽出
│   ├── java_decls.py             # Phase 1.6: 宣言インデックスと呼び出しサイト抽出
│   ├── lsp_client.py             # Phase 1.6: 最小 LSP クライアント (JSON-RPC over stdio)
│   ├── resolution.py             # Phase 1.6: jdtls セッションとシンボル解決
│   ├── prompts.py                # Gemini プロンプト
│   ├── spanner_setup.py          # テーブル・グラフの DDL と作成
│   └── scorer.py                 # score コマンド
├── graph_query_agent/            # ADK エージェント (root_agent → graph_agent → run_gql_query)
├── mcp_server/                   # FastMCP サーバー (README.md にデプロイ手順)
├── webapp/                       # FastAPI + Vite/React のビューアとチャット
├── tests/                        # pytest。fake_lsp_server.py で jdtls 無しでも動く
│   └── fixtures/jdtls_spike/     # jdtls 事前検証の記録 (SPIKE_NOTES.md)
├── scripts/                      # jdtls_spike.py (事前検証)。lsp_*.py / roslyn_spike.py は移植前の遺物
├── test_codes/iryo-kanri-system/ # 同梱コーパス (12 Maven プロジェクト + ground_truth/)
├── docs/                         # 上のドキュメント 4 件
├── presentation.html             # 紹介スライド
└── sample_prompt.txt             # エージェントへの質問例
```
