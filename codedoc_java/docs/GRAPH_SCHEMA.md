# コードグラフのスキーマ

Spanner に何が入るかを説明します。5 つのノードテーブル、8 つのエッジテーブル、ID の作り方、ソースから行への対応、GQL の書き方です。
解決の仕組みは [LSP_RESOLUTION.md](LSP_RESOLUTION.md)、フェーズの流れは [ARCHITECTURE.md](ARCHITECTURE.md)、複数アーティファクトの識別と検証は [CROSS_PROJECT.md](CROSS_PROJECT.md)、接続先の設定は [../README.md](../README.md) に譲ります。

定義の出どころは `graph_generator/spanner_setup.py` (DDL) と `pipeline.py` の Phase 8〜10 (行の生成) です。

## ファイルでは答えられない問い

Wiki やソースを開けば「このクラスは何か」は分かります。次の問いはグラフでないと答えられません。

- このメソッドを呼んでいるのは誰か。`calculate(int, int)` と `calculate(int...)` を区別して。
- このインターフェースの呼び出しは実行時にどの実装へ行くか。
- このファイルを変えると、2 ホップ先までどのファイルが影響を受けるか。
- 別アーティファクトの同名クラスを混同せずに継承階層を辿りたい。

グラフは 1 本ずつのエッジに根拠 (呼び出し箇所のファイルと行、解決経路) を持たせ、GQL のパターンマッチで多段の関係を引けるようにしたものです。

## ノードテーブル (5)

プロパティグラフ `code_graph_a` の下に 13 テーブル (ノード 5 + エッジ 8) があります。

| テーブル | 1 行 | 主キー | 主な列 | 由来 | embedding |
|---|---|---|---|---|---|
| `Files` | ソースファイル | `file_id` | `file_name` `extension` `directory` `summary` | Phase 1 のスキャン。`summary` は Phase 2 の要約 (先頭 4,000 字) | あり |
| `Classes` | class / interface / enum / record / annotation (入れ子型も 1 行) | `class_id` | `name` `file_id` `kind` `modifiers` `namespace` `fq_symbol` `artifact` `decl_file` `decl_line` `summary` | Phase 1.6 の宣言 + tree-sitter の `kind` / `modifiers`。`summary` は `"<fq_symbol>: <kind>"` の短いラベル | あり |
| `Methods` | メソッド / コンストラクタ (record の正準コンストラクタ・アクセサを含む) | `method_id` | `name` `class_id` `file_id` `signature` `modifiers` `return_type` `namespace` `fq_symbol` `artifact` `member_kind` `decl_file` `decl_line` `summary` | Phase 1.6 の宣言。`summary` は `fq_symbol` と同じ | なし |
| `Modules` | LLM が付けたトピック | `module_id` | `name` `summary` | Phase 4 / 5。`summary` はトピック文書 (先頭 4,000 字) | あり |
| `Directories` | ディレクトリ | `dir_id` | `name` `summary` | Phase 1 のスキャン。`summary` は Phase 3 の要約 | なし |

- `kind` は `class` / `interface` / `enum` / `record` / `annotation`。`member_kind` は `method` / `constructor`。コンストラクタの `name` は `<init>`。
- `namespace` (Java パッケージ) / `fq_symbol` / `artifact` / `member_kind` / `decl_file` / `decl_line` は Phase 1.6 で解決されたファイルの行にだけ入ります。フォールバック行 (LSP 無効、`pom.xml` なし、ロード失敗) では NULL です。
- 解決済みの Methods 行は `signature` に `fq_symbol` が入り、`modifiers` / `return_type` は空文字です。フォールバック行は tree-sitter 由来の `"<戻り型> <クラス>.<名前>(<仮引数>)"` と `return_type` を持ちます。
- `embedding` 列は全テーブルにありますが、値が入るのは Files / Classes / Modules だけです (Phase 10)。

ノードの出どころは 2 系統です。

| 系統 | テーブル | 生成方法 | 性質 |
|---|---|---|---|
| 構造系 | Files, Classes, Methods, Directories | スキャン / tree-sitter / jdtls | 決定的。同じコードなら毎回同じ |
| 意味系 | Modules | LLM のトピック抽出 | 非決定的。実行ごとに名前や粒度が揺れる |

## エッジテーブル (8)

向きはプロパティグラフ定義の `SOURCE KEY` → `DESTINATION KEY` です。全テーブルが `edge_id` (主キー) + 始点列 + 終点列 (+ 補助列) の形です。

| テーブル | 向き (始点列 → 終点列) | 書くフェーズ | 由来 (根拠) | 投入 |
|---|---|---|---|---|
| `DirContainsFile` | Directory → File (`dir_id` → `file_id`) | 9 | `dir_tree` | あり |
| `FileDefinesClass` | File → Class (`file_id` → `class_id`) | 9 | Phase 1.6 の宣言 (解決済みファイル) / tree-sitter (フォールバック) | あり |
| `ClassDefinesMethod` | Class → Method (`class_id` → `method_id`) | 9 | 同上。`container` を辿って所属型を決める | あり |
| `FileDependsOn` | File → File (`source_file` → `target_file`) | 9 | ヒューリスティック 2 段。`import` 末尾の単純名とファイル basename の一致 (basename が重複するものは除外)、同一パッケージ内で `extends` / `implements` 句に書かれた型の定義ファイル | あり |
| `ClassInherits` (補助列 `kind` = `extends` / `implements`) | Class → Class (`child_class` → `parent_class`) | 9 | 解決済みファイルのみ。tree-sitter の `extends` / `implements` 句の単純名が Phase 1.6 の型宣言でちょうど 1 つに一致するときだけ。interface が extends する interface は `extends` | あり |
| `MethodCalls` (補助列 `callee_name` `target_symbol` `site_kind` `resolved_via` `call_file` `call_line`) | Method → Method (`caller_method` → `callee_method`) | 9 | Phase 1.6 で `resolved` になったサイトのみ。`static_target` があればそのインターフェースメンバーへの `lsp` エッジも張る。型レベルターゲット、`enclosing` なし、`ambiguous_symbols` は除外 | あり |
| `FileBelongsToModule` | File → Module (`file_id` → `module_id`) | 9 | LLM トピックの `linked_files` (basename 一致。重複する basename は対象外) | あり |
| `FileImports` | File → File (`source_file` → `target`) | なし | 予約枠。Phase 9 は書かない (import は FileDependsOn に集約) | なし |

Phase 9 は 7 種を並行に書きます。`FileDependsOn` は `(始点, 終点)` で、`MethodCalls` はエッジ ID で重複を防ぎます。

## ソースから行へ (例)

`iryo-billing` の `PointMath.java` と、それを呼ぶ `reception-desk` の `DailyCloseFlow.run()` を追います。

```java
// iryo-billing/src/main/java/iryo/billing/PointMath.java   (pom.xml の <artifactId> は iryo-billing)
package iryo.billing;

public final class PointMath {                       // 6 行目
    private PointMath() {                            // 7 行目
    }
    public static int toYen(int points) {            // 10 行目
        return points * 10;
    }
    public static int roundTo10(int yen) {           // 14 行目
        return (yen + 5) / 10 * 10;
    }
}
```

```java
// reception-desk/src/main/java/iryo/reception/flows/DailyCloseFlow.java   (artifactId は reception-desk)
import iryo.billing.PointMath;
...
    public static void run() {                       // 14 行目
        ...
        int yen = PointMath.toYen(15);               // 21 行目
```

**Phase 1.5 — tree-sitter のエンティティ** (`entities.json`、抜粋):

```json
{"file_path": ".../PointMath.java", "namespace": "iryo.billing", "imports": [],
 "classes": [{"name": "PointMath", "kind": "class", "modifiers": "public final",
              "base_classes": [], "interfaces": [],
              "methods": [{"name": "PointMath", "modifiers": "private", "return_type": "void", "parameters": "", "calls": []},
                          {"name": "toYen", "modifiers": "public static", "return_type": "int", "parameters": "int points", "calls": []},
                          {"name": "roundTo10", "modifiers": "public static", "return_type": "int", "parameters": "int yen", "calls": []}]}]}
```

`DailyCloseFlow.java` 側の `run` は `"calls": ["println", "new Logger", "log", "get", "lines", "new Validator", "validate", "toYen"]` を持ちますが、これは名前だけで解決先ではありません。

**Phase 1.6 — 宣言とサイト** (`resolution.json`、2026-09-18 の実行より):

```json
{"artifact": "iryo-billing", "symbol": "iryo.billing.PointMath",                "kind": "type",        "file": "iryo-billing/src/main/java/iryo/billing/PointMath.java", "line": 6,  "container": "iryo.billing",           "namespace": "iryo.billing"}
{"artifact": "iryo-billing", "symbol": "iryo.billing.PointMath.<init>()",       "kind": "constructor", "file": "...", "line": 7,  "container": "iryo.billing.PointMath", "namespace": "iryo.billing"}
{"artifact": "iryo-billing", "symbol": "iryo.billing.PointMath.toYen(int)",     "kind": "method",      "file": "...", "line": 10, "container": "iryo.billing.PointMath", "namespace": "iryo.billing"}
{"artifact": "iryo-billing", "symbol": "iryo.billing.PointMath.roundTo10(int)", "kind": "method",      "file": "...", "line": 14, "container": "iryo.billing.PointMath", "namespace": "iryo.billing"}

{"file": "reception-desk/src/main/java/iryo/reception/flows/DailyCloseFlow.java", "line": 21, "col": 28, "token": "toYen",
 "site_kind": "invocation", "enclosing": "iryo.reception.flows.DailyCloseFlow.run()", "enclosing_artifact": "reception-desk",
 "status": "resolved", "resolved_via": "lsp",
 "target": {"artifact": "iryo-billing", "symbol": "iryo.billing.PointMath.toYen(int)", "kind": "method", "file": ".../PointMath.java", "line": 10}}
```

**Phase 8 — ノード行** (`ID_PREFIX = a`):

| テーブル | 行 (主な列) |
|---|---|
| `Files` | `file_id = a_…` (絶対パスから)、`file_name = PointMath.java`、`extension = .java`、`directory = iryo-billing/src/main/java/iryo/billing` |
| `Classes` | `class_id = a_cc10dbfded8dd651` = `"a_" + sha256("class\|iryo-billing\|iryo.billing.PointMath")[:16]`、`name = PointMath`、`kind = class`、`modifiers = public final`、`namespace = iryo.billing`、`fq_symbol = iryo.billing.PointMath`、`artifact = iryo-billing`、`decl_line = 6` |
| `Methods` | `method_id = a_ac0ccb8b77277836` = `sha256("method\|iryo-billing\|iryo.billing.PointMath.toYen(int)")`、`name = toYen`、`class_id = a_cc10dbfded8dd651`、`signature = fq_symbol = iryo.billing.PointMath.toYen(int)`、`member_kind = method`、`decl_line = 10`。同様に `<init>()` (`member_kind = constructor`) と `roundTo10(int)` |
| `Methods` (呼び出し元) | `method_id = a_5b6b4b411bb5a47f` = `sha256("method\|reception-desk\|iryo.reception.flows.DailyCloseFlow.run()")` |

**Phase 9 — エッジ行**:

| テーブル | 行 | 条件 |
|---|---|---|
| `DirContainsFile` | `iryo/billing` ディレクトリ → `PointMath.java` | 常に |
| `FileDefinesClass` | `PointMath.java` → `PointMath` | 常に |
| `ClassDefinesMethod` | `PointMath` → `<init>()`、`toYen(int)`、`roundTo10(int)` (例: `edge_id = a_ccdc311cc8a8fbdb` = `sha256("cdm\|<class_id>\|<method_id>")`) | 常に |
| `MethodCalls` | `DailyCloseFlow.run()` → `PointMath.toYen(int)`。`edge_id = a_3c0089db0d42958c` = `sha256("calls\|<caller_id>\|<callee_id>")`、`callee_name = toYen`、`target_symbol = iryo.billing.PointMath.toYen(int)`、`site_kind = invocation`、`resolved_via = lsp`、`call_file = reception-desk/.../DailyCloseFlow.java`、`call_line = 21` | Phase 1.6 が `resolved` にしたときだけ |
| `FileDependsOn` | `DailyCloseFlow.java` → `PointMath.java` | import 末尾 `PointMath` に一致する basename のファイルがリポジトリ内に 1 つだけなら。コーパスには `iryo-pharmacy` にも `PointMath.java` があるので、この依存は張られない |
| `FileBelongsToModule` | `PointMath.java` → (LLM が付けたトピック) | LLM がそのファイルをトピックに紐づけたとき |

同じ行の `System.out.println(...)` と `ledger.lines().get(0)` は `external` (`java.base`) なので、ノードにもエッジにもなりません。

## ID の規則

すべての ID は `pipeline.py` の `_make_id` で作ります。

```
ID = "<ID_PREFIX>_" + sha256("part1|part2|...")[:16]
```

| ノード / エッジ | 材料 |
|---|---|
| File | `("file", 絶対パス)` |
| Directory | `("dir", 絶対パス)` |
| Module | `("module", トピック名)` |
| Class (解決済み) | `("class", artifact, fq_symbol)` |
| Method (解決済み) | `("method", artifact, fq_symbol)` |
| Class (フォールバック) | `("class", 絶対パス, クラス名)` |
| Method (フォールバック) | `("method", 絶対パス, クラス名, メソッド名)` |
| エッジ | 種別タグ + 両端 ID。タグは `calls` / `inherits` / `dep` / `fdc` / `cdm` / `fbm` / `dcf` |

解決済みノードの ID がパスを含まないので、同じ Maven プロジェクト内でファイルを移動しても ID は変わりません。`fq_symbol` は引数型を含むので、オーバーロードは別ノードです。同じ `(artifact, fq_symbol)` を持つ宣言が複数あっても 1 ノードで、メンバーの重複は `ambiguous_symbols` としてエッジから外れます。異なる `(artifact, fq_symbol)` が同じ ID に写像されたら Phase 8 は `RuntimeError` (「ノードID衝突を検出」) で止まります。

書き込みは `insert_or_update` です。同じコードを再解析すれば同じ ID になるので、再実行やレジュームで重複行はできません。削除はしないため、入力から消えた行は残ります。

## グラフ名と接続

プロパティグラフ名は `code_graph_a` です。`setup spanner` はこの名前で `CREATE OR REPLACE PROPERTY GRAPH` を実行します (`spanner_setup.py` に固定)。エージェントが GQL の先頭に付ける名前は `GRAPH_NAME` (既定 `code_graph_a`) です。

```bash
# gcloud から実行する例 (既定のインスタンス名と DB 名)
gcloud spanner databases execute-sql codedoc-java --instance=codedoc \
  --sql="GRAPH code_graph_a MATCH (c:Classes) RETURN COUNT(c) AS classes"
```

```sql
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '' ORDER BY TABLE_NAME;  -- 13 個
SELECT PROPERTY_GRAPH_NAME FROM INFORMATION_SCHEMA.PROPERTY_GRAPHS;                          -- code_graph_a
```

Spanner Studio (コンソール → Spanner → DB) でも同じ GQL を実行できます。`TO_JSON(パス)` を返すとグラフ可視化タブが出ます (例は [CROSS_PROJECT.md](CROSS_PROJECT.md))。

## GQL の書き方

形は `GRAPH code_graph_a MATCH (...)-[:エッジ]->(...) RETURN ...` です。`graph_query_agent` が書くクエリも同じ形です。

```sql
-- 件数
GRAPH code_graph_a
MATCH (f:Files) RETURN COUNT(f) AS files;

-- あるクラスのメソッド一覧
GRAPH code_graph_a
MATCH (c:Classes {name: 'FeeCalculator'})-[:ClassDefinesMethod]->(m:Methods)
RETURN m.name, m.fq_symbol;

-- 継承 (直接 + 間接、最大 5 段)
GRAPH code_graph_a
MATCH (child:Classes)-[:ClassInherits]->{1,5}(ancestor:Classes)
WHERE ancestor.name = 'Entity'
RETURN child.fq_symbol, child.artifact;

-- このメソッドを呼んでいるのは誰か (名前で)
GRAPH code_graph_a
MATCH (caller:Methods)-[c:MethodCalls]->(callee:Methods {name: 'calculate'})
RETURN caller.fq_symbol, callee.fq_symbol, c.call_file, c.call_line;

-- オーバーロードを fq_symbol で厳密に指定
GRAPH code_graph_a
MATCH (caller:Methods)-[c:MethodCalls]->(m:Methods)
WHERE m.fq_symbol = 'iryo.billing.FeeCalculator.calculate(int, int)'
RETURN caller.fq_symbol, caller.artifact, c.resolved_via;

-- 実行時 (DI) の配線だけ
GRAPH code_graph_a
MATCH (a:Methods)-[c:MethodCalls]->(b:Methods)
WHERE c.resolved_via = 'di'
RETURN a.fq_symbol AS caller, b.fq_symbol AS runtime_impl;

-- ファイルの依存先
GRAPH code_graph_a
MATCH (a:Files {file_name: 'AccountingFlow.java'})-[:FileDependsOn]->(b:Files)
RETURN b.file_name;

-- 影響範囲 (最大 3 ホップ)
GRAPH code_graph_a
MATCH (affected:Files)-[:FileDependsOn]->{1,3}(f:Files {file_name: 'Patient.java'})
RETURN DISTINCT affected.file_name;

-- モジュール (トピック) に属するファイル
GRAPH code_graph_a
MATCH (f:Files)-[:FileBelongsToModule]->(mod:Modules)
WHERE mod.name = '会計'
RETURN f.file_name;

-- 多段: ファイル → クラス → メソッド
GRAPH code_graph_a
MATCH (f:Files {file_name: 'AccountingFlow.java'})-[:FileDefinesClass]->(c:Classes)-[:ClassDefinesMethod]->(m:Methods)
RETURN c.name, m.name;
```

アーティファクト境界を越える呼び出しのクエリは [CROSS_PROJECT.md](CROSS_PROJECT.md) にあります。

## 表現していないもの

- JDK と外部 jar のシンボルはノードにならない。そこへの呼び出しは `resolution.json` に `external` として残るだけ。
- `FileDependsOn` はファイル粒度のヒューリスティック。同名 basename はスキップ、ワイルドカード import と static import は一致しない、別アーティファクトの同名パッケージを同一視する。
- `ClassInherits` は基底の単純名がリポジトリ内で一意のときだけ。基底が JDK / 外部 jar なら張らない。
- `Modules` と `FileBelongsToModule` は LLM 由来で、実行ごとに変わりうる。basename が重複するファイルはモジュールに紐づかない。
- `Methods` と `Directories` に埋め込みは無い。`FileImports` は空。
- `Classes` / `Methods` の `summary` は短い機械ラベルで、自然言語の要約は Files / Directories / Modules 側にある。
- Phase 1.6 が解決できなかった呼び出し (`unresolved` / `ambiguous`) とフォールバックしたファイルの呼び出し・継承はグラフに無い。
