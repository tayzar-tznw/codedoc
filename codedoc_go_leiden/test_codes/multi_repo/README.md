# multi_repo: リポジトリをまたぐ解決のフィクスチャ F3

三つのリポジトリを並べて取り込み、互いをピアにして `crossref` の辺を検証するフィクスチャ。ケースは 31 件、クロスリポジトリ辺は 28 本 (2026-10-14)。
`evaluate` はオフラインと env の両モードで、誤答 0、完全性 floor(0.85·N)+1、辺の集合の完全一致を判定する。

製品コードにコメントは書かない。正解は `ground_truth/` にある。

| リポジトリ | モジュール | 役割 |
|---|---|---|
| `shared` | `example.com/iryo/shared` (ルート)、`example.com/iryo/textkit` (`textkit/`、入れ子) | 共有ライブラリ。`money` (`Price`、`Round`)、`log` (`Logger`)、`contracts` (`Clock`、`Audit`) |
| `web` | `example.com/iryo/web` | 単一モジュール。`shared` v1.0.0、`textkit` v1.0.0、`example.com/thirdparty/uuidlite` v1.0.0 を require する |
| `api` | `go.work` が `service` と `notify` を束ねる。`go.work` の外に `forks/textkit`、`tools`、`legacy` | 請求 API。`forks/textkit` は `example.com/iryo/textkit` のフォークで、`service` が `replace => ../forks/textkit` で使う |

`test_codes/_proxy` には `example.com/iryo/shared` と `example.com/iryo/textkit` の v1.0.0 も公開されている。公開版の `shared` は API が違う。`Price.Format` と `contracts.Audit` が無く、`money.Legacy` がある。

## ピアの置き換え (仕様 §8.2)

| モジュール | 置き換え | 理由 |
|---|---|---|
| `web` | `replace example.com/iryo/shared => <shared の絶対パス>`。`-modfile` で渡す go.mod のコピーに書く (両モード)。ダウンロードモードはこのコピーに `-mod=mod` でダウンロードして go.sum のコピーを埋め (改訂 R12)、足された require は go.mod のコピーを戻して消す (改訂 R45) | `shared` を require する単一モジュール |
| `api` の `service`、`notify` | 生成する `go.work` に同じ `replace` | ワークスペースのグループ |
| `example.com/iryo/textkit` (`web` から) | 置き換えない | `shared` と `api` の二つのピアが持つ (フォーク)。ピアがない場合と同じく公開版になる |
| `example.com/iryo/textkit` (`api` から) | 置き換えない | `service` の go.mod が自分で `replace => ../forks/textkit` している。グループのモジュールかリポジトリの `go.work` がすでに replace しているパスには、ピアの replace を足さない (Q3 の裁定)。足すと go.work の replace が勝ち、ピアの `Banner` になる |
| `api/legacy` | 置き換えない | `go mod vendor` した `vendor/` を持つ。vendor のコピーが勝つ |
| `api/tools` | 置き換えない | `example.com/iryo/shared` を require しない (宣言のないピア依存) |

## go コマンドの結果

go1.27.2、`GOTOOLCHAIN=local`、`CGO_ENABLED=0`、`GOPROXY=file:///…/test_codes/_proxy`、`GOSUMDB=off`、仕様 §3.6 の `GOFLAGS` (`-mod=readonly`、vendor のあるグループは `-mod=vendor`) で写しを動かした結果 (2026-10-14)。

| ディレクトリ | ピアなし | ピアを置き換えたとき | オフライン (`GOPROXY=off`、空のキャッシュ) とピア |
|---|---|---|---|
| `web` | 失敗: `orders/orders.go:21:25: sum.Format undefined` | 成功 | `shared` のパッケージは読める。`textkit` と `uuidlite` は `module lookup disabled by GOPROXY=off` |
| `api/service` (`api/go.work`) | 失敗: `undefined: contracts.Audit`、`amount.Format undefined` など | 成功。`textkit` は `service` の replace でフォーク | 成功 (第三者モジュールを使わない) |
| `api/legacy` | 成功 (vendor) | 置き換えない | 成功 (vendor) |
| `api/tools` | 失敗: `cannot find module providing package example.com/iryo/shared/money: import lookup disabled by -mod=readonly` | 置き換えない | 失敗 |
| `shared`、`shared/textkit` | 成功 (`go test ./...`) | — | 成功 |

## 辺 (完全一致)

`ground_truth/cases.py` の `CROSS_REPO_EDGES` が正解で、唯一の記録。`generate_manifest.py` がこれを `manifest.json` と `ground_truth/expected_cross_repo_edges.json` (`evaluate` が読む) に書き出す。このファイルは手で編集しない。`check_ground_truth.py` は三つが同じかを調べる。

| 表 | 本数 | 端点 |
|---|---|---|
| `CrossRepoImports` | 8 | ファイル `<リポジトリ>:<パス>` → パッケージ `<リポジトリ>:<ディレクトリ>\|<パッケージ名>` |
| `CrossRepoRef` | 3 (`embeds` 2、`implements` 1) | 型 `<リポジトリ>:<パス>:<qualname>` → 型 |
| `CrossRepoCalls` | 17 | 呼び出し元のメソッド → 呼び出し先のメソッド (`<リポジトリ>:<パス>:<qualname>`) |

`check_ground_truth.py` は、ピアを行き先とするケースから `CrossRepoCalls` を導き、ファイルの集合と完全に同じかを調べる。
`implements` は `web` の `SystemClock` が `shared` の `contracts.Clock` を満たす組。`embeds` は `web` の `OrderService` が埋め込む `*log.Logger` と、`api` の `AuditedInvoice` が埋め込む `contracts.Audit`。
`web` の `clock.Stamp` から `contracts.Clock.Now` への呼び出しはピアのインターフェースメソッドへの `CrossRepoCalls`。

## 負の対照 (辺を作らない)

| ケース | 仕様の項目 | 期待値 |
|---|---|---|
| NEG-001 | 同じモジュールパスが二つのリポジトリにある (`textkit` のフォーク) | env では公開版の external `example.com/iryo/textkit`、オフラインでは `offline=` で unresolved (missing-module)。`crossref` は結ばない |
| NEG-004 | 宣言のないピア依存 (`api/tools`) | unresolved |
| NEG-005、NEG-006、NEG-007 | vendor したピアのコピー (`api/legacy`) | external `example.com/iryo/shared` (vendor)。`Legacy` は公開版にだけある |
| XRP-005、XRP-012、XRP-013 | `_proxy` にも同じパスで公開されているピア (API が違う) | ピアの作業中のソースにだけある `Format` と `Audit.Stamp`。置き換えが効かないと解決できない |
| NEG-002 | 単純名が同じ別の型 (`web` の `checks.Logger.Info`) | リポジトリ内の解決。名前でピアに結び付けない |
| NEG-003 | どのリポジトリも持たない第三者モジュール (`uuidlite`) | external。`crossref` の対象外 |
| API-001 | ピアと同じパスを自分で replace するモジュール (`api` の `service` とフォーク) | リポジトリ内の解決。ピアの replace は足さない |

## オラクル

`entries.json` の入口は 5 件。入口のないケースは NEG-004 だけ (go コマンドが失敗する)。
ピアの置き換えは、どのリポジトリにも属さない `ground_truth/oracle/web.work` と `api.work` で表す。`GOWORK` に指定すると、`shared` をワークスペースのモジュールとして読む。開発者のローカルの `go.work` と同じ形。`textkit` は `web` では `_proxy` の公開版、`api` では `service` の replace によるフォーク。

## 再生成

`go.sum` と `vendor/` は `test_codes/_proxy` から作ったもの。手で編集しない。`_proxy` の zip が変わったら、作業用の写しで作り直し、次のファイルだけを戻す。

| ファイル | 作り方 |
|---|---|
| `web/go.sum`、`api/service/go.sum`、`api/notify/go.sum`、`api/legacy/go.sum` | `go mod download -json <モジュール>@<版>` の `Sum` と `GoModSum` |
| `api/legacy/vendor/` | `api/legacy` で `GOWORK=off go mod vendor` |

## 道具

`codedoc_go/` で実行する。

```bash
python3 test_codes/_gt/generate_manifest.py test_codes/multi_repo
python3 test_codes/_gt/check_ground_truth.py test_codes/multi_repo
python3 test_codes/_gt/run_oracle.py test_codes/multi_repo
```

## 守ること

- フォーク、vendor のコピー、宣言のない依存、同名の型は検証の本体。整理しない。
- フィクスチャの中で go コマンドを動かさない。ワークスペースでは `go.work.sum` が書かれる。写しで動かす。
- 製品コードにコメントを書かない。
