# 医療管理システム — Java シンボル解決コーパス

診療所・病院向け業務システムを模した、12 個の自己完結 Maven
プロジェクトです。各ディレクトリ名は `artifactId` と一致し、
座標はすべて `jp.iryo:<artifactId>:1.0.0` です。

プロジェクト間の依存は共有ローカルフィード `_feed/` に配置した
コンパイル済み JAR だけを参照します。親 POM、Maven reactor、
相対モジュール参照は使いません。各プロジェクトは独自の `pom.xml`,
`build.sh`, `README.md`, `.gitignore` を持ち、別々のリポジトリと
同じ境界でビルドできます。

## 必要環境

- JDK 21。`JAVA21_HOME` で場所を上書きできます。
- Maven 3.9 以降。

全 POM は `--release 21`, `-parameters`, UTF-8 を指定し、compiler,
jar, deploy, source, resources, dependency の各 Maven プラグインを
固定バージョンで使用します。

## クリーンビルド順序

フィクスチャ直下で次の順に実行します。

```bash
rm -rf _feed _m2

./iryo-kernel/build.sh
./iryo-domain/build.sh

./iryo-data/build.sh
./iryo-billing/build.sh
./iryo-pharmacy/build.sh
./iryo-scheduling/build.sh
./iryo-reporting/build.sh

./iryo-legacy-claims/build.sh
./iryo-interop/build.sh

./reception-desk/build.sh
./admin-console/build.sh
./patient-portal/build.sh
```

最初の 9 プロジェクトはソース JAR を付けて `_feed/` へ deploy します。
受付端末と管理コンソールは依存 JAR を `target/deps/` へ集めて実行し、
次の完了マーカーを表示します。

- `=== 受付業務デモ完了 ===`
- `=== 管理タスク完了 ===`

患者ポータルは JDK 標準 HTTP サーバーを使用しますが、`build.sh` は
パッケージ作成だけを行い、待受ポートは開きません。

## ローカルフィードと Maven リポジトリ

- `_feed/`: ライブラリの deploy 先。消費側はここから
  `jp.iryo:*:1.0.0` を解決します。
- `_m2/`: このコーパス専用の Maven ローカルリポジトリです。
  各 `build.sh` が `-Dmaven.repo.local` で明示するため、利用者の
  `~/.m2` を変更しません。

ライブラリの `build.sh` は自分自身の `_m2/jp/iryo/<artifactId>` を
先に削除します。同じ `1.0.0` を再ビルドしたときも古い JAR が残りません。
`settings.xml` は共通設定として明示的に渡し、リポジトリ位置は
コマンドライン指定を正とします。

## 依存未構築の失敗

依存側を先にビルドすると必ず解決に失敗します。たとえばクリーン状態で
次を実行すると、`jp.iryo:iryo-kernel:jar:1.0.0` を解決できません。

```bash
rm -rf _feed _m2
./reception-desk/build.sh
```

これはすべてのプロジェクト間参照が JAR 境界を通ることを確認する
意図的な失敗経路です。その後、上記のトポロジカル順序でビルドします。

## 想定される警告

警告は受付端末の `AccountingFlow` にある旧点数表呼び出しの
deprecation 警告 1 件だけです。`reception-desk` だけが
`-Xlint:deprecation` を有効にし、ほかの javac 警告は想定していません。

## プロジェクトと解決シナリオ

各プロジェクトの役割、依存関係、および呼び出し解決シナリオの対応は
[docs/PROJECTS.md](docs/PROJECTS.md) を参照してください。製品ソースには
検証用タグを置かず、正解データは `ground_truth/` に分離します。
