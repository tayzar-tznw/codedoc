# プロジェクト解説 — 医療管理システム Java コーパス

12 個の Maven プロジェクトの役割、主要型、依存関係、および
シンボル解決上の確認点をまとめます。依存はすべて `_feed/` の
コンパイル済み JAR を通り、Maven reactor や相対モジュール参照はありません。

## 依存関係図

```mermaid
graph TD
    subgraph アプリケーション
        Reception[reception-desk<br>受付コンソール]
        Admin[admin-console<br>管理コンソール]
        Portal[patient-portal<br>患者ポータル]
    end
    subgraph 業務ライブラリ
        Data[iryo-data]
        Billing[iryo-billing]
        Pharmacy[iryo-pharmacy]
        Scheduling[iryo-scheduling]
        Reporting[iryo-reporting]
    end
    subgraph 基盤・独立系
        Kernel[iryo-kernel]
        Domain[iryo-domain]
        Legacy[iryo-legacy-claims]
        Interop[iryo-interop]
    end
    Domain --> Kernel
    Data --> Kernel & Domain
    Billing --> Kernel & Domain
    Pharmacy --> Kernel & Domain
    Scheduling --> Kernel & Domain
    Reporting --> Kernel & Domain
    Reception --> Kernel & Domain & Data & Billing & Pharmacy & Scheduling & Reporting & Legacy & Interop
    Admin --> Kernel & Domain & Billing & Pharmacy & Reporting
    Portal --> Kernel & Domain & Scheduling & Billing
```

## 基盤・独立系

### iryo-kernel — 共通基盤

依存なし。`Logger` のコンストラクタ連鎖と `log` オーバーロード、
`Notifier`、既定メソッドを持つ `PaymentPolicy`、引数検証の `Guard`、
小規模 DI の `ServiceCollection` / `ServiceProvider` を提供します。
通知実装は同アーティファクトの `MailNotifier` と、予約側の
`SmsNotifier` にまたがります。

### iryo-domain — ドメインモデル

依存: `iryo-kernel`。`Entity → AuditedEntity → Patient` の三段継承、
スキップレベルの `describe()`、共変 `clone()`、静的 `create()` の隠蔽を
持ちます。`Money` は `BigDecimal` を成分に持つ record、薬局側の
`Money` は通常クラスです。南北の `Ward` は別パッケージ・別ファイルの
同名型です。`ChartArchive` は static 入れ子型 `Compactor` と
非 static 内部型 `Janitor` を持ちます。

### iryo-legacy-claims — 旧レセコン互換

依存なし。非推奨の `PointTable` と移行名 `OldTable`、単一値版
`ChartCache<T>`、`[旧レセ]` 書式の `ReceiptFormatter` を提供します。

### iryo-interop — 外部連携

依存なし。`PatientIdKit.normalize()`、`[連携]` 書式の
`ReceiptFormatter`、`Exporter` とその 2 実装 `CsvExporter` /
`XmlExporter` を提供します。`Exporter` は実装が一意ではない場合の
解決保留を確認する対です。

## 業務ライブラリ

### iryo-data — データアクセス

依存: `iryo-kernel`, `iryo-domain`。`Repository<T>` の実装は
`SqlPatientRepository` だけで、ロガーをコンストラクタ注入します。
`RepositoryFactory.createDefault()` は型推論連鎖の起点です。
`ChartCache<K,V>` と `Registry.<T>create(Class<T>)` も提供します。

### iryo-billing — 会計・診療報酬

依存: `iryo-kernel`, `iryo-domain`。`FeeCalculator.calculate` の
5 オーバーロードは完全一致、プリミティブ拡大変換、固定引数と varargs の
優先順位を分けます。`route(long)` / `route(Integer)` は拡大変換と
ボックス化のフェーズ差を示します。`run(Runnable)` / `run(Supplier<String>)`,
`scale` 2 種、`bill(String)` / `bill(Object)`, `pay(BigDecimal)` も
呼び出し形ごとに宣言が分かれます。`PointMath`, `Validator`,
`ReceiptFormatter` は薬局・旧式・連携側に同名の対応物があります。

### iryo-pharmacy — 薬局

依存: `iryo-kernel`, `iryo-domain`。調剤書式の `Logger`、処方箋用
`Validator`、院内レートの `PointMath`、乗算を行う `DoseCalculator`,
通常クラスの `Money` を持ちます。会計側と同じ単純名・同じ引数形でも
業務結果が異なります。

### iryo-scheduling — 予約

依存: `iryo-kernel`, `iryo-domain`。`AppointmentBook.reserve()` は
`Appointment` を返します。`SmsNotifier` は別アーティファクトから
`iryo.kernel.Notifier` を実装します。

### iryo-reporting — 統計

依存: `iryo-kernel`, `iryo-domain`。`VitalSequence` は独自の
`filter` / `map` / `total` フルーエント API を提供します。
受付フローでは同じ画面上に `java.util.stream` の `filter` も置かれます。
`KarteKit.heading()` は static import で使用します。

## アプリケーション

### reception-desk — 受付コンソール

全 9 ライブラリに依存します。登録は `Program.buildContainer()` の 4 行に
集約され、通知は Mail の後に SMS を登録するため単一解決は SMS、
複数解決は登録順を維持します。

| フロー | 主な内容 |
|---|---|
| `CheckInFlow` | DI の最後勝ち、複数解決、リポジトリの一意実装、`var` 連鎖、三段継承 |
| `AccountingFlow` | オーバーロード、ラムダ目標型、null の最詳細型、static import、既定メソッド、領収書三兄弟、旧 API 警告 |
| `PharmacyFlow` | 同名ツイン、2 つの wildcard import と single-type import、識別子 `Pharmacy`、完全修飾名 |
| `ReportFlow` | 独自 fluent と JDK stream、南北 `Ward`、`logger::flush`、`Logger::new`、`Exporter` 2 実装 |
| `MaintenanceFlow` | 構築ジェネリクス、型 witness、別パッケージ同名キャッシュ、入れ子・内部型、静的メソッド隠蔽、reflection、static import の遮蔽 |
| `DailyCloseFlow` | 管理コンソールとのプロジェクト間分岐対 |

### admin-console — 管理コンソール

依存: `iryo-kernel`, `iryo-domain`, `iryo-billing`, `iryo-pharmacy`,
`iryo-reporting`。`DailyBatch.run()` の処理行は受付の
`DailyCloseFlow.run()` と見出し以外が同一ですが、import だけが異なり、
結果は `[調剤]` / `false` / `162円` になります。`ReportDigest` は
独自列を使わず JDK stream だけで集計します。

### patient-portal — 患者ポータル

依存: `iryo-kernel`, `iryo-domain`, `iryo-scheduling`, `iryo-billing`。
JDK の `com.sun.net.httpserver` で予約・料金ハンドラを提供します。
`src/main/webapp/patient_view.jsp` は旧画面の保管テンプレートで、
HTTP サーバーからは配信しません。

## 解決シナリオ対応表

| 区分 | 呼び出し・宣言 | 確認する境界 |
|---|---|---|
| XPR | `DailyCloseFlow` / `DailyBatch` | 同一処理行が import と呼び出し元 artifact により別宣言へ到達 |
| TWIN | 2 種の `Logger`, `Validator`, `PointMath`, `Money`; 3 種の `ReceiptFormatter` | 同じ単純名を package と receiver 型で分離 |
| OVL | `FeeCalculator.calculate/run/scale/bill/route` | 拡大変換対ボックス化、固定引数、varargs、ラムダ目標型、null 最詳細型 |
| INH | `Entity → AuditedEntity → Patient` | 祖父母宣言、上書き、共変戻り値、static hiding |
| DIM | `PaymentPolicy` / `UrgentPolicy` / `CombinedPolicy` | interface 既定メソッド、上書き、`PaymentPolicy.super` |
| DI | `Program.buildContainer()` | class literal 登録、最後勝ち、登録順、コンストラクタ注入、factory lambda 除外 |
| IFI | `Repository<T>` / `Exporter` | 実装 1 個なら具体化、2 個なら推測しない |
| GEN | 2 種の `ChartCache`, `Registry.<ChartEntry>create` | 構築型、型 witness、別 package の同名ジェネリクス |
| STR | `VitalSequence.filter/map` / `stream().filter` | アプリ内 fluent と JDK API の同名呼び出し |
| MRF | `logger::flush` / `Logger::new` | メソッド参照とコンストラクタ参照 |
| REC | `iryo.domain.Money` | record の正準コンストラクタと accessor |
| NSP | wildcard、single-type、`java.util.List` 対 `java.awt.*`、完全修飾名、interface static 呼び出し | Java の import・名前探索規則 |
| BND | `_feed` 内の JAR と source JAR | 全プロジェクト間呼び出しのバイナリ境界 |
| UNR | `FeeCalculator.class.getMethods()` の文字列比較 | reflection による候補集合を推測しない |
