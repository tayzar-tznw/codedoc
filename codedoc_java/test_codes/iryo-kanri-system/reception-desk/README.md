# reception-desk

来院受付から会計、薬局連携、日報、保守、日次締めまでを順に実行する
受付窓口コンソールです。依存性注入の合成ルートは `Program` に集約しています。

依存アーティファクト: `iryo-kernel`, `iryo-domain`, `iryo-data`,
`iryo-billing`, `iryo-pharmacy`, `iryo-scheduling`, `iryo-reporting`,
`iryo-legacy-claims`, `iryo-interop`

依存先は共有 Maven フィード `../_feed` から取得します。`./build.sh` は
ビルド後にデモを実行します。
