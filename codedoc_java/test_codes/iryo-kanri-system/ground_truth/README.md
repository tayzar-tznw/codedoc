# ground_truth — シンボル解決の正解データ

製品コード(`../iryo-*` の 9 ライブラリ、`../reception-desk`、`../admin-console`、
`../patient-portal` の 12 Maven プロジェクト)には**タグもアサーションも一切ない**。
解決の正解はすべてこのディレクトリが持ち、製品コードを編集しても
ここから再構築できる。

## STATUS(2026-09-17 時点)— manifest 系は C# 版のまま、有効なゲートは SemanticProbe のみ

コーパスは Java/Maven へ移植済みだが、manifest 系 4 点はまだ C# 期の成果物で、
参照用に残している。**移植が済むまで `generate_manifest.py` は実行しないこと**
(manifest を上書きする)。

| 成果物 | 状態 |
|---|---|
| `SemanticProbe/` | **Java 版・有効**。`./SemanticProbe/build.sh` → 「検証項目: 87 件」「SEMANTIC PROBE PASSED」 |
| `cases.py` | C# 版のまま。106 ケース、`Iryo.ReceptionDesk/…/*.cs` などの C# パス。`DECL_PINS` / `STATIC_TARGET_PINS` も C# 宣言を指す |
| `generate_manifest.py` | C# 版のまま。`SYMBOL_GRAMMAR` は C# 文法、`PROJECTS` 表は `Iryo.*` → via nuget/source |
| `expected_resolutions.json` | version 2、106 ケース、ターゲットは `assembly` キー、via = nuget 97 / source 6 / bcl 1 |
| `check_ground_truth.py` | 実行はできるが、現在は **106 件の「ファイルがない」不一致で終了コード 1** |
| `graph_generator/scorer.py`(`python -m graph_generator score`) | Java 版 v3 スキーマ(後述)を前提。manifest が v2 のため、移植完了まで受け入れゲートとして使えない |

## 構成と流れ(移植後に復活する形)

```
cases.py                   … 記述元 (source of record)。ファイル + 一意スニペットで
                             呼び出し箇所を指し、期待シンボルを宣言する(行番号を持たない)
        │ python3 generate_manifest.py
        ▼
expected_resolutions.json  … 生成物(コミット対象)。行番号・宣言アンカー・
                             distractors・today(現行パイプラインの挙動)付き
        │ python3 check_ground_truth.py
        ▼
終了コード 0               … 独立検証(往復・スニペット一意性・アンカー・全単射)
        │ python -m graph_generator score test_codes/iryo-kanri-system
        ▼
受け入れゲート             … 完全性 > 85% / 誤答 0 / ノード被覆率 ≥ 0.85 / エッジ解決率 ≥ 0.85
```

## SemanticProbe — 実行時検証(有効)

`SemanticProbe/`(`jp.iryo:semantic-probe:1.0.0`、自己完結のコンソールプロジェクト)が、
製品コード無改変のまま主要な束縛主張 **87 件**を実行時に検証する。`../_feed` の
9 ライブラリ JAR だけを参照し、自身は `_feed` へ deploy しない。

```bash
./SemanticProbe/build.sh
# 検証項目: 87 件
# SEMANTIC PROBE PASSED
```

検証手段(詳細は [SemanticProbe/README.md](SemanticProbe/README.md)): 式の静的型
(ボックス化した実行時クラス)、`SerializedLambda` の束縛先記述子、リフレクション
(宣言クラス・戻り値型・`isDefault()`・`isRecord()`)、jar 走査(インタフェース実装数)、
標準出力の実測。XPR 対は `iryo.probe.xpr.ReceptionSide` / `AdminSide` に写してあり、
製品側と同じく手続き行はバイト単位で同一に保つ。

## テストコード編集後の必須手順

今日有効なのは SemanticProbe だけ:

```bash
./SemanticProbe/build.sh            # SEMANTIC PROBE PASSED が必須
```

manifest の Java 移植後は、従来どおり次を続ける:

```bash
python3 generate_manifest.py        # スニペット再探索で行番号を更新
python3 check_ground_truth.py       # 0 終了が必須(ずれ・重複・アンカー切れを検出)
```

スニペットが一意でなくなった(0 件または複数件になった)場合は generate が
ケース ID・ファイル・スニペット付きで即座に失敗する。その場合は
`cases.py` のスニペットを編集後のコードに合わせて更新する。

## ケース ID の体系

C# 版 manifest の 17 区分のうち、Java へ引き継ぐ区分と、C# 固有のため
[docs/PROJECTS.md](../docs/PROJECTS.md) の Java シナリオ区分へ置き換える区分に分かれる。

| 接頭辞 | 対象 | Java 移植 |
|---|---|---|
| OVL | オーバーロード解決(拡大変換対ボックス化・固定引数対 varargs・ラムダ目標型・null 最詳細型) | 引き継ぐ |
| TWIN | アーティファクト間の同名クラス/同名メソッド(オーバーロードではない) | 引き継ぐ |
| DI | 依存性注入(class literal 登録・最後勝ち・登録順・コンストラクタ注入) | 引き継ぐ |
| DIM | interface 既定メソッド(既定・上書き・`PaymentPolicy.super`) | 引き継ぐ |
| BND | コンパイル済み JAR 境界(ReceiptFormatter 三兄弟・旧点数表) | 引き継ぐ |
| NSP | import と名前探索の罠(wildcard・single-type・`java.util.List` 対 `java.awt.*`・完全修飾名) | 引き継ぐ |
| GEN | ジェネリクス(アリティ・構築型・型 witness・別 package の同名ジェネリクス) | 引き継ぐ |
| INH | 継承ディスパッチ(祖父母宣言・上書き・共変戻り値・static hiding) | 引き継ぐ |
| UNR | 単一解を持たないもの(reflection の候補集合を推測しない) | 引き継ぐ |
| VAR | `var` 連鎖(型推論の連鎖) | 引き継ぎ候補。null 条件呼び出しは Java に無く、PROJECTS.md では独立区分になっていない |
| XPR | **プロジェクト間分岐対**(同一手続き行が別アーティファクトへ束縛: XPR-00x 受付 ⇔ XPR-10x 管理) | 引き継ぐ |
| LNQ / EXT / EIF / DLG / BLD / WEB | LINQ・拡張メソッド・明示的インタフェース実装・委譲/イベント・C# パーサ盲点・Razor PageModel | C# 固有。IFI(実装が一意なら具体化、2 個なら推測しない)・STR(独自 fluent と `java.util.stream`)・MRF(メソッド参照/コンストラクタ参照)・REC(record の正準コンストラクタ/accessor)へ置き換える |

## manifest スキーマ(移植先: Java v3、1 ケース)

`graph_generator/scorer.py` が突き合わせる形(`tests/test_scorer.py` の合成ケースと同じ)。
判定はフィールドだけに依存し `version` 値は読まないが、語彙は次を前提とする。

- `artifact`: Maven artifactId(`iryo-kernel` など)または JDK モジュール名(`java.base` など)。
  C# 版の `assembly` を置き換える(`project` も artifactId)
- `via`: `source`(呼び出し元と同じプロジェクト内)/ `maven`(`_feed` の JAR 越し、ソースは
  リポジトリ内)/ `jdk`(リポジトリ外。ノード被覆率の分母に入らない)
- `symbol`: `graph_generator/java_decls.py` が定める Java シンボル文法。package 修飾の型名、
  入れ子型は `$`、コンストラクタは `<init>`、仮引数型は単純名(配列・varargs・wildcard・
  ジェネリック引数はソース表記を保持)。例: `iryo.billing.FeeCalculator.calculate(int, int)`,
  `iryo.kernel.logging.Logger.<init>(String)`, `iryo.domain.ChartArchive$Compactor.start()`
- `expected.resolution`: `resolved` / `external` / `ambiguous`(C# の `runtime` は dynamic 用で
  Java では使わない)
- 完全性のしきい値は `case_count` から導出: 正解 ≥ floor(0.85 × case_count) + 1(106 件なら 91)

```jsonc
// ファイル先頭: {"version": 3, "root": "test_codes/iryo-kanri-system",
//               "language_note": "…", "symbol_grammar": "…", "case_count": N, "cases": [ … ]}
{
  "id": "XPR-002",
  "category": "cross-project-divergence",
  "file": "reception-desk/src/main/java/iryo/reception/flows/DailyCloseFlow.java",
  "line": 17,                       // generate が常に再計算
  "token": "log",                   // 行内で解決対象となる字句
  "site_kind": "invocation",        // invocation / object_creation / class_literal / method_reference
  "snippet": "ledger.log(\"日次点検\")",   // ファイル内で一意
  "source_line": "…",               // 実ソース行(検証用)
  "expected": {
    "resolution": "resolved",
    "targets": [{
      "symbol": "iryo.kernel.logging.Logger.log(String)",
      "kind": "method",             // type / constructor / method
      "project": "iryo-kernel",
      "artifact": "iryo-kernel",
      "via": "maven",
      "file": "iryo-kernel/src/main/java/iryo/kernel/logging/Logger.java",
      "line": 22                    // 宣言行(check が実在とメンバー名を検証)
    }],
    "static_target": { … },         // DI ケース等: 純粋な定義へ移動の答え(インタフェース宣言)
    "runtime_target": { … }         // 上書きケース: 実行時ディスパッチ先
  },
  "distractors": ["iryo.pharmacy.Logger.log(String)"],   // 同名の紛らわしい別シンボル(最大 8)
  "today": {                        // 現行 graph_generator の挙動スナップショット(陳腐化前提)
    "failure_mode": "…",
    "note": "…"
  },
  "why_hard": "…"                   // なぜ構文解析だけでは解けないか(日本語)
}
```

契約: `expected.*` は Java コンパイラ(javac / JLS)の意味論であり**安定**。`today.*` は
現行パイプラインのスナップショットであり、パイプラインが改善されたら
陳腐化してよい(正解は変わらない)。
