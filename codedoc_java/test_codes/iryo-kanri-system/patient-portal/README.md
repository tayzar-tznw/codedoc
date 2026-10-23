# patient-portal

JDK 標準の `com.sun.net.httpserver` を利用する患者向けポータルです。
予約確認と料金目安の HTTP ハンドラを持ちます。

依存アーティファクト: `iryo-kernel`, `iryo-domain`, `iryo-scheduling`,
`iryo-billing`

このプロジェクトの `./build.sh` はパッケージ作成のみを行い、
HTTP サーバーは起動しません。
