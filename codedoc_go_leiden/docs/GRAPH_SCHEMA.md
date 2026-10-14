# グラフスキーマ

Cloud Spanner に何が入るか（ノード表・エッジ表・ID の決め方）と、Go のソースコードがどの行になるか、どう問い合わせるかを説明します。
呼び出し先を確定する仕組みは [LSP_RESOLUTION.md](LSP_RESOLUTION.md)、フェーズの流れは [ARCHITECTURE.md](ARCHITECTURE.md#2-フェーズとトラック)、トピックの作り方は [ARCHITECTURE.md](ARCHITECTURE.md#10-leiden-トピック)、モジュールとリポジトリをまたぐエッジは [CROSS_PROJECT.md](CROSS_PROJECT.md)、接続先の設定は [../README.md](../README.md) に譲ります。
この版（codedoc_go_leiden）の表は codedoc_go と同じで、トピックの層だけが違います。トピックは、確定した呼び出し・埋め込み・実装から Leiden 法で求めた構造のトピックです。`Topics` に列を、`FileBelongsToTopic` に `role` を足し、エッジ表を 3 つ足しています。

## 1. 問題: ファイルでは答えられない問い

生成した Wiki は「このファイルは何をするか」には答えますが、次の問いには答えられません。

- `FeeCalculator.Calculate` を呼んでいるのは誰か。
- `PaymentPolicy` を満たす型はどれか。`Inpatient` は何を埋め込んでいるか。
- `billing/fee.go` を変えたら何ファイルに波及するか。
- 受付のモジュールから会計のモジュールへの呼び出しはどこにあるか。
- `FeeCalculator` はどのトピックに属し、そのトピックはどのトピックに依存しているか。

これらは要素どうしの関係をたどる問いで、グラフとして持っていれば 1〜3 ホップの探索で決定的に答えられます。CodeDoc はそのためにソースコードを Spanner のプロパティグラフとして保存し、問い合わせのエージェントはこのグラフに GQL を投げます。

## 2. ノード表

全ノード表に `repo`（所属リポジトリ）と `embedding ARRAY<FLOAT64>` があります。ベクトルが入るのは Files、Packages、Classes（擬似型 `<package>` を除く）、Methods（クロージャと `<init>` を除く）、Topics の、要約のある行で、`--wiki` の Phase 10 が書きます。`--graph` が Spanner の Topics を書き直すときは、名前と要約が前回の `--wiki` と同じトピックのベクトルを、埋め込みのキャッシュから戻します。

| 表 | 1 行は何か | カラム | 書く処理 | 何から作るか |
|---|---|---|---|---|
| Files | `.go` ファイル 1 つ | `file_id` `file_name` `extension` `directory` `path`（リポジトリからの相対パス） `origin`（package / command / test / xtest） `generated` `package`（import パス） `package_name`（パッケージ句の名前） `module`（モジュールパス） `build_constraint` `build_included` `summary` `repo` `embedding` | Phase 8 | Phase 1 のスキャンと Phase 1.5 のエンティティ |
| Packages | ディレクトリとパッケージ句の名前の組 1 つ | `package_id` `import_path` `name` `directory` `module` `origin` `file_count` `summary` `repo` `embedding` | Phase 8 | Phase 1.5 のエンティティを（ディレクトリ、パッケージ句）でまとめたもの |
| Classes | Go の型 1 つ（struct、interface、名前付きの型、型エイリアス）。各ファイルの擬似型 `<package>` も 1 行 | `class_id` `name` `qualname` `package` `fqcn`（import パス + "." + qualname） `file_id` `kind`（struct / interface / named / alias / package） `type_params` `embeds` `start_line` `end_line` `origin` `module` `summary` `repo` `embedding` | Phase 8 | Phase 1.5 のエンティティ |
| Methods | 関数、メソッド、インタフェースのメソッド、`init` 関数、クロージャ、`<init>` 1 つ | `method_id` `name` `class_id` `file_id` `qualname` `fqmn`（import パス + "." + qualname） `signature` `member_kind`（func / method / interface_method / init / closure / var_init） `receiver` `type_params` `has_body` `start_line` `end_line` `origin` `module` `entry_point` `summary` `repo` `embedding` | Phase 8 | Phase 1.5 のエンティティ |
| Topics | 構造のトピック 1 つ（Leiden 法のコミュニティ）。レベル 1 がトピック、レベル 2 がサブトピック。Go のモジュールではない | `topic_id` `name` `summary` `repo` `embedding` `level` `parent_topic_id` `size` `file_count` `cohesion` `conductance` `hub_class_id` `label_source` `basis` `membership_hash` | Phase 9 の後（`--graph`）と Phase 10（`--wiki`） | Phase 4a のコミュニティ（`topics/communities.json`）。名前は Phase 4b、要約は Phase 5 のページ |
| Directories | ディレクトリ 1 つ | `dir_id` `name` `summary` `repo` `embedding` | Phase 8 | スキャンしたファイルのディレクトリとその祖先 |

- **モジュールは列。** Go のモジュールパス（go.mod の `module` 行）は `Files.module`、`Classes.module`、`Methods.module`、`Packages.module` にあります。`Topics` は Leiden 法で求めた構造のトピックで、Go のモジュールとは別物です。モジュールパスもパッケージの import パスも ID の材料ではありません。
- **Packages。** ディレクトリとパッケージ句の名前の組ごとに 1 行です。同じディレクトリの外部テストパッケージ（import パスの末尾が `_test`）は別の行です。`origin` は、属するファイルのうち最も本番に近いもの（package、command、test、xtest の順）です。
- **Classes。** `embeds` は埋め込んだ型の文字列の JSON 配列で、ポインタの埋め込みは `*` が付きます（例 `Visit` は `["*patient.Patient"]`）。`type_params` は型パラメータの宣言の文字列です。
- **Methods の持ち主（`class_id`）。** メソッドはレシーバーの型の行に属します。型の宣言と別のファイルにあるメソッドも、`file_id` は自分のファイルのまま、`class_id` は型の行を指します（2026-10-13 の調査で etcd に 105、Prometheus に 302 ありました）。レシーバーの型にビルドの双子があるときは、同じファイルの双子、主ビルド構成でコンパイルされる双子、相対パスの小さい双子の順で選びます。関数、`init#k`、`<init>`、レシーバーの型が見つからないメソッドは、そのファイルの `<package>` に属します。クロージャは一番外側の宣言の持ち主に属します。インタフェースのメソッドはインタフェースの行に属し、`receiver` は空、`has_body` は false です。
- **Methods の列。** `receiver` はレシーバーの書き方（例 `*FeeCalculator`）です。`has_body` が false なのは、本体の無い宣言（アセンブリ、`//go:linkname`）とインタフェースのメソッドです。`entry_point` は `package main` の `func main` にだけ入るコマンド名（ディレクトリ名）です。
- **BOOL の列。** `generated`、`build_included`、`has_body` は BOOL です。

### Topics の列

| 列 | 内容 |
|---|---|
| `level` | 1 がトピック、2 がサブトピック。サブトピックに分かれなかったレベル 1 のトピックは葉で、行はレベル 1 の 1 つだけ |
| `parent_topic_id` | サブトピックの親のトピック。レベル 1 では null |
| `size` | メンバーの原子（型と擬似型 `<package>`）の数。役割を問わない |
| `file_count` | メンバーの宣言があるファイルの数 |
| `cohesion` | クラスタリングの対象のメンバーの間で、エッジのある組の数を可能な組の数で割った値。重みは使わない |
| `conductance` | クラスタリングの対象のメンバーから外へ出る重みの和を、メンバーの重み付き次数の和と残りの和の小さい方で割った値 |
| `hub_class_id` | トピックの中で PageRank が最も高いメンバー。Phase 4b の前は、その名前（型なら `billing.FeeCalculator`、擬似型なら `billing/fee.go` の形）がトピックの名前になる |
| `label_source` | 名前の出どころ（下の表） |
| `basis` | 常に `structure`（構造から決めたトピック） |
| `membership_hash` | primary のメンバーの `class_id` を並べた文字列の SHA-256 の先頭 16 桁。`topic_id` の材料 |

| `label_source` | 名前 |
|---|---|
| `hub` | ハブの名前。Gemini の名前がまだ無いトピック（初めての `--wiki` の前と、メンバーが変わった後の `--graph`）と、Phase 4b が再試行しても正しい名前を得られなかったトピック |
| `llm` | Phase 4b で Gemini が付けた名前 |
| `inherited` | 前回の実行から引き継いだ名前。前回と同じ ID のトピックか、primary のメンバーの Jaccard 係数が 0.6 以上で前回のトピックに対応付いたトピック |
| `fixed` | 「未分類」の固定の名前 |

- **「未分類」。** どのトピックにも付けられなかった原子を集めるレベル 1 のトピックで、そういう原子があるときだけ、リポジトリに 1 つできます。Gemini には送りません。`hub_class_id`、`cohesion`、`conductance` は null で、`membership_hash` は空の集合のハッシュです。
- **ID の安定。** 名前を変えても `topic_id` は変わりません。primary のメンバーが 1 つでも変われば別の ID になります（[第 5 節](#5-id-規則)）。

### summary の値

`--wiki` の前は Go のドキュメントコメント（無ければ空）です。`--wiki` では Phase 2 と 3 が要約を作り、Phase 10 がそれで Spanner の summary 列を書き換えます。要約の無いノードは元の値のままです。`--no-spanner` では書き換えず、要約は `summaries/` に残ります。

`--wiki` のあとに Spanner に書く `--graph` をやり直すと、Phase 8 は新しいファイルと変わったファイルの行にドキュメントコメントを書き、変わっていないファイルの行（内容の SHA-256 が同じファイルのノードと、子の要約が変わらないパッケージとディレクトリ）は要約を保ちます。変わったファイルの行は、次の `--wiki` で新しい要約になります。`graph_rows/` の summary は常にドキュメントコメントです。`embedding` は Phase 8 が書かないので、`--graph` では変わりません（Topics は Phase 9 の後に書き直し、名前と要約が変わらないトピックのベクトルを埋め込みのキャッシュから戻します）。

| 列 | `--wiki` の前 | `--wiki` の後 |
|---|---|---|
| `Files.summary` と、擬似型 `<package>` の `Classes.summary` | そのファイルのパッケージのドキュメントコメント | ファイルの要約（Phase 2） |
| `Packages.summary` | テストでないファイルのパッケージのドキュメントコメントをつないだもの。無ければテストのファイルのもの | パッケージの要約（Phase 3） |
| ほかの `Classes.summary`、`Methods.summary` | 宣言のドキュメントコメント | ノードの要約（Phase 2） |
| `Directories.summary` | 空 | ディレクトリの要約（Phase 3） |
| `Topics.summary` | 空。ID が前回の `--wiki` から変わらないトピックは、そのときのページ | トピックのページ（Phase 5） |

### qualname の文法

| 宣言 | qualname |
|---|---|
| 関数 | `F` |
| メソッド（値とポインタのレシーバー） | `T.M`。レシーバーの書き方は `receiver` 列で、型パラメータは名前に入らない |
| インタフェースのメソッド | `I.M` |
| クロージャ（関数リテラル） | `F.func1`。入れ子は `F.func1.func1`。番号は親ごとのソース順。パッケージ変数の初期化式の中は `<init>.func1` |
| `init` 関数 | `init#1`、`init#2`（ファイルごとの連番） |
| 空白の関数 `func _()` | `_#1`、`_#2`。空白のメソッドは `T._#1` |
| コンパイルできないコードで重なった名前 | 2 つ目から `F#2`、`F#3` |
| パッケージ変数の初期化式 | `<init>`（ファイルごとに 1 つ。そこからの呼び出しの呼び出し元） |
| 擬似型 | `<package>`（ファイルごとに 1 つ） |

- 1 つのファイルの中で qualname は重なりません（2026-10-13 に etcd と Prometheus の 1,840 ファイルで確かめました）。
- `<package>`、`<init>`、`init#k`、`_#k` はファイルごとにあり、ビルドの双子は同じ qualname を別のファイルに持ちます。どちらも同じパッケージの中で `fqcn` / `fqmn` が重なるので、`Files.path` か `file_id` で区別します。

## 3. エッジ表

すべて `edge_id`（主キー）、source 列、destination 列、補助列の形です。向きはプロパティグラフ定義の `SOURCE KEY` / `DESTINATION KEY` が決めます。

| 表 | 向き | カラム | 書く処理 | 何から作るか |
|---|---|---|---|---|
| PackageImports | Files → Packages | `edge_id` `source_file` `target_package` `import_path` `alias`（無ければ空） `kind`（normal / dot / blank） | Phase 9 | `resolved` の import の記録で、相手がこのリポジトリのパッケージ |
| FileDependsOn | Files → Files | `edge_id` `source_file` `target_file` `resolution`（最初に届いたサイトの `via`） | Phase 9 | MethodCalls と ClassEmbeds が届くファイル（自分自身を除く） |
| ClassEmbeds | Classes → Classes | `edge_id` `child_class` `embedded_class` `pointer` `resolution`（`types`） | Phase 9 | 両端がこのリポジトリの型の、埋め込みの記録 |
| ClassImplements | Classes → Classes | `edge_id` `class_id` `interface_id` `pointer_receiver` `resolution`（`types`） | Phase 9 | 型のファイルを解決したビルド構成での（具体型、このリポジトリのインタフェース）の組。`pointer_receiver` は `*T` だけが満たすとき true |
| MethodCalls | Methods → Methods | `edge_id` `caller_method` `callee_method` `callee_name` `resolution`（記録の `via`） `site_kind`（call / go / defer / range） `call_line` | Phase 9 | `resolved` の記録で、相手がこのリポジトリの Methods のノード（インタフェースのメソッドとクロージャを含む） |
| PossiblyCalls | Methods → Methods | `edge_id` `caller_method` `callee_method` `callee_name` `reason`（implementation / variant） `candidate_count` `open_world` | Phase 9 | インタフェースのサイトと `ambiguous` の記録の、グラフにある候補 |
| FileDefinesClass | Files → Classes | `edge_id` `file_id` `class_id` | Phase 9 | エンティティ |
| ClassDefinesMethod | Classes → Methods | `edge_id` `class_id` `method_id` | Phase 9 | Methods の持ち主（第 2 節） |
| DirContainsFile | Directories → Files | `edge_id` `dir_id` `file_id` | Phase 9 | ディレクトリツリー |
| PackageContainsFile | Packages → Files | `edge_id` `package_id` `file_id` | Phase 9 | ファイルの属するパッケージ |
| FileBelongsToTopic | Files → Topics | `edge_id` `file_id` `topic_id` `role`（primary / secondary） | Phase 9 の後と Phase 10 | ファイルの型と擬似型 `<package>` が属する葉のトピックすべて（「未分類」を含む）。宣言（型と、クロージャを除く関数・メソッド）が最も多いトピックが `primary`、ほかは `secondary`。同数なら `topic_id` の小さい方が `primary` |
| ClassBelongsToTopic | Classes → Topics | `edge_id` `class_id` `topic_id` `role` | Phase 9 の後と Phase 10 | 各レベルのトピックのメンバー（擬似型 `<package>` を含む）。サブトピックに分かれたトピックの原子は、レベル 1 とレベル 2 に 1 行ずつ持つ |
| TopicDependsOn | Topics → Topics | `edge_id` `source_topic` `target_topic` `weight` `call_count` | Phase 9 の後と Phase 10 | 葉のトピックの間の、向きのある依存（下記） |
| TopicContainsTopic | Topics → Topics | `edge_id` `parent_topic` `child_topic` | Phase 9 の後と Phase 10 | サブトピックに分かれたレベル 1 のトピックと、その子 |
| CrossRepoCalls | Methods → Methods | `edge_id` `caller_method` `callee_method` `source_repo` `target_repo` `symbol` `site_kind` | `crossref` | ピアの関数・メソッドの呼び出し |
| CrossRepoRef | Classes → Classes | `edge_id` `source_class` `target_class` `source_repo` `target_repo` `symbol` `kind`（embeds / implements） | `crossref` | ピアの型の埋め込みと実装 |
| CrossRepoImports | Files → Packages | `edge_id` `source_file` `target_package` `source_repo` `target_repo` `import_path` | `crossref` | ピアのパッケージの import |

このグラフの約束は次のとおりです。

- MethodCalls、PackageImports、ClassEmbeds、ClassImplements は、Phase 1.6 が `resolved` にした記録で、両端がこのリポジトリのノードのものからしか作らない。
- `external`（標準ライブラリ、依存モジュール、cgo、ピア）、`builtin`、`dynamic`、`unresolved` は数えるだけで、MethodCalls にしない（インタフェースの候補の PossiblyCalls は下記）。ピアの相手は `crossref` が CrossRepo* にする（[CROSS_PROJECT.md](CROSS_PROJECT.md)）。
- インタフェースの呼び出しは、インタフェースのメソッドへの MethodCalls と、各候補への PossiblyCalls（`reason` は `implementation`）になる。リポジトリの外のインタフェース（`external`）でも、リポジトリ内の候補は PossiblyCalls に載る。`open_world` は、リポジトリの外の型もそのインタフェースを満たすサイトで true になる。
- 1 サイトの候補が `POSSIBLY_CALLS_MAX_CANDIDATES`（[README](../README.md#設定リファレンス)）を超えると、そのサイトの PossiblyCalls は 1 本も書かず、数だけ数える。名前の推測による候補は作らない。
- 同じ（表、source、target）の行は 1 本にまとめ、ソース順で最初のサイトの `call_line` と `site_kind` を残す。
- トピックのメンバーは、MethodCalls、ClassEmbeds、ClassImplements の行だけから決める（エッジの無い原子は同じファイルの原子に合わせる）。PossiblyCalls、外部の相手、ディレクトリやパッケージ、名前の似かよりは使わない。

status ごとの判断は [LSP_RESOLUTION.md](LSP_RESOLUTION.md#9-statusvia理由) にあります。

### トピックのエッジ

`ClassBelongsToTopic.role` は、原子がどうしてそのトピックに入ったかを表します。付け方の手順は [ARCHITECTURE.md](ARCHITECTURE.md#103-メンバーの決め方と未分類) にあります。

| `role` | 意味 |
|---|---|
| `primary` | クラスタリングで決まったメンバー。本番のファイル（origin が package か command）で、生成コードでなく、主ビルド構成でコンパイルされ、エッジを持つ原子 |
| `attached-file` | エッジの無い原子と、小さすぎて隣の無い集まりに残った原子。同じファイルの primary の原子が最も多いトピックに付けた。レベル 2 では、親の中にエッジの無い原子と、どこにも付かずにメンバーのハッシュが最も小さい子に入れた原子（テスト、生成コード、ビルドから外れるコードを除く）もこれになる |
| `attached-test` | テストのファイルの原子。primary の原子と最も多く重みを共有するトピックに付けた |
| `attached-generated` | 生成コードの原子。付け方は `attached-test` と同じ |
| `attached-excluded` | 主ビルド構成でコンパイルされないファイルの原子。付け方は `attached-test` と同じ |
| `unassigned` | どこにも付けられなかった原子。「未分類」のメンバー |

TopicDependsOn は次のように作ります。

- 葉のトピックの間で、クラスタリングの対象の原子（テスト、生成コード、ビルドから外れるコードを除く）を結ぶ事実から作る。「未分類」は入らない。
- 向きは、呼び出しが呼び出し元の持ち主から呼び出し先の持ち主へ、埋め込みが埋め込む型から埋め込まれる型へ、実装が型からインタフェースへ。
- `call_count` は、呼び出し（インタフェースのメソッドへの呼び出しを含む）の（呼び出し元、呼び出し先）メソッドの組の数。同じ組を何か所で呼んでも 1 つで、クロージャからの呼び出しは外側の宣言の呼び出しとして数える。
- `weight` は、source のトピックの原子から target のトピックの原子への組ごとに求めた Σ α·min(log2(1 + n), cap) の和で、正規化しない。α と cap は [ARCHITECTURE.md](ARCHITECTURE.md#101-射影) にある。埋め込みと実装も入るので、それだけでつながるトピックの組は `call_count` が 0 になる。

## 4. 具体例: F1 の `handleFees` から `FeeCalculator.Calculate` へ

フィクスチャ F1 を `repo=iryo-kanri-system`、`ID_PREFIX=a` で処理したときの行です。患者ポータル（`example.com/iryo/patient-portal`）と会計（`example.com/iryo/billing`）は、同じリポジトリの別のモジュールです。

`patient-portal/internal/server/server.go`（34〜37 行目）:

```go
func (p *Portal) handleFees(w http.ResponseWriter, r *http.Request) {
	points := 120
	fmt.Fprintf(w, "診察料 %d円", p.fees.Calculate(points, 1))
}
```

`billing/fee.go`（5〜15 行目）:

```go
type FeeCalculator struct {
	Rate int
}

func NewFeeCalculator(rate int) *FeeCalculator {
	return &FeeCalculator{Rate: rate}
}

func (c *FeeCalculator) Calculate(points, count int) int {
	return guard.EnsurePositive(points*count) * c.Rate / 10
}
```

**Phase 1（モジュールとグループ）**: 2 つのモジュールはどちらもルートの `go.work` が `use` するので、1 つのワークスペースのグループで一緒に読み込みます。`server.go` の import `"example.com/iryo/billing"` はリポジトリ内のソースに着きます。

**Phase 1.5（エンティティ）**: `server.go` は `package=example.com/iryo/patient-portal/internal/server`、`package_name=server`、`origin=package` です。`Portal.handleFees`（method、レシーバー `*Portal`、34〜37 行目）のサイト（行:バイト列）は `Fprintf`（call、36:6）と `Calculate`（call、36:43）です。列はバイトで数えるので、日本語の文字列の後ろにある `Calculate` は 43 列目です。`fee.go` の `FeeCalculator.Calculate`（method、13〜15 行目）は `EnsurePositive`（call、14:15）を呼びます。

**Phase 1.6（解決）**

| サイト | 結果 |
|---|---|
| `fmt.Fprintf(...)` | `external`（モジュール `std`） |
| `p.fees.Calculate(...)` | `resolved`（`types`）。相手は `billing/fee.go` の `FeeCalculator.Calculate`。同じワークスペースのモジュールなので、リポジトリ内のソースに着く |
| `guard.EnsurePositive(...)`（`Calculate` の中） | `resolved`（`types`）。相手は `kernel/guard/guard.go` の `EnsurePositive` |
| import `"example.com/iryo/billing"`（10 行目） | リポジトリ内のパッケージ（ディレクトリ `billing`、パッケージ句 `billing`） |

**Phase 8（ノード）**

| 表 | 主キー | 主な値 |
|---|---|---|
| Files | `a_70a89941b966783d` | `path=patient-portal/internal/server/server.go` `package=example.com/iryo/patient-portal/internal/server` `module=example.com/iryo/patient-portal` `origin=package` |
| Files | `a_1c58a4e8fc5505ae` | `path=billing/fee.go` `package=example.com/iryo/billing` `package_name=billing` `module=example.com/iryo/billing` |
| Packages | `a_4b019ebe2ad69a43` | `import_path=example.com/iryo/billing` `name=billing` `directory=billing` `file_count=3`（`fee.go`、`session.go`、`fee_test.go`） `origin=package` |
| Classes | `a_b28d7c3d3add2168` | `name=Portal` `qualname=Portal` `fqcn=example.com/iryo/patient-portal/internal/server.Portal` `kind=struct` |
| Classes | `a_ff6a51302bb35490` | `name=FeeCalculator` `qualname=FeeCalculator` `fqcn=example.com/iryo/billing.FeeCalculator` `kind=struct` `start_line=5` `module=example.com/iryo/billing` |
| Classes | `a_8adfec202255db5b` | `name=<package>` `qualname=<package>` `kind=package` `file_id=a_1c58a4e8fc5505ae`（`fee.go` の擬似型） |
| Methods | `a_664243d851b691d0` | `name=handleFees` `qualname=Portal.handleFees` `receiver=*Portal` `member_kind=method` `class_id=a_b28d7c3d3add2168` `start_line=34` `end_line=37` |
| Methods | `a_1af7d83dfa68f211` | `name=Calculate` `qualname=FeeCalculator.Calculate` `fqmn=example.com/iryo/billing.FeeCalculator.Calculate` `receiver=*FeeCalculator` `member_kind=method` `class_id=a_ff6a51302bb35490` `start_line=13` `end_line=15` |
| Methods | `a_b4091fa8f2f275b1` | `name=NewFeeCalculator` `qualname=NewFeeCalculator` `member_kind=func` `class_id=a_8adfec202255db5b` |

**Phase 9（エッジ）**

| 表 | source → destination | 補助列 |
|---|---|---|
| PackageImports | `server.go` → パッケージ `billing` | `import_path=example.com/iryo/billing` `alias=` `kind=normal` |
| MethodCalls | `Portal.handleFees` → `FeeCalculator.Calculate` | `edge_id=a_8a8092f06d61765c` `callee_name=Calculate` `resolution=types` `site_kind=call` `call_line=36` |
| MethodCalls | `FeeCalculator.Calculate` → `EnsurePositive` | `callee_name=EnsurePositive` `resolution=types` `site_kind=call` `call_line=14` |
| FileDependsOn | `server.go` → `fee.go`、`fee.go` → `kernel/guard/guard.go` | `resolution=types` |
| ClassDefinesMethod | `FeeCalculator` → `FeeCalculator.Calculate`、`fee.go` の `<package>` → `NewFeeCalculator` | |
| PackageContainsFile | パッケージ `billing` → `fee.go` | |

**Phase 4a と Phase 9 の後（トピック）**: `FeeCalculator` は本番のファイルの型で呼び出しのエッジを持つので、Phase 4a でどれかのトピックの primary のメンバーになります。ClassBelongsToTopic はレベルごとに 1 行で、トピックがサブトピックに分かれていればレベル 1 とレベル 2 の 2 行です。`fee.go` は FileBelongsToTopic で、宣言が最も多い葉のトピックに `role=primary` で属します。`Portal` と `FeeCalculator` が別の葉のトピックに入れば、この MethodCalls は TopicDependsOn（`Portal` のトピック → `FeeCalculator` のトピック）の `call_count` に 1 つ数えられます。どのトピックに入るかは Leiden 法の結果で決まるので、トピックの ID と名前はここに載せません。

`fmt.Fprintf` は `external` なので数えるだけです。`handleFees` → `Calculate` の行は、両端の `module` が `example.com/iryo/patient-portal` と `example.com/iryo/billing` で違う、同じリポジトリの中でモジュールをまたぐ呼び出しです。「エッジが無い」は「呼んでいない」ではなく、「相手がグラフの外にある」か「確定できなかった」場合を含みます。

## 5. ID 規則

ノードの ID は `pipeline._make_id(kind, repo, relpath, qualname)` が作ります。

```
ID = ID_PREFIX + "_" + sha256(kind + "\0" + repo + "\0" + relpath + "\0" + qualname) の 16 進の先頭 16 桁
```

| ノード | 材料 |
|---|---|
| File | (`file`, repo, 相対パス, "") |
| Package | (`package`, repo, ディレクトリの相対パス, パッケージ句の名前) |
| Class | (`class`, repo, 宣言のファイルの相対パス, qualname。擬似型は `<package>`) |
| Method | (`method`, repo, 宣言のファイルの相対パス, qualname) |
| Directory | (`dir`, repo, ディレクトリの相対パス, "") |
| Topic | (`topic`, repo, `L<レベル>`, `membership_hash`)。「未分類」は (`topic`, repo, `L1`, `unassigned`) |
| エッジ | (`edge`, 表名, source の ID, target の ID) |

- 区切りの `\0` はパスにも名前にも現れないので、別の入力が同じ文字列にならない。
- 相対パスなので、リポジトリをどのマシンのどこに置いて解析しても同じ ID になる。
- qualname を含むので、別のファイルや別の型の同名メソッドが 1 ノードに混ざらない。同じ ID に別のシンボルが当たったら、Phase 8 が止まる。
- `repo` を含むので、別リポジトリの同じ相対パス・同じ qualname も別ノードになる。
- モジュールパスと import パスは含めない。go.mod の `module` 行を変えても ID は変わらない。
- ID は抽出の結果から作るので、解決が失敗しても変わらない。
- トピックの ID は名前でなく primary のメンバーで決まる。名前を変えても ID は変わらず、primary のメンバーが変われば別の ID になる。`graph_generator/topics/community.py` が同じ式で計算する。
- `_make_id` の材料を変えたら `ID_SCHEME`（現在 1）を上げる。`graph_checkpoint.json` と `graph_meta.json` に記録され、古いスキームのチェックポイントは捨てられ、`crossref` は古いスキームのリポジトリを飛ばす。

## 6. プロパティグラフと接続

`setup_spanner_graph.graph_ddl()` が 6 つのノード表と 17 のエッジ表（codedoc_go の 14 に、`topics_schema.py` が足す 3 つ）を束ねて、`GRAPH_NAME` の名前で `CREATE OR REPLACE PROPERTY GRAPH` します。接続先の設定は [README](../README.md#設定リファレンス) にあります。`--no-spanner` でも同じ行が `graph_rows/` に出ます（[README](../README.md#出力)）。既定の名前（インスタンス `codedoc-instance`、データベース `codedoc-go-leiden`、グラフ `code_graph_a`）での確認の例です。

```bash
gcloud spanner databases execute-sql codedoc-go-leiden --instance=codedoc-instance \
  --sql="GRAPH code_graph_a MATCH (c:Classes) RETURN COUNT(c) AS classes"
```

## 7. GQL クエリ集

GQL は `GRAPH <名前> MATCH (...)-[:エッジ]->(...) RETURN ...` の形です。最後の `RETURN` ではノードそのものではなくプロパティを返します。複数のリポジトリを置いている場合は `WHERE n.repo = '<repo>'` で絞り、厳密に指すときは `fqcn` / `fqmn` を使います（単純名は重複します）。`<package>` などファイルごとの名前は `Files.path` で区別します。

```sql
-- 存在確認（通常の SQL）
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '' ORDER BY TABLE_NAME;
SELECT PROPERTY_GRAPH_NAME FROM INFORMATION_SCHEMA.PROPERTY_GRAPHS;

-- 件数
GRAPH code_graph_a MATCH (f:Files) RETURN COUNT(f) AS files;
GRAPH code_graph_a MATCH (m:Methods) RETURN m.repo, m.module, COUNT(m) AS methods GROUP BY m.repo, m.module;

-- この関数・メソッドを呼んでいるのは誰か（確定したエッジだけ）
GRAPH code_graph_a
MATCH (caller:Methods)-[e:MethodCalls]->(callee:Methods {fqmn: 'example.com/iryo/billing.FeeCalculator.Calculate'})
RETURN caller.fqmn, caller.module, e.resolution, e.site_kind, e.call_line;

-- この関数・メソッドが呼んでいるもの（確定と候補）
GRAPH code_graph_a
MATCH (caller:Methods {fqmn: 'example.com/iryo/reception-desk/internal/flows/accounting.Run'})-[e:MethodCalls]->(callee:Methods)
RETURN callee.fqmn, callee.member_kind, callee.module, e.resolution, e.site_kind, e.call_line;
GRAPH code_graph_a
MATCH (caller:Methods {fqmn: 'example.com/iryo/reception-desk/internal/flows/accounting.charge'})-[p:PossiblyCalls]->(callee:Methods)
RETURN callee.fqmn, p.callee_name, p.reason, p.candidate_count, p.open_world;

-- インタフェースのメソッドへの呼び出しと、リポジトリの外の型も満たす呼び出しの候補
GRAPH code_graph_a
MATCH (caller:Methods)-[e:MethodCalls]->(im:Methods {member_kind: 'interface_method'})
WHERE caller.repo = 'iryo-kanri-system'
RETURN caller.fqmn, im.fqmn, e.call_line;
GRAPH code_graph_a
MATCH (caller:Methods)-[p:PossiblyCalls]->(impl:Methods)
WHERE p.open_world = true
RETURN caller.fqmn, impl.fqmn, p.candidate_count;

-- 型のメソッド一覧
GRAPH code_graph_a
MATCH (c:Classes {fqcn: 'example.com/iryo/billing.FeeCalculator'})-[:ClassDefinesMethod]->(m:Methods)
RETURN m.qualname, m.member_kind, m.receiver, m.signature;

-- パッケージ直下の関数（擬似型 <package> のメソッド）と、関数のクロージャ
GRAPH code_graph_a
MATCH (f:Files {package: 'example.com/iryo/billing'})-[:FileDefinesClass]->(c:Classes {kind: 'package'})-[:ClassDefinesMethod]->(m:Methods)
RETURN f.path, m.qualname, m.member_kind, m.signature;
GRAPH code_graph_a
MATCH (m:Methods {member_kind: 'closure'})
WHERE STARTS_WITH(m.fqmn, 'example.com/iryo/reception-desk/internal/flows/dispatch.Run.')
RETURN m.qualname, m.start_line;

-- インタフェースを満たす型と、型が満たすインタフェース
GRAPH code_graph_a
MATCH (t:Classes)-[e:ClassImplements]->(i:Classes {fqcn: 'example.com/iryo/kernel/policy.PaymentPolicy'})
RETURN t.fqcn, t.kind, t.module, e.pointer_receiver;
GRAPH code_graph_a
MATCH (t:Classes {fqcn: 'example.com/iryo/kernel/policy.UrgentPolicy'})-[e:ClassImplements]->(i:Classes)
RETURN i.fqcn, i.module, e.pointer_receiver;

-- 埋め込みの木（3 階層まで）と、この型を埋め込んでいる型
GRAPH code_graph_a
MATCH (t:Classes {fqcn: 'example.com/iryo/domain/patient.Inpatient'})-[:ClassEmbeds]->{1,3}(inner:Classes)
RETURN DISTINCT inner.fqcn, inner.kind;
GRAPH code_graph_a
MATCH (outer:Classes)-[e:ClassEmbeds]->(t:Classes {fqcn: 'example.com/iryo/kernel/policy.BasePolicy'})
RETURN outer.fqcn, outer.module, e.pointer;

-- パッケージの依存と、このパッケージを import しているパッケージ
GRAPH code_graph_a
MATCH (p:Packages {import_path: 'example.com/iryo/reception-desk/internal/flows/accounting'})-[:PackageContainsFile]->(f:Files)-[e:PackageImports]->(dep:Packages)
RETURN DISTINCT dep.import_path, dep.module, e.kind;
GRAPH code_graph_a
MATCH (src:Packages)-[:PackageContainsFile]->(f:Files)-[:PackageImports]->(p:Packages {import_path: 'example.com/iryo/billing'})
RETURN DISTINCT src.import_path, src.module;

-- このファイルに依存しているファイル（影響範囲、3 ホップまで）
GRAPH code_graph_a
MATCH (dep:Files)-[:FileDependsOn]->{1,3}(f:Files {path: 'billing/fee.go'})
RETURN DISTINCT dep.path, dep.package;

-- 既定のビルドから外れるファイルと生成コード
GRAPH code_graph_a
MATCH (f:Files)
WHERE f.repo = 'iryo-kanri-system' AND (f.build_included = false OR f.generated = true)
RETURN f.path, f.build_constraint, f.generated;

-- コマンド（func main）の一覧
GRAPH code_graph_a
MATCH (m:Methods)
WHERE m.repo = 'iryo-kanri-system' AND m.entry_point <> ''
RETURN m.entry_point, m.fqmn, m.module;

-- トピックに属するファイル
GRAPH code_graph_a
MATCH (f:Files)-[b:FileBelongsToTopic]->(t:Topics {name: '会計'})
RETURN f.path, f.package, b.role;

-- トピックに属する型（役割つき）
GRAPH code_graph_a
MATCH (c:Classes)-[b:ClassBelongsToTopic]->(t:Topics {name: '会計'})
RETURN c.fqcn, c.kind, b.role;

-- 型が属するトピック（レベル 1 と、分かれていればサブトピック）
GRAPH code_graph_a
MATCH (c:Classes {fqcn: 'example.com/iryo/billing.FeeCalculator'})-[b:ClassBelongsToTopic]->(t:Topics)
RETURN t.name, t.level, b.role
ORDER BY t.level;

-- トピックごとの型の数（役割別、各型を 1 回だけ数える）
GRAPH code_graph_a
MATCH (c:Classes)-[b:ClassBelongsToTopic]->(t:Topics)
WHERE t.repo = 'iryo-kanri-system' AND t.level = 1
RETURN t.name, b.role, COUNT(*) AS types
GROUP BY t.name, b.role
ORDER BY t.name, types DESC;

-- トピックの間の依存（呼び出すメソッドの組の多い順）
GRAPH code_graph_a
MATCH (a:Topics)-[d:TopicDependsOn]->(b:Topics)
WHERE a.repo = 'iryo-kanri-system'
RETURN a.name, b.name, d.call_count, d.weight
ORDER BY d.call_count DESC;

-- トピックの木（トピックとサブトピック）
GRAPH code_graph_a
MATCH (parent:Topics)-[:TopicContainsTopic]->(child:Topics)
WHERE parent.repo = 'iryo-kanri-system'
RETURN parent.name, child.name, child.size, child.label_source;
```

トピックの名前は `--wiki` で決まります。初めての `--wiki` の前は、ハブの名前（`label_source = hub`）です。前回の `--wiki` から ID の変わらないトピックは、`--graph` の後もその名前のままです。

モジュールとリポジトリをまたぐクエリは [CROSS_PROJECT.md](CROSS_PROJECT.md#6-グラフでの見え方) にあります。

## 8. このグラフが表していないもの

- 呼び出し・埋め込み・実装・import のエッジは確定したものだけである。標準ライブラリや依存モジュールへの呼び出し（`external`）、組み込み関数（`builtin`）、関数値の呼び出し（`dynamic`）、確定できなかった呼び出し（`unresolved`）は数えるだけで線にならない。エッジが無いことは呼んでいないことを意味しない。
- インタフェースの呼び出しはインタフェースのメソッドへの線である。実行時の相手は PossiblyCalls の候補のどれかとは限らない（リポジトリの外の型、ジェネリック型、候補が上限を超えたサイト、G4 で候補を出さないサイト）。実装が 1 つしかなくても、その実装への MethodCalls は張らない。
- 型変換 `T(x)` は呼び出しに数えない。
- 標準ライブラリと依存モジュールの宣言はノードにならない。
- 既定のビルドから外れるファイル（`build_included = false`）の宣言もノードになる。その中の呼び出しは、`GO_EXTRA_BUILDS` のどの構成もそのファイルをコンパイルしなければ `unresolved`（`excluded-by-build`）で、線にならない。
- トピックのメンバーは構造から決まり、同じ入力なら同じになる。名前とページは Gemini 由来で、ID の変わらないトピックは前の名前を引き継ぐ。
- トピックの単位は型と擬似型 `<package>` で、関数とメソッドは持ち主の原子と一緒に動く。1 つのファイルの宣言が複数のトピックに分かれることがある（FileBelongsToTopic の `secondary`）。
- TopicDependsOn は確定したエッジだけから作るので、`external`、`dynamic`、`unresolved` の呼び出しと、テストからの呼び出しは数えない。
- 再投入では、前回書いた行のうち今回書かなかった行を消す。別の書き込み先に書いた行と、`graph_rows/` を消したあとに再投入したときの前回の行は残る（[README](../README.md#鮮度とレジューム)）。
- トピックの 5 つの表は、リポジトリと `ID_PREFIX` ごとに毎回消してから入れ直す。書き込みは 2,000 行ずつのコミットに分かれ、途中で止まると次の実行まで一部だけが入った状態になる。
