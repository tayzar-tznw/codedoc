# モジュールとリポジトリをまたぐ扱い

1 つのリポジトリに複数の Go モジュールがあるとき、複数のリポジトリを 1 つの Spanner グラフに置くときに、同じ名前のシンボルを取り違えない仕組みと、境界をまたぐ呼び出しをどう扱うかを説明します。
コマンドとピアの渡し方は [../README.md](../README.md#ピアリポジトリ)、表とカラムは [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md)、1 つのグループの中の解決（読み込み、位置の分類、ゲート）は [LSP_RESOLUTION.md](LSP_RESOLUTION.md) に譲ります。

## 1. 問題: 同じ名前、同じ行でも行き先が違う

- 1 つのリポジトリにモジュールがいくつもあると、`Logger.Log` や `Validator.Validate` のような同じ名前のメソッドが別々のモジュールにある。名前で繋ぐと別のモジュールの定義に結び付く。
- import 文だけが違い、呼び出しの行がバイト単位で同じファイルがある。同じ行でも、どのモジュールの関数を呼ぶかはそのファイルの import と、属するモジュールのグループで決まる。
- 同じリポジトリのモジュールでも、go コマンドがリポジトリ内のソースではなく公開版をビルドすることがある。`go.work` に入っていないモジュールが、隣のモジュールを replace せずに require している場合である。
- 別のリポジトリでも、相対パスと qualname が完全に一致することがある。ノード ID にリポジトリが入っていなければ 1 ノードに混ざる。
- 別のリポジトリのモジュールは、そのままではモジュールキャッシュの公開版として読まれる。公開版は、取り込んだ作業中のソースとは別物である。

## 2. 1 つのリポジトリの中のモジュール

- `go.mod` のあるディレクトリが 1 つのモジュール（ユニット）になる。キーはモジュールパスである。ファイルは、その上にある一番深い `go.mod` のモジュールに属する。どの `go.mod` の下にも無いファイルは抽出するが解決しない（理由 `no-module`）。
- 一緒に読み込むモジュールの組を**グループ**と呼ぶ。グループは go コマンドが実際にどうビルドするかに合わせて決める。

| 構成 | グループ | モジュールをまたぐ呼び出しの行き先 |
|---|---|---|
| モジュールのルートから上にたどって最初に見つかる `go.work` が、そのモジュールを `use` している | その `go.work` ごとに 1 つのワークスペースのグループ | `use` された全モジュールはリポジトリ内のソースに着く（`MethodCalls`）。版は全モジュールの require を合わせて選ばれ、`go.work` の replace が各 `go.mod` の replace に勝つ |
| 上にたどって見つかる `go.work` が無い、またはそれが `use` していない | 単一モジュールのグループ（`GOWORK=off`） | `go.mod` のローカル replace がリポジトリ内のモジュールを指すときだけリポジトリ内のソース。それ以外は公開版の `external`（モジュールパスと版付き） |

- 1 つのモジュールは 1 つのグループにだけ属する。上にない `go.work` がモジュールを `use` していても、そのモジュールをグループには入れず、警告を出す。開発者がモジュールのディレクトリで go コマンドを実行したとき、その `go.work` は見えないからである。
- 例: F1 の `iryo-kanri-system` では、ルートの `go.work` が大半のモジュールを束ねる。その外にある `admin-console` は、`billing` などをローカル replace でリポジトリ内のソースに向ける一方、`example.com/thirdparty/textfmt` は replace せずに公開版 `v1.1.0` を require する。ワークスペースの中では `textfmt` への呼び出しがリポジトリ内のコピー（`third_party/textfmt`）に着くが、`admin-console` からの同じ呼び出しは公開版の `external` になる。
- vendor したグループでは、依存モジュールは vendor のコピーとして `external` になる。`vendor/modules.txt` が go.mod と合わないグループは status `error` で、解決しない。
- 両端のノードに `module` カラムがある。モジュールをまたぐかどうかは、この 2 つを比べれば分かる（[6. グラフでの見え方](#6-グラフでの見え方)）。
- `units.json` の `in_repo_deps` は、そのモジュールのファイル（主ビルド構成でコンパイルされるもの。テストを含む）が import するパッケージを持つ、リポジトリ内のモジュールの一覧である。ただし、グループの中でその import がリポジトリ内のソースに着くもの（同じワークスペースのモジュール、またはローカル replace の先）に限る。公開版に着く import は数えない。

## 3. 同一性

ノード ID は（種別、リポジトリ、リポジトリからの相対パス、qualname）のハッシュです（[GRAPH_SCHEMA.md](GRAPH_SCHEMA.md) の ID 規則）。

- モジュールパスとパッケージの import パスは ID の材料ではなく列である。`go.mod` の `module` 行を変えても ID は変わらない。
- リポジトリ名が材料に入るので、別のリポジトリの同じ相対パス・同じ qualname は別ノードになる。全ノード表に `repo` カラムがある。
- ID は抽出結果から作るので、解決が失敗しても変わらない。
- `ID_PREFIX` は 1 つの DB に複数のグラフを置く次元、`repo` は 1 つのグラフの中でリポジトリを分ける次元である。

```
        Spanner DB / プロパティグラフ code_graph_a
  ┌────────────────────────────┐    ┌────────────────────────────┐
  │ repo = "web"               │    │ repo = "shared"            │
  │ モジュールごとの module     │    │ module =                   │
  │ モジュールの間も MethodCalls │    │   example.com/iryo/shared  │
  └─────────────┬──────────────┘    └──────────────▲─────────────┘
                │ Phase 1.6: ピアの相手は external（線なし）    │
                └── crossref: CrossRepoCalls / CrossRepoImports / CrossRepoRef ──┘
```

通常のエッジ表（`MethodCalls`、`PossiblyCalls`、`PackageImports`、`ClassEmbeds`、`ClassImplements`、`FileDependsOn`）がリポジトリの境界を越えることはありません。

## 4. ピア: Phase 1.6 でほかのリポジトリのソースを読む

ピアを渡さなければ、リポジトリは Phase 1.6 で孤立しています。別のリポジトリのモジュールへの呼び出しは、モジュールキャッシュの公開版を指す `external` になるか、オフラインで手に入らなければ `unresolved`（理由 `missing-module`）になります。ピアは `analyze --repos`（マニフェストの全リポジトリが互いにピア）、`--peer [name=]path`、`PEER_REPOS` で渡します（[README](../README.md#ピアリポジトリ)）。名前は、そのリポジトリを投入した（これから投入する）ときのリポジトリ名と同じにします。

ピアの規則は次のとおりです。

1. **発見。** ピアは自分のリポジトリと同じく、ファイルだけで発見する。
2. **置き換え。** グループのモジュールが、ピアのモジュールのパスを `go.mod` で require している（`// indirect` を含む）ときだけ、`replace <モジュールパス> => <ピアのモジュールの絶対パス>` を足す。足す先は、生成する `go.work` か、`-modfile` で使う go.mod のコピーである。リポジトリの中のファイルは書き換えない。require していないピアのモジュールを import するパッケージは、開発者のビルドと同じく読み込めず、その呼び出しは `unresolved` になる。ダウンロードモードでも同じである。
3. **置き換えないもの。** 次の場合は replace を足さず、警告に出す。
   - vendor したグループ。vendor のコピーが勝つ。
   - 2 つのピアが同じモジュールパスを持つ（フォーク）。ピアが無い場合と同じに解決する。
   - グループのいずれかのモジュールか、グループの `go.work` が、そのパスをすでに replace している（版付きでも、版なしでも）。開発者の replace が優先される。足すと `go.work` の replace が勝ち、開発者の指定を上書きしてしまう。
   - そのパスがこのリポジトリ自身のモジュールでもある。
   - ピアのモジュールの `go` 行がツールチェーンより新しい。足すとグループの読み込み全体が失敗するので、公開版のまま読む。
   - グループのワークスペースのモジュールそのもの。
4. **効果。** 読み込みは、require した版の代わりにピアのソースを読む。開発者が手元の `go.work` や replace でするのと同じである。
5. **ピアの依存。** ピアのモジュールの require も版の選択に加わる。オフラインでそれらの `.mod` が手に入らないと、ワークスペースのグループでは外部のパッケージがすべて読めなくなる。`doctor` と `analyze` は、置き換えたピアのモジュールごとに、オフラインで欠ける依存と、ツールチェーンより新しい依存の `go` 行を警告する。
6. **分類。** ピアのモジュールのディレクトリに着いた相手は `external` になり、`{repo, relpath, qualname}` を持つ（位置の分類は [LSP_RESOLUTION.md](LSP_RESOLUTION.md)）。

## 5. `crossref`: 投入後にリポジトリ間のエッジを引く

`python -m graph_generator crossref` は、投入済みの全リポジトリ（`OUTPUT_DIR/repos/*`）の `resolutions.json`、`entities.json`、`units.json`、`graph_meta.json` を読みます。導出は純関数でローカルに完結し、Spanner に触るのは書き込みだけです。投入済みのリポジトリが 1 つ以上あれば、`crossref_report.json` を毎回書きます（1 つだけのときは、ほかのリポジトリの相手がすべて `repo not ingested` で落ちます）。Spanner には、書くエッジか消すエッジがあるときだけ触れます（[前回のエッジの削除](#前回のエッジの削除)）。`--dry-run` は Spanner に書きません。

| ピアの相手 | 出力 |
|---|---|
| 呼び出しの相手がピアの関数・メソッド | `CrossRepoCalls`（`site_kind` 付き） |
| import の相手がピアのパッケージ | `CrossRepoImports`（参照元はファイル、参照先はパッケージ） |
| 埋め込みの相手がピアの型 | `CrossRepoRef`（kind `embeds`） |
| 実装する相手がピアのインターフェース | `CrossRepoRef`（kind `implements`） |

- 相手の `{repo, relpath, qualname}` から、Phase 8 と同じ `_make_id` でノード ID を計算する。名前で探すことはしない。
- 同じ表・同じ両端の組は 1 本にまとめる。
- 投入済みのリポジトリのモジュールパスを指していても、モジュールキャッシュ（と vendor）の相手は照合しない。そのコピーは公開版で、投入したソースではないからである。`crossref_report.json` の `module_cache` に、`--peer` を付けて再投入する案内付きで載る。そのモジュールを持つ投入済みのリポジトリが 2 つあれば、`two owners` として落とす。

エッジにしなかったものは黙って捨てず、コンソールと `OUTPUT_DIR/crossref_report.json` に理由付きで出します。

```json
{"stats": {"repos": ["shared", "web"], "external_records": 6, "call_edges": 2,
           "import_edges": 1, "ref_edges": 1, "module_cache": 1,
           "drops_by_reason": {"no owner": 0, "two owners": 0, "node missing": 0,
                               "repo not ingested": 0, "stale": 0}},
 "drops": [{"reason": "repo not ingested", "repo": "web", "source": "<relpath>:<line>:<col>",
            "target": "<repo>:<relpath>:<qualname>"}],
 "module_cache": [{"repo": "web", "source": "<relpath>:<line>:<col>",
                   "module": "example.com/iryo/textkit", "version": "v1.0.0", "owner": "shared",
                   "hint": "re-ingest web with --peer shared=<path> to edge into its source"}]}
```

この JSON は形を示す例で、数は実測値ではありません。

| `stats` のキー | 意味 |
|---|---|
| `repos` | 読んだリポジトリの名前 |
| `external_records` | 調べた `external` の記録の数。同じエッジになる記録も 1 件ずつ数える |
| `call_edges` / `import_edges` / `ref_edges` | 導出した `CrossRepoCalls` / `CrossRepoImports` / `CrossRepoRef` の本数 |
| `module_cache` | 照合しなかったモジュールキャッシュの相手の数（`module_cache` の件数） |
| `drops_by_reason` | 理由ごとの落とした件数 |

`drops` はサイトごとに 1 件です。`repo` は参照元のリポジトリ、`source` は参照元の位置（埋め込みと実装では `<relpath>:<型の qualname>`）、`target` は相手のラベルです。

| 理由 | 意味 | 対処 |
|---|---|---|
| `no owner` | 相手がどのリポジトリのものか記録されていない | 持ち主のリポジトリを投入し、ピアとして渡して再投入する |
| `two owners` | そのモジュールを持つ投入済みのリポジトリが 2 つ以上ある（フォーク） | 同じモジュールパスを 2 つのリポジトリに置かない。どちらかの投入をやめる |
| `node missing` | 持ち主のリポジトリはあるが、参照元か相手のノードが無い | 持ち主のリポジトリを最新のコードで再投入する |
| `repo not ingested` | ピアとして名前を付けたリポジトリがまだ投入されていない | そのリポジトリを同じ名前で投入する |
| `stale` | 参照元のファイル、または相手のピアのファイルの `resolutions.json` の SHA-256 が `entities.json` と違う。解決の途中でファイルが変わった。そのファイルの記録は `module_cache` にも載せない | 編集を終えてから、そのリポジトリを再投入し、`crossref` を再実行する |

どれかのリポジトリを再投入したら `crossref` を再実行します。ID が決定的なので再実行は冪等です。

### 前回のエッジの削除

`crossref` は Spanner に書くたびに、書いたエッジの主キーを表ごとに、書き込み先（`SPANNER_INSTANCE`、`SPANNER_DATABASE`、`GRAPH_NAME`、`ID_PREFIX`）と一緒に `OUTPUT_DIR/crossref_rows.json` に記録します。

- 同じ書き込み先への次の書き込みは、upsert のあと、前回の記録にあって今回は導かなかったエッジを主キーで消し、`[crossref] deleted N <table> edges it no longer derives` を出す。消すときの再試行は書き込みと同じである。
- 消し終わるまで、記録は前回と今回の両方の主キーを持つ。途中で止まっても、次の実行が残りを消す。
- 書き込み先が違えば何も消さず、記録を今回のもので置き換える。
- `--dry-run` は記録を読みも書きもしない。
- 記録が無いとき（初回、記録を消したあと）は何も消さない。読めない記録も何も消さず、コンソールに出して置き換える。

1 つのリポジトリの中の行を再投入で消す仕組み（`graph_prune.json`）は [README の鮮度とレジューム](../README.md#鮮度とレジューム) にあります。

## 6. グラフでの見え方

モジュールをまたぐ呼び出し（同じリポジトリ）:

```sql
GRAPH code_graph_a
MATCH (caller:Methods)-[e:MethodCalls]->(callee:Methods)
WHERE caller.repo = 'iryo-kanri-system' AND caller.module <> callee.module
RETURN caller.module, callee.module, COUNT(*) AS calls
GROUP BY caller.module, callee.module
ORDER BY calls DESC;
```

あるモジュールの関数を、ほかのどのモジュールが呼んでいるか:

```sql
GRAPH code_graph_a
MATCH (caller:Methods)-[e:MethodCalls]->(callee:Methods {module: 'example.com/iryo/billing'})
WHERE caller.module <> 'example.com/iryo/billing'
RETURN DISTINCT caller.module, caller.fqmn, callee.fqmn;
```

リポジトリをまたぐ呼び出し、import、参照:

```sql
GRAPH code_graph_a
MATCH (caller:Methods)-[x:CrossRepoCalls]->(callee:Methods)
WHERE callee.repo = 'shared'
RETURN caller.repo, caller.fqmn, callee.fqmn, x.site_kind;

GRAPH code_graph_a
MATCH (f:Files)-[i:CrossRepoImports]->(p:Packages)
RETURN i.source_repo, i.target_repo, p.import_path, COUNT(*) AS files
GROUP BY i.source_repo, i.target_repo, p.import_path
ORDER BY files DESC;

GRAPH code_graph_a
MATCH (src:Classes)-[r:CrossRepoRef]->(tgt:Classes)
RETURN r.source_repo, r.target_repo, r.kind, COUNT(*) AS refs
GROUP BY r.source_repo, r.target_repo, r.kind
ORDER BY refs DESC;
```

`validate` はリポジトリ別のノード数と、リポジトリの組ごとの結合度を表示します。リポジトリ別のノード数で、投入漏れやリポジトリ名の衝突に気付けます。

## 7. 検証ランブック

フィクスチャ `test_codes/multi_repo/`（F3）は 3 つのリポジトリです。`shared`（モジュール `example.com/iryo/shared` と入れ子の `example.com/iryo/textkit`）、`shared` を require する単一モジュールの `web`、`go.work` で 2 つのモジュールを束ねる `api` です。正解のリポジトリ間エッジの集合を `ground_truth/expected_cross_repo_edges.json` に持ちます。わざと間違えやすくした対照も入っています。

| 対照 | 期待 |
|---|---|
| 2 つのリポジトリにある同じモジュールパス（`textkit` のフォーク） | 置き換えない。`crossref` は結ばない |
| 宣言していないピアへの依存（`api/tools`） | 置き換えない。`unresolved` |
| vendor したピアのコピー（`api/legacy`） | vendor のコピーの `external` |
| 合成プロキシにも同じパスで公開されているピア（API が違う） | ピアの作業中のソースにだけあるメソッドに着く。置き換えが効かないと解決できない |
| 同じ名前の別の型 | リポジトリ内で解決する。名前でピアに結び付けない |
| ピアと同じパスを自分で replace するモジュール | ピアの replace を足さない |

上から順に実行します。件数の期待値と実測日は [LSP_RESOLUTION.md](LSP_RESOLUTION.md) の検証表が持ちます。

| # | 検証 | コマンド（`codedoc_go/` で実行） | 合格条件 |
|---|---|---|---|
| 1 | 正解データの整合性 | `python3 test_codes/_gt/check_ground_truth.py test_codes/multi_repo` | 終了コード 0 |
| 2 | 実行時の確認 | `python3 test_codes/_gt/run_oracle.py test_codes/multi_repo` | 終了コード 0。実行された全ケースが正解の相手に着く |
| 3 | 解決と導出（オフライン） | `python -m graph_generator evaluate --fixture multi_repo` | 誤り 0、リポジトリ間エッジが正解の集合と完全一致 |
| 4 | 解決と導出（env モード） | `python -m graph_generator evaluate --fixture multi_repo --env` | 同上 |
| 5 | 導出したエッジを見る | `python -m graph_generator evaluate --fixture multi_repo --dump-edges` | 書き出された `CrossRepoCalls`、`CrossRepoImports`、`CrossRepoRef` と `drops` を目で確かめられる |
| 6 | 自動テスト | `.venv/bin/python -m pytest -q --no-cov -k crossref` | 全件成功 |

自分のリポジトリで確かめるときは、依存される側から順に `analyze --graph --repo-name <name> --peer ...` で投入し、`crossref --dry-run` で導出結果と `crossref_report.json` の落とした理由を見てから `crossref` を実行します。

## 8. 制限

- ピアを渡さずに投入したリポジトリからは、リポジトリ間のエッジが引けない。名前による推測はしない。
- ピアのソースは、require した版の代わりに読まれる。開発者の手元に同じ replace が無ければ、実際のビルドとは違うソースを読むことになる。
- 2 つのピアが同じモジュールパスを持つとき（フォーク）と、vendor したグループでは、ピアを使わない。
- モジュールキャッシュのコピーは、投入したリポジトリのソースと照合しない。
- リポジトリ間のエッジは `crossref` を実行するまでグラフに現れない。通常のエッジ表にはリポジトリをまたぐ行が無いという前提でクエリを書ける。
- 再投入と `crossref` が消すのは、同じ書き込み先に前回書いたと記録した行のうち、今回書かなかった行だけである。別の書き込み先に書いた行と、記録が無いときの行（前回の `graph_rows/` や `crossref_rows.json` を消したあと）は残り、`validate` が孤立エッジとして報告する。
- webapp は一度に 1 つのリポジトリの Wiki を表示する（`WEBAPP_REPO` か `?repo=` で切り替える）。
