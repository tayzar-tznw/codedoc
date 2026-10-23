# iryo-kanri-system ドキュメント

## はじめに



## モジュールドキュメント

*   **[患者・予約・受付管理](topics/患者予約受付管理.md)**: # 患者・予約・受付管理
*   **[診療報酬・医療会計管理](topics/診療報酬医療会計管理.md)**: # 診療報酬・医療会計管理
*   **[処方箋・調剤・投薬管理](topics/処方箋調剤投薬管理.md)**: # 処方箋・調剤・投薬管理
*   **[カルテ・バイタルデータ管理](topics/カルテバイタルデータ管理.md)**: # カルテ・バイタルデータ管理
*   **[業務日報・バッチ・データ出力](topics/業務日報バッチデータ出力.md)**: # 業務日報・バッチ・データ出力
*   **[ポータル・通知連携](topics/ポータル通知連携.md)**: # ポータル・通知連携
*   **[データアクセス・エンティティ基盤](topics/データアクセスエンティティ基盤.md)**: # データアクセス・エンティティ基盤
*   **[システム基盤・保守点検・動作検証](topics/システム基盤保守点検動作検証.md)**: # システム基盤・保守点検・動作検証

## コンポーネント一覧

| ファイル | 概要 |
|------|---------|
| iryo-reporting/src/main/java/iryo/reporting/VitalSequence.java | `VitalSequence.java`は、医療レポーティングシステム（`iryo.reporting`パッケージ）において、一連のバイタルデータ（整数値）を業 |
| admin-console/src/main/java/iryo/admin/Program.java | 本ファイルは、医療管理システムにおける管理コンソールアプリケーションのエントリーポイント（起動点）を提供するクラスです。主要クラスである `Program` は |
| iryo-domain/src/main/java/iryo/domain/AuditedEntity.java | 本ファイル（`AuditedEntity.java`）は、ドメインモデルにおいて監査証跡の記録・管理機能を備えた中間エンティティを定義することを目的としています |
| iryo-kernel/src/main/java/iryo/kernel/Notifier.java | `Notifier.java`は、医療系ドメイン（`iryo.kernel`パッケージ）において、患者や職員に対する連絡・通知手段を抽象化するためのインターフェ |
| iryo-data/src/main/java/iryo/data/Repository.java | 本ファイルは、業務システム（`iryo.data`パッケージ）におけるデータアクセス層の共通仕様を定義するインターフェースファイルです。主要な構成要素である`R |
| iryo-billing/src/main/java/iryo/billing/text/ReceiptFormatter.java | `ReceiptFormatter.java` は、医療会計システム（`iryo.billing.text` パッケージ）において、現行会計用の領収書表示テキス |
| iryo-pharmacy/src/main/java/iryo/pharmacy/Money.java | 本ファイルは、薬局管理システムにおいて医薬品などの在庫評価額を安全かつ明確に表現することを目的としたドメインモデルです。主要クラスである `Money` は、在 |
| iryo-domain/src/main/java/iryo/domain/ChartArchive.java | 本ファイルは、医療ドメインにおけるカルテの長期保管庫を表現する `iryo.domain.ChartArchive` クラスを定義しています。外部ライブラリへの |
| iryo-interop/src/main/java/iryo/interop/XmlExporter.java | 本ファイル（XmlExporter.java）は、XML形式による外部提出データの生成・出力を目的としたソースコードです。主要クラスである`XmlExporte |
| iryo-reporting/src/main/java/iryo/reporting/KarteKit.java | 本ファイル（KarteKit.java）は、医療系システムの帳票出力機能において、カルテの見出し整形や数値データの簡易的な集計を支援することを目的としたユーティ |
| iryo-kernel/src/main/java/iryo/kernel/UrgentPolicy.java | `UrgentPolicy.java`は、医療システムにおいて時間外診療や急患時における支払いルール（手数料率や割増率）を定義するためのソースコードです。本ファ |
| admin-console/src/main/java/iryo/admin/ReportDigest.java | 本ファイルは、医療管理システムにおいて週次の脈拍傾向データおよび収益状況を集計・出力することを目的としたファイルです。主要クラスである `ReportDiges |
| iryo-domain/src/main/java/iryo/domain/Patient.java | 本ファイルは、医療ドメイン（`iryo.domain`パッケージ）における患者情報を表すエンティティクラス「Patient」を定義しています。患者台帳のデータモ |
| iryo-kernel/src/main/java/iryo/kernel/StandardPolicy.java | `StandardPolicy.java` は、医療ドメイン（`iryo.kernel` パッケージ）において支払いに関する手数料算定方針を定義するクラスファイ |
| iryo-billing/src/main/java/iryo/billing/Validator.java | 本ファイルは、医療請求システム（`iryo.billing`パッケージ）において保険証記号番号の形式を検証することを目的としたクラスです。主要クラスである`Va |
| reception-desk/src/main/java/iryo/reception/flows/MaintenanceFlow.java | `MaintenanceFlow.java`は、医療システムにおけるカルテキャッシュ、アーカイブ機能、点数表計算などの各種サブシステムが正常に動作するかを点検・ |
| iryo-billing/src/main/java/iryo/billing/FeeCalculator.java | 本ファイル（FeeCalculator.java）は、医療事務における診療報酬の点数計算や窓口請求・受付処理を行うための計算クラスです。クラスは`final`宣 |
| ground_truth/SemanticProbe/src/main/java/iryo/probe/Program.java | `Program.java`は、医療ドメイン向けシステム（`iryo`名前空間）におけるJava言語仕様に基づいた型解決やバインディングの正当性を実行時に検証す |
| reception-desk/src/main/java/iryo/reception/flows/DailyCloseFlow.java | `DailyCloseFlow.java` は、医療受付システムにおける受付端末の日次締め処理を実行することを目的としたファイルです。主要クラスである `Dai |
| iryo-scheduling/src/main/java/iryo/scheduling/SmsNotifier.java | 本ファイルは、予約管理システムにおいてSMS（ショートメッセージサービス）を用いた通知機能を提供するクラスを定義しています。`iryo.scheduling`パ |
| iryo-legacy-claims/src/main/java/iryo/legacy/claims/text/ReceiptFormatter.java | 本ファイル（ReceiptFormatter.java）は、旧世代のレセプトコンピュータ（レセコン）との互換性を保った領収書テキストの整形処理を提供するユーティ |
| iryo-kernel/src/main/java/iryo/kernel/PaymentPolicy.java | `PaymentPolicy.java`は、支払い処理における事務手数料の算出方針を規定するためのインターフェースです。`iryo.kernel`パッケージに属 |
| iryo-kernel/src/main/java/iryo/kernel/Guard.java | 本ファイル（Guard.java）は、`iryo.kernel`パッケージに属し、業務APIにおけるメソッド引数の事前条件（バリデーション）を検証するための共通 |
| iryo-interop/src/main/java/iryo/interop/PatientIdKit.java | 本ファイルは、外部システム連携処理において患者IDのフォーマットを統一・正規化することを目的としたユーティリティクラスです。提供される`PatientIdKit |
| iryo-kernel/src/main/java/iryo/kernel/logging/Logger.java | 本ファイルは、診療業務システムにおいて共通で利用されるロギング機能を提供する`Logger.java`です。主要クラスである`Logger`は、システム内で発生 |
| iryo-legacy-claims/src/main/java/iryo/legacy/claims/PointTable.java | 本ファイルは、レガシーな医療費請求処理において経過措置加算を含めた点数計算を行うことを目的としたクラスです。主要クラスである `PointTable` は、バー |
| ground_truth/SemanticProbe/src/main/java/iryo/probe/xpr/AdminSide.java | `AdminSide.java`は、管理コンソール側の手続きを再現し、インポート宣言による型の束縛先と実行時挙動の違いを検証・証明するためのプローブクラスです。 |
| iryo-data/src/main/java/iryo/data/Registry.java | 本ファイルは、リフレクションを利用して動的にオブジェクトのインスタンスを生成・提供することを目的としたユーティリティクラスです。`iryo.data` パッケー |
| iryo-data/src/main/java/iryo/data/ChartCache.java | `ChartCache.java` は、医療データ処理システム（`iryo.data` パッケージ）において、診療科などの任意のキーに基づきカルテ情報をメモリ上 |
| iryo-legacy-claims/src/main/java/iryo/legacy/claims/ChartCache.java | 本ファイルは、旧来のレセコン（レセプトコンピュータ）処理系において単一の値を一時的に保持・管理するための簡易キャッシュ機能を提供するものです。中心となる`Cha |
| iryo-pharmacy/src/main/java/iryo/pharmacy/PointMath.java | 本ファイル（PointMath.java）は、医療・調剤システムにおいて薬価点数を院内レートに基づいて日本円へ換算することを目的としたユーティリティファイルです |
| iryo-domain/src/main/java/iryo/domain/Entity.java | 本ファイルは、医療などの台帳ドメインにおいて各エンティティの基底クラスとなる`Entity`クラスを定義しています。エンティティを一意に識別するための識別子（` |
| iryo-kernel/src/main/java/iryo/kernel/Logger.java | 本ファイルは、旧来の単一パッケージ構成（`iryo.kernel`）を採用している既存端末向けに提供された、ロガーの後方互換性を維持するためのラッパークラスです |
| iryo-data/src/main/java/iryo/data/SqlPatientRepository.java | 本ファイルは、医療ドメインにおける患者データへのアクセスを担当するSQLベースの実装クラス `SqlPatientRepository` を定義しています。汎用 |
| admin-console/src/main/java/iryo/admin/DailyBatch.java | 本ファイルは、医療・調剤管理システムにおける管理者向けの日次バッチ処理を統括・実行するためのエントリーポイントです。主要クラスである`DailyBatch`は、 |
| iryo-scheduling/src/main/java/iryo/scheduling/AppointmentBook.java | `AppointmentBook.java`は、医療システム等における確定済みの予約情報をインメモリで管理・保持するための台帳クラスです。パッケージ`iryo. |
| reception-desk/src/main/java/iryo/reception/Program.java | 本ファイルは、医療管理システムにおける受付端末アプリケーションのエントリーポイント（起動点）です。主要クラスである`Program`は、インスタンス化を防ぐプラ |
| iryo-kernel/src/main/java/iryo/kernel/ServiceCollection.java | 本ファイルは、依存性の注入（DI）コンテナにおいてサービスの登録と構成を担う合成ルート向けのクラス群を定義しています。中心となる`ServiceCollecti |
| patient-portal/src/main/webapp/patient_view.jsp | 本ファイルは、かつて患者情報を表示するために使用されていた旧画面のJSPテンプレートです。冒頭のコメントに記されている通り、現在は「PortalServer」か |
| iryo-interop/src/main/java/iryo/interop/CsvExporter.java | `CsvExporter.java` は、外部提出用データをCSV形式で出力する機能を提供するファイルです。`iryo.interop` パッケージに属する ` |
| iryo-pharmacy/src/main/java/iryo/pharmacy/Logger.java | 本ファイル（Logger.java）は、調剤台帳システムにおいて調剤処理専用のログメッセージをメモリ上で保持・管理することを目的としたユーティリティクラスです。 |
| patient-portal/src/main/java/iryo/portal/PortalServer.java | 本ファイルは、患者向けの予約および診療料金照会機能を提供する軽量なHTTPサーバー「`PortalServer`」を定義しています。`PortalServer` |
| iryo-legacy-claims/src/main/java/iryo/legacy/claims/OldTable.java | 本ファイルは、医療報酬請求等のシステムにおいて移行期間中の照合処理で利用される旧点数表を表すクラスファイルです。主要クラスである`OldTable`は、後方互換 |
| iryo-interop/src/main/java/iryo/interop/Exporter.java | `Exporter.java`は、`iryo.interop`パッケージに属し、外部システムへの提出データの出力処理を共通化・抽象化するためのインターフェースで |
| reception-desk/src/main/java/iryo/reception/flows/ReportFlow.java | `ReportFlow.java`は、医療システムにおけるバイタルデータの集計処理および外部提出用データの生成フローを統括・実行するためのファイルです。中心とな |
| ground_truth/SemanticProbe/src/main/java/iryo/probe/shadow/ShadowSide.java | `ShadowSide.java` は、受付端末の実装におけるスコープ遮蔽（シャドウイング）構造を再現し、Java言語仕様（JLS 6.4.1）に基づく静的イン |
| iryo-pharmacy/src/main/java/iryo/pharmacy/DoseCalculator.java | `DoseCalculator.java`は、患者の体重と体重あたりの投与基準量（単位用量）に基づき、一回あたりの薬の投与量を算出することを目的としたクラスファ |
| iryo-domain/src/main/java/iryo/domain/north/Ward.java | 本ファイルは、医療ドメインにおける北病棟の案内および位置情報を表現することを目的としたクラスファイルです。主要なクラスである`Ward`は`final`修飾子付 |
| iryo-pharmacy/src/main/java/iryo/pharmacy/Validator.java | `Validator.java`は、`iryo.pharmacy`パッケージに属し、処方箋データの様式が妥当であるかを判定・検証することを目的としたファイルです |
| reception-desk/src/main/java/iryo/reception/flows/AccountingFlow.java | `AccountingFlow.java`は、医療受付業務における診療点数の計算から患者への請求・受領、および領収書発行に至る一連の会計処理を実行・検証するため |
| reception-desk/src/main/java/iryo/reception/flows/CheckInFlow.java | 本ファイルは、医療受付業務における来院受付処理および患者台帳照会の一連の手順を実行するユーティリティクラス `CheckInFlow` を定義しています。プライ |
| reception-desk/src/main/java/iryo/reception/flows/PharmacyFlow.java | 本ファイルは、薬局窓口における処方箋の受領・確認から調剤部門および請求部門への連携、在庫評価に至る一連の業務フローを統括・実行することを目的としています。主要ク |
| ground_truth/SemanticProbe/src/main/java/iryo/probe/xpr/ReceptionSide.java | 本ファイルは、受付端末における日次締め処理の手続きを再現し、インポート宣言による名前解決・束縛の違いを実行時に検証するためのプローブ用クラスです。対となる `i |
| iryo-domain/src/main/java/iryo/domain/Appointment.java | 本ファイルは、医療システムにおける診察予約のデータを表現・管理するドメインモデルです。主要クラスである`Appointment`は、予約ID（`id`）、患者I |
| iryo-data/src/main/java/iryo/data/RepositoryFactory.java | `RepositoryFactory.java`は、バッチ処理等で利用される既定のリポジトリインスタンスを一元的に生成・提供することを目的としたファイルです。本 |
| iryo-domain/src/main/java/iryo/domain/south/Ward.java | 本ファイル（`Ward.java`）は、医療関連システムなどのドメインモデルにおいて「南病棟」の案内や位置情報の特定を提供することを目的としたクラスファイルです |
| iryo-kernel/src/main/java/iryo/kernel/ServiceProvider.java | `ServiceProvider.java` は、軽量な依存性注入（DI）コンテナにおいてサービスの解決およびインスタンス生成を担うサービスクラスです。DI（依 |
| iryo-kernel/src/main/java/iryo/kernel/MailNotifier.java | 本ファイルは、医療システムにおける通知機能の一部として、院内既定のメール送信処理を担当するクラスを定義したものです。主要クラスである `MailNotifier |
| iryo-domain/src/main/java/iryo/domain/ChartEntry.java | `ChartEntry.java` は、医療ドメイン（`iryo.domain` パッケージ）においてカルテの1件分の記載情報を表現・保持するモデルファイルです |
| iryo-interop/src/main/java/iryo/interop/text/ReceiptFormatter.java | `ReceiptFormatter.java` は、医療システムの外部連携処理において領収データを所定のテキスト形式へ整形することを目的としたクラスファイルです |
| iryo-domain/src/main/java/iryo/domain/Money.java | `Money.java`は、ドメイン層（`iryo.domain`）において円建ての金銭データを表現するための不変な値オブジェクト（Value Object）で |
| iryo-billing/src/main/java/iryo/billing/PointMath.java | PointMath.javaは、医療費請求処理（iryo.billingパッケージ）において、診療報酬点数の円換算および金額の丸め処理を提供するユーティリティク |