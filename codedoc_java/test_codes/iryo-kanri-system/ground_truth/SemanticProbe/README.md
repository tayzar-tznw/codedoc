# SemanticProbe — 束縛主張の実行時検証

`jp.iryo:semantic-probe:1.0.0`。9 個のライブラリアーティファクトすべてを
`../../_feed` から参照し、**製品コードを一切改変せずに** ground truth が
依拠する意味論(どのオーバーロード・どのパッケージ・どの宣言へ束縛されるか)を
実行時に確かめるコンソールプロジェクトです。

```bash
./build.sh
# 検証項目: 87 件
# SEMANTIC PROBE PASSED
```

失敗時は `検証失敗 #<番号>: <主張>` の `AssertionError` で停止します。
番号は成功時の出力順(上から数えた検証項目の位置)と一致します。

## 検証手段

| 手段 | 何を固定するか |
|---|---|
| 式の静的型(ボックス化した実行時クラス) | `calculate(288,146L)` が `Long` を生むこと = `(long,long)` 束縛 |
| `SerializedLambda` の束縛先記述子 | メソッド参照が選んだ実体 (`calculate(III)I` / `calculate([I)I` / `<init>()V`)。C# の `Delegate.Method` 相当 |
| リフレクション | 宣言クラス・戻り値型・`isDefault()`・コンストラクタ引数・`isRecord()` |
| jar 走査 | インタフェース実装数(Repository=1 / Exporter=2 / Notifier=2) |
| 標準出力の実測 | `SmsNotifier.send` や入れ子型 `Compactor.start` の到達先 |

## 検証区分

`Program` の各セクションが順に検証番号を進めます。

1. オーバーロード解決(拡大変換・厳密一致 vs 可変長引数・逓減・ラムダ形・null・ボックス化)
2. パッケージ間ツイン(`[記録]`/`[調剤]`、H判定/Rp判定、150/162、加算/乗算/+1000、領収書三兄弟、record/通常クラス、南北 Ward)
3. XPR(同一手続き行の分岐)
4. DI マイクロコンテナ(最後勝ち・登録順・コンストラクタ注入・ファクトリラムダ・未登録は例外)
5. 継承(スキップレベル・共変 `clone`・静的隠蔽・祖父母メンバ・コンストラクタ連鎖)
6. 既定インタフェース実装(既定 5% / 上書き 25% / interface static / `PaymentPolicy.super`)
7. 構造(`ChartArchive$Compactor`、`outer.new Janitor()`、実装数、ジェネリックアリティ、型ウィットネス)
8. 静的インポートと遮蔽
9. メソッド参照(`Logger::new` / `logger::flush` / `PointMath::toYen`)
10. 独自 fluent 列と `java.util.stream`

## 製品コードを写した補助クラス

製品側に手を入れないため、import 依存・可視性の都合で直接呼べない構造は
同形のソースを probe 内に置いて検証します。

- `iryo.probe.xpr.ReceptionSide` / `AdminSide`:
  `DailyCloseFlow.run()` と `DailyBatch.run()` の手続き行を写した対。
  **両ファイルの手続き行はバイト単位で同一**で、import 宣言とクラス名だけが違います。
  片方を編集したらもう片方も必ず合わせてください。
- `iryo.probe.shadow.ShadowSide`: `MaintenanceFlow` の静的インポート遮蔽
  (局所 `toYen` が `import static` を覆う)の写し。製品側の局所 `toYen` は
  `private static` かつ reception-desk はライブラリ配布されないため、
  probe のクラスパスからは呼び出せません。
- `Program.ReviewPolicy` / `Program.CombinedPolicy`: `AccountingFlow` 内の
  `PaymentPolicy.super.fee()` ダイヤモンドの写し(製品側は private 入れ子型)。

## ビルド設定

`--release 21`, `-parameters`, UTF-8, `-Xlint:all`(警告ゼロ)。compiler /
jar / resources / dependency の各プラグインをバージョン固定で使用します。
このプロジェクトは検証専用のため `_feed` へ deploy しません。
