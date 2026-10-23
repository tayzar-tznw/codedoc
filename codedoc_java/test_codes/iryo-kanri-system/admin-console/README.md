# admin-console

日次バッチと週次レポートを実行する管理者向けコンソールです。
受付端末とは異なる業務パッケージを使って日次処理を行います。

依存アーティファクト: `iryo-kernel`, `iryo-domain`, `iryo-billing`,
`iryo-pharmacy`, `iryo-reporting`

依存先は共有 Maven フィード `../_feed` から取得します。`./build.sh` は
ビルド後に管理タスクを実行します。
