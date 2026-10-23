# 複数アーティファクトにまたがるシンボルの識別

1 回の実行に複数の Maven アーティファクトが含まれるとき、同じ名前のクラス・メソッドを取り違えずに区別し、境界を越える呼び出しを正しく結ぶ方法と、その検証手順を説明します。
ID の計算式は [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md)、解決アルゴリズムとゲートの全体は [LSP_RESOLUTION.md](LSP_RESOLUTION.md)、コーパスのビルド手順と設定は [../README.md](../README.md) に譲ります。

## 何が問題か

現実の Java リポジトリでは同じ単純名の型やメソッドが複数の jar にあります。同梱コーパス `test_codes/iryo-kanri-system` (12 の独立 Maven プロジェクト、座標 `jp.iryo:<artifactId>:1.0.0`、プロジェクト間依存はすべて `_feed/` の jar 経由) は、それを意図的に踏むように作られています。

| 同名シンボル | 定義元 A | 定義元 B | 挙動の差 |
|---|---|---|---|
| `Logger.log(String)` | `iryo-kernel` の `iryo.kernel.logging.Logger` (`[記録]`) | `iryo-pharmacy` の `iryo.pharmacy.Logger` (`[調剤]`) | 出力書式 |
| `Validator.validate(String)` | `iryo-billing` (保険証記号 `H…` で true) | `iryo-pharmacy` (処方箋 `Rp` を含めば true) | 同じ引数で真偽が逆 |
| `PointMath.toYen(int)` | `iryo-billing` (×10 → 150 円) | `iryo-pharmacy` (×10.8 切上げ → 162 円) | 戻り値 |
| `calculate(int, int)` | `iryo-billing` の `FeeCalculator` (加算) | `iryo-pharmacy` の `DoseCalculator` (乗算)、`iryo-legacy-claims` の `PointTable` (+1000) | 戻り値 |
| `ReceiptFormatter.format(Object)` | `iryo-billing` | `iryo-legacy-claims`、`iryo-interop` | `[会計]` / `[旧レセ]` / `[連携]` |
| `Ward.locate()` | `iryo-domain` の `iryo.domain.north` | `iryo-domain` の `iryo.domain.south` (同一アーティファクト内) | 文字列 |
| `Money` | `iryo-domain` (record) | `iryo-pharmacy` (通常クラス) | 型の種類 |

決定打は XPR 対です。`reception-desk` の `DailyCloseFlow.run()` と `admin-console` の `DailyBatch.run()` は、見出しの `println` を除いて処理行がバイト単位で同一です。

```java
// reception-desk/.../DailyCloseFlow.java            // admin-console/.../DailyBatch.java
import iryo.billing.Validator;                       import iryo.pharmacy.Logger;
import iryo.billing.PointMath;                       import iryo.pharmacy.Validator;
import iryo.kernel.logging.Logger;                   import iryo.pharmacy.PointMath;
...                                                  ...
        var ledger = new Logger();                           var ledger = new Logger();
        ledger.log("日次点検");                               ledger.log("日次点検");
        var check = new Validator();                         var check = new Validator();
        System.out.println("様式判定: " + check.validate("H12345678"));     // 両方とも同一
        int yen = PointMath.toYen(15);                       int yen = PointMath.toYen(15);
```

実行結果は受付が `[記録]` / `true` / `150円`、管理が `[調剤]` / `false` / `162円` です。テキストをいくら見ても区別できず、import 文脈を知るコンパイラだけが答えを持ちます。

だからキーの取り方が決まります。

- ファイルパスをキーにすると、ファイル移動で ID が変わり、jar 越しのターゲット (ソースツリーの外) を表せない。
- 単純名や `クラス名.メソッド名` をキーにすると、上の表のすべてが 1 ノードに潰れ、「最初に見つかった同名」に繋ぐ誤エッジが量産される。

## 識別キー — artifact + fq_symbol

クラスとメソッドの識別子は `artifact` (Maven artifactId) と `fq_symbol` (引数型付き完全修飾シンボル) の組です。Spanner の `Classes` / `Methods` 行はこの 2 列に加えて `namespace` / `decl_file` / `decl_line` を持つので、「どのアーティファクトのどのファイル何行目か」を常に列で読めます。ハッシュの式は [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md) を参照してください。

`fq_symbol` はパッケージと引数型を含むので、`iryo.domain.north.Ward` と `iryo.domain.south.Ward`、`calculate(int, int)` と `calculate(int...)` は別ノードです。`artifact` は、仮にパッケージ名まで衝突しても別ノードにするための成分です。

### 呼び出し元の artifact

`.java` ファイルは最寄りの祖先 `pom.xml` の `<artifactId>` に帰属します (`resolution.py` の `discover_projects`)。この値が `resolution.json` の `enclosing_artifact` に入り、Phase 9 は呼び出し元ノードをこれで選びます。同名メソッドが複数アーティファクトにあっても呼び出し元を取り違えません。

### 呼び出し先の artifact

`textDocument/definition` の答えは 2 系統です。

1. **ソースツリー内の `file://`**: そのファイルが帰属する artifact の宣言索引で位置を引きます。解析対象外の `file://` は候補にしません。
2. **`jdt://`** (jar と JDK): URI のクエリに Maven の座標が埋め込まれています。

```text
jdt://contents/mini-lib-1.0.0.jar/jp.iryo.spike.lib/Calculations.java?=mini-app/…=/maven.groupId=/jp.iryo.spike=/=/maven.artifactId=/mini-lib=/=/maven.version=/1.0.0=/…
jdt://contents/java.base/java.util/List.java?=mini-app/…jrt-fs.jar`java.base=/…
```

`_artifact_from_jdt_uri` はこの順で決めます。

| URI の特徴 | artifact |
|---|---|
| GAV がソースツリー内のいずれかの `pom.xml` と一致 (`gav_map`) | その `artifactId`。これが決定打 |
| GAV はあるがツリー外 | URI の `maven.artifactId` |
| GAV が無く、コンテナが jar でない (JRT) | モジュール名 (`java.base`、`jdk.httpserver`、`java.desktop`) |
| それ以外の jar | バージョン接尾辞を除いた jar 名 (`foo-1.2.3.jar` → `foo`) |

続けて `java/classFileContents` で本文 (添付ソースか逆コンパイル結果) を取り、ソースツリーと同じ宣言索引にかけて正確なオーバーロードまで特定します。artifact がツリー内なら `(artifact, fq_symbol)` をソースツリーの実宣言に写像し直し、`target.file` / `line` は実ソースを指します。ツリー外なら `status = "external"` として `artifact` と `symbol` を記録するだけで、ノードには繋ぎません。

2026-09-18 の実行では `DailyCloseFlow.java:21` の `PointMath.toYen(15)` が `iryo-billing-1.0.0.jar` 越しに `jdt://` で返り、GAV `jp.iryo:iryo-billing:1.0.0` から `iryo-billing/src/main/java/iryo/billing/PointMath.java` の 10 行目に写像されました。統合テスト `test_cross_jar_site_maps_back_to_the_in_repo_declaration` がこれを固定しています。

### 取り違えに直接効くゲート

全ゲートの一覧は [LSP_RESOLUTION.md](LSP_RESOLUTION.md) にあります。アーティファクトの取り違えに直接効くのは次の 3 つです。

- 呼び出し元は `enclosing_artifact` で帰属させ、シンボル名だけで別アーティファクトのメンバーに帰属させない。
- 同一 artifact 内で `fq_symbol` が重複する宣言は `ambiguous_symbols` に隔離し、エッジから外す。
- 異なる `(artifact, fq_symbol)` が同じノード ID に写像されたら Phase 8 は `RuntimeError` で止まる。

## グラフでの見え方

2026-09-18 の全コーパス実行 (製品コード 12 プロジェクト) で、`resolved` かつメンバーをターゲットとするサイトのうち `enclosing_artifact ≠ target.artifact` のものは 117 サイト、アーティファクトの組は 19 でした。Spanner の `MethodCalls` は同じ呼び出し元 → 先を 1 本に畳むので、エッジ数はこれより少なくなります。

XPR 対の同一行は import どおりに分岐しました。

```text
DailyCloseFlow.java:16  new Logger()          reception-desk → iryo-kernel    iryo.kernel.logging.Logger.<init>()
DailyCloseFlow.java:17  ledger.log("…")       reception-desk → iryo-kernel    iryo.kernel.logging.Logger.log(String)
DailyCloseFlow.java:19  new Validator()       reception-desk → iryo-billing   iryo.billing.Validator (型)
DailyCloseFlow.java:20  check.validate("…")   reception-desk → iryo-billing   iryo.billing.Validator.validate(String)
DailyCloseFlow.java:21  PointMath.toYen(15)   reception-desk → iryo-billing   iryo.billing.PointMath.toYen(int)

DailyBatch.java:16      new Logger()          admin-console  → iryo-pharmacy  iryo.pharmacy.Logger (型)
DailyBatch.java:17      ledger.log("…")       admin-console  → iryo-pharmacy  iryo.pharmacy.Logger.log(String)
DailyBatch.java:19      new Validator()       admin-console  → iryo-pharmacy  iryo.pharmacy.Validator (型)
DailyBatch.java:20      check.validate("…")   admin-console  → iryo-pharmacy  iryo.pharmacy.Validator.validate(String)
DailyBatch.java:21      PointMath.toYen(15)   admin-console  → iryo-pharmacy  iryo.pharmacy.PointMath.toYen(int)
```

「(型)」は明示コンストラクタの無い型への `new` で、ノードの同定には効きますが `MethodCalls` にはなりません。単純名 `calculate` は宣言索引上 7 つの別シンボル (iryo-billing 5、iryo-legacy-claims 1、iryo-pharmacy 1) で、`AccountingFlow.java` の 6 呼び出しは iryo-billing の 5 オーバーロードへ 1 対 1 で解決されています。

境界を越える呼び出しの GQL:

```sql
-- (1) アーティファクト間の呼び出し行列
GRAPH code_graph_a
MATCH (a:Methods)-[:MethodCalls]->(b:Methods)
WHERE a.artifact <> b.artifact
RETURN a.artifact AS src, b.artifact AS dst, COUNT(*) AS n
GROUP BY src, dst ORDER BY n DESC

-- (2) 他アーティファクトから iryo-kernel の log を呼んでいるのは誰か (iryo.pharmacy.Logger が混入しないこと)
GRAPH code_graph_a
MATCH (a:Methods)-[:MethodCalls]->(b:Methods)
WHERE b.artifact = 'iryo-kernel' AND b.name = 'log' AND a.artifact <> 'iryo-kernel'
RETURN a.artifact AS src, a.fq_symbol AS caller, b.fq_symbol AS callee

-- (3) 特定プロジェクトの外向き依存だけ
GRAPH code_graph_a
MATCH (a:Methods)-[c:MethodCalls]->(b:Methods)
WHERE a.artifact = 'reception-desk' AND b.artifact <> 'reception-desk'
RETURN b.artifact, b.fq_symbol, c.resolved_via
```

Spanner Studio では `TO_JSON(パス)` を返すとグラフとして描画されます (ノードのラベルは `name`、クリックで `fq_symbol` / `artifact` / `decl_file` / `decl_line`)。

```sql
-- (V1) 境界を越える全エッジ (重ければ LIMIT を付ける)
GRAPH code_graph_a
MATCH p = (a:Methods)-[c:MethodCalls]->(b:Methods)
WHERE a.artifact <> b.artifact
RETURN TO_JSON(p) AS path

-- (V2) XPR 対のサブグラフ。同一行の run() 2 本が別アーティファクトへ分岐する
GRAPH code_graph_a
MATCH p = (a:Methods)-[c:MethodCalls]->(b:Methods)
WHERE a.fq_symbol IN ('iryo.reception.flows.DailyCloseFlow.run()', 'iryo.admin.DailyBatch.run()')
RETURN TO_JSON(p) AS path
```

同名メンバーが別ノードに分かれていることの棚卸しは通常の SQL で行います。

```sql
SELECT m.artifact, m.name, COUNT(*) AS overloads, ARRAY_AGG(m.fq_symbol ORDER BY m.fq_symbol) AS symbols
FROM Methods m
GROUP BY m.artifact, m.class_id, m.name
HAVING COUNT(*) > 1
ORDER BY overloads DESC, m.name
```

## 検証ランブック (`test_codes/iryo-kanri-system`)

上から順に実行します。各層は独立した証拠です。件数の期待値と実測日は [LSP_RESOLUTION.md](LSP_RESOLUTION.md) の検証表が持ちます。

| # | 検証 | コマンド | 合格条件 |
|---|---|---|---|
| 0 | 前提 | `java -version` / `mvn -version` / `JAVA_LSP_CMD` の jdtls | JDK 21、Maven 3.9 以上、jdtls が起動する |
| 1 | コーパスのビルド | [../README.md](../README.md) の「同梱コーパスで Phase 1.6 を確認する」の順に 12 プロジェクトの `build.sh` を実行 | 全ビルド成功。`_feed/jp/iryo/` に 9 ライブラリ。コンソール 2 つが `=== 受付業務デモ完了 ===` / `=== 管理タスク完了 ===` を表示。javac 警告は `AccountingFlow` の deprecation 1 件のみ |
| 2 | 実行時の束縛検証 | `ground_truth/SemanticProbe/build.sh` | `SEMANTIC PROBE PASSED` が出る |
| 3 | Phase 1.6 の実行 | `python -m graph_generator generate graph test_codes/iryo-kanri-system` (Spanner を使わず解決だけ見るなら `resolution.run_resolution(target_dir, java_files, output_dir)` を直接呼ぶ) | `resolution.json` の `stats` で `solutions_fallback` が 0、`ambiguous` が 0 |
| 4 | グラフの整合 | `python -m graph_generator validate` | 12 テーブルの件数表示、孤立チェック 3 種がすべて `OK` |
| 5 | GQL の実査 | 上の (1)〜(3) と (V2) | (2) に `iryo.pharmacy.Logger` が混入しない。(V2) で 2 本の `run()` のエッジ先が別アーティファクト |
| 6 | 自動テスト | `pytest -q -m "not integration"` / `pytest -q -m integration` | 全件 green |
| 7 | 採点 | `python -m graph_generator score test_codes/iryo-kanri-system` | 現状は不合格 (終了コード 1)。正解マニフェストが C# 版のままで未移植。移植後は「誤答 0」ゲートが取り違えを直接判定する |

CLI でコーパス全体を指定すると `ground_truth/SemanticProbe` も解析対象に入ります ([../README.md](../README.md) の「同梱コーパスで Phase 1.6 を確認する」を参照)。

わざと壊して確かめる方法もあります。

- `.env` で `JAVA_LSP_ENABLED=false` にし、新しい DB (または別の `ID_PREFIX`) へ `generate graph` → ノードのみ生成され、`MethodCalls` / `ClassInherits` は 0 件になる (名前推測に退行しない。既存 DB では前回の行が残る)。
- `rm -rf _feed _m2` の直後に `reception-desk/build.sh` → `jp.iryo:iryo-kernel:jar:1.0.0` を解決できず失敗する (意図した失敗経路)。同じ状態で Phase 1.6 を走らせると、classpath に無い他アーティファクトへの呼び出しは `unresolved` に倒れ、誤ったエッジは作られない (`~/.m2` に古い jar が残っていれば、そちらが解決されうる)。

## 自分のリポジトリで確かめる

1. 各プロジェクトに `pom.xml` があり、依存ライブラリが `mvn install` (または deploy) でローカルリポジトリに入っている状態にする。
2. jdtls を導入し、PATH に無ければ `JAVA_LSP_CMD` に絶対パスを書く。`~/.m2` と分離したリポジトリを使うなら `JAVA_LSP_MAVEN_SETTINGS` も指定する。
3. `generate graph <dir>` を実行し、`resolution.json` の `stats` と `solutions[].status` を見る。`fallback` の `reason` に失敗理由が入る。
4. 同名シンボルの分離は `SELECT name, fq_symbol, artifact FROM Methods WHERE name = '<よくある名前>'` で見るのが早い。
5. 「なぜこのエッジになったか」は `sites[]` の `enclosing_artifact` / `status` / `target` / `static_target` / `resolved_via` で遡れる。

## 制限

- 1 セッションは「祖先方向に連続する `pom.xml` の最上位」単位です。親 POM がツリー内の祖先ディレクトリにある入れ子構成は 1 セッションで読み、各ファイルは自分のモジュールの `artifactId` に帰属します。`<modules>` や `<relativePath>` は読みません。親 POM がツリー外にある構成は、その子 pom 自体がセッションのルートになります。コーパスには親 POM も reactor も無いため、この経路は統合テストで検証されていません。
- ソースツリー内で `artifactId` が重複すると区別できません。`gav_map` は座標を artifactId に写像し、宣言レジストリは artifactId をキーにするため、同じ artifactId を持つ 2 つの pom の宣言は 1 つの artifact として合算されます。同じ `fq_symbol` のメンバーがあれば `ambiguous_symbols` に落ち、無ければ別プロジェクトの宣言が同じラベルで混ざります。
- ツリー外のアーティファクト (JDK、外部 jar) はノードになりません。`external` として `artifact` ラベルと `symbol` が記録されるだけで、`symbol` の綴りは添付ソースか逆コンパイル表示に依存するので、照合は `artifact` 単位で行います。
- 同じ単純名の interface が複数のアーティファクトにあると、インターフェース → 実装の付け替えは行いません。
