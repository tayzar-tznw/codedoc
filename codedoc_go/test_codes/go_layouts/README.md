# go_layouts: モジュール配置と発見のフィクスチャ F2

go コマンドがモジュール、ワークスペース、vendor、ツールチェーン、ビルド制約をどう扱うかを、小さなレイアウト 23 個で一通り試すフィクスチャ。モジュールは 29 個、ケースは 67 件 (2026-10-14)。
`evaluate` はオフラインと env の両モードで、誤答 0 と完全性 floor(0.85·N)+1 だけを判定する。
発見処理 (仕様 §3) の事実は `ground_truth/expected_units.json` にあり、WP-D のテストと `evaluate` が使う。

トップレベルのディレクトリ一つが一つのリポジトリ (リポジトリ名 = ディレクトリ名)。`_outside/` はどのリポジトリでもなく、リポジトリの外を指す replace の行き先だけを置く。フィクスチャのルートにも祖先にも `go.mod` と `go.work` はない。
製品コードにコメントは書かない。正解は `ground_truth/` にある。

## レイアウトと go コマンドの結果

go1.27.2、`GOTOOLCHAIN=local`、`GOOS=linux GOARCH=amd64 CGO_ENABLED=0`、`GOPROXY=file:///…/test_codes/_proxy`、`GOSUMDB=off`、仕様 §3.6 の `GOFLAGS` (`-mod=readonly`、vendor のあるグループは `-mod=vendor`) で写しを動かした結果 (2026-10-14)。モジュールパスは `example.com/layouts/` に続けて書く。グループは改訂 R4 の規則による: モジュールのルートから上へたどって見つかる `go.work` がそのモジュールを `use` していればワークスペース、そうでなければ単一モジュール (`GOWORK=off`)。

| リポジトリ | モジュール | 試すこと | グループ | go コマンドの結果 |
|---|---|---|---|---|
| `nested` | `nested`、`nested/inner` | 入れ子のモジュールを `require` と `replace => ./inner` で使う | module ×2 | 成功 |
| `ws` | `ws/app`、`ws/lib`、`ws/extra` | `go.work` が app と lib を束ねる。app は lib を require せずに import。app は textfmt v1.0.0、lib は v1.1.0 を require し、MVS は v1.1.0 を選ぶ。extra は `ws/` の下にあるが `use` されていない | workspace (app, lib)、module (extra) | app は成功。`GOWORK=off` では `cannot find module providing package example.com/layouts/ws/lib/tally: import lookup disabled by -mod=readonly`。extra は失敗 (同じエラー。`ws/go.work` のままなら `directory prefix . does not contain modules listed in go.work`) |
| `wsvendor` | `wsvendor/svc`、`wsvendor/util` | `go work vendor` の `vendor/` (`modules.txt` の 1 行目が `## workspace`) | workspace、vendor | 成功。オフラインでも成功 |
| `modvendor` | `modvendor` | `go mod vendor` の `vendor/` | module、vendor | 成功。オフラインでも成功 |
| `replace-in` | `replacein` (`app/`)、`common` (`common/`) | `replace => ../common` (リポジトリ内の隣のモジュール) | module ×2 | 成功 |
| `replace-out` | `replaceout` | `replace example.com/outside/extlib => ../_outside/extlib` (リポジトリの外の実在するディレクトリ) | module | 成功 |
| `ws-missing` | `wsmissing/present` | `go.work` の `use ./absent` が存在しない | workspace、degraded | `ws-missing/go.work` のままでは失敗: `go: cannot load module ../absent listed in go.work file: open ../absent/go.mod: no such file or directory`。`GOWORK=off` なら成功 |
| `toonew` | `toonew` | `go 1.28` | module、toolchain | 失敗: `go: go.mod requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)` |
| `ws-toonew` | `wstoonew/member` | `go.work` の `go 1.28` (モジュールは `go 1.26`) | workspace、toolchain | 失敗: `go: ../go.work requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)`。`GOWORK=off` なら成功 |
| `newtoolchain` | `newtoolchain` | `go 1.26` と `toolchain go1.28.1` | module、ok (警告) | `GOTOOLCHAIN=local` では成功。既定の `auto` では失敗: `go: download go1.28.1 for linux/amd64: toolchain not available` |
| `depnewgo` | `depnewgo/m` | `go.work` のモジュールが require する `example.com/thirdparty/newgo` v1.0.0 の go.mod が `go 1.28` | workspace、error (ロード後) | env では失敗: `go: module example.com/thirdparty/newgo@v1.0.0 requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)`。オフラインでは `newgo` が無いだけで、リポジトリ内のパッケージは読める |
| `nosum` | `nosum` | `go.sum` に textfmt の行がない (uuidlite の行はある) | module | `go run .` は成功。`go build ./caps` は失敗: `missing go.sum entry for module providing package example.com/thirdparty/textfmt` |
| `nomodule` | なし | `go.mod` がない | なし | `go run loose.go` は成功、`go build ./...` は `directory prefix . does not contain main module` |
| `skipdirs` | `skipdirs` | `testdata/`、`_draft/`、`.cache/` と、ファイル `_old.go`、`.dot.go` | module | 成功。`./...` は `skipdirs` だけ。三つのディレクトリは明示的な import でコンパイルされる |
| `ignoredir` | `ignoredir` | go.mod の `ignore ./generated` | module | 成功。`./...` は `generated` を含まないが、明示的な import ではコンパイルされる |
| `buildignore` | `buildignore` | `//go:build ignore` の `package main` と、パス要素と違う名前の `package tool` | module | 成功。`go list` の `IgnoredGoFiles` は `gen.go` |
| `symlinks` | `symlinks` | `b/real.go -> ../a/real.go` (ファイル) と `alias -> a` (ディレクトリ) | module | 成功 (`a! b!`)。`./...` は `alias` をたどらない |
| `cgo` | `cgo` | `import "C"` のファイルと `//go:build !cgo` の双子 | module | 成功。`CGO_ENABLED=0` で `native pure`、`=1` で `native CGO` |
| `linedir` | `linedir` | `//line view.tmpl:…` と `/*line…*/`、別のリポジトリ内ファイルの宣言の位置を指す `//line view.go:15:1` (改訂 R3) | module | 成功 |
| `dotimport` | `dotimport` | ドット import (自前のパッケージと `strings`) | module | 成功 |
| `nonancestor` | `nonancestor/api`、`nonancestor/server` | `tools/go.work` が `../api` と `../server` を `use` するが、どちらの祖先でもない (改訂 R4) | module ×2 (警告) | server は成功し、`_proxy` の公開版 api を使う (`published`)。`GOWORK=tools/go.work` にするとリポジトリ内の api (`[in-repo]`) になる |
| `twowork` | `twowork/app`、`twowork/core` | core をルートの `go.work` と `scratch/go.work` の両方が `use` する (改訂 R4) | workspace (警告) | 成功 |
| `badvendor` | `badvendor` | `vendor/` は yamlish v1.0.0 で作ったまま、go.mod と go.sum は v1.1.0 (改訂 R35) | module、error | 失敗: `go: inconsistent vendoring in …: example.com/thirdparty/yamlish@v1.1.0: is explicitly required in go.mod, but not marked as explicit in vendor/modules.txt` |

- `-mod` を明示しなければ、go コマンドは `vendor/` を見つけて vendor モードを選ぶ。`-mod=readonly` を明示すると `vendor/` は使われず、オフラインでは `module lookup disabled by GOPROXY=off` で失敗する。
- ワークスペースの写しで `go.work.sum` は書かれない。必要なチェックサムは各モジュールの `go.sum` にある。

## シナリオ

| 接頭辞 | 内容 | 件数 |
|---|---|---|
| NEST | 入れ子のモジュールと、その replace | 3 |
| WS | `go.work` のワークスペース、MVS、`go.work` に入っていないモジュール | 6 |
| WSV | ワークスペースの `vendor/` | 4 |
| VND | モジュールの `vendor/` | 3 |
| REP | リポジトリ内と外へのローカル replace | 5 |
| DEG | 存在しない `use` (degraded) | 2 |
| TCH | ツールチェーンより新しい `go` 行 (閉じる) と `toolchain` 行 (閉じない) | 5 |
| DNG | 依存の `go` 行が新しいワークスペース (改訂 R5) | 2 |
| SUM | `go.sum` の行の欠落 (改訂 R12) | 3 |
| NOM | モジュールのないファイル | 2 |
| SKP | `testdata/`、`_x/`、`.x/` (スキャンしない) | 4 |
| IGD | go.mod の `ignore` (改訂 R21) | 2 |
| IGN | `//go:build ignore` の二つ目のパッケージ | 3 |
| SYM | シンボリックリンクのファイル (G1 variant) | 3 |
| CGO | `CGO_ENABLED=0` の cgo ファイル | 4 |
| LIN | `//line` ディレクティブ (改訂 R3) | 6 |
| DOT | ドット import | 4 |
| NAN | 祖先でない `go.work` (改訂 R4) | 2 |
| TWO | 二つの `go.work` に並ぶモジュール (改訂 R4) | 2 |
| BVD | `vendor/` と go.mod の不一致 (改訂 R35) | 2 |

状態の内訳は resolved 34、unresolved 19、external 13、ambiguous 1。
`expect` は env モード (`_proxy` に対するダウンロードモード) の答え。モードで答えが変わる site は次のとおり。

| ケース | env (`expect`) | オフライン |
|---|---|---|
| WS-004、WS-005 | external | unresolved (missing-module)。第三者モジュールなので未回答として数える |
| SUM-001、NAN-001 | external (NAN-001 は `_proxy` の公開版) | `offline=` で unresolved (missing-module) |
| SUM-002 | external (ダウンロードが `-modfile` のコピーに `-mod=mod` で制御ディレクトリの go.sum を埋める、改訂 R12。go.mod のコピーは元に戻す、改訂 R45) | `offline=` で unresolved (missing-module) |
| DNG-001 | unresolved (toolchain) | `offline=` で unresolved (missing-module) |
| DNG-002 | unresolved (toolchain)、グループは error | `offline=` で resolved (`local`)。依存の go.mod が見えないのでロードは成功する |

`offline=` の期待値はマニフェストの `expect_offline` になり、`evaluate` はオフラインの実行をそれで採点する (マスタープラン C10)。

vendor の 4 件 (WSV-001、WSV-002、VND-001、VND-002) はオフラインでも external。

## 発見処理の事実

`ground_truth/expected_units.json` は、レイアウトのリポジトリごとに、ユニット、グループ (種類、モジュール、vendor、degraded、状態、パターン、警告)、ファイルの所有、スキャンの件数とスキップの件数、モードごとのモジュールの供給元と欠けたモジュールを持つ。形はファイル冒頭の `"doc"` に書いた。

## オラクル

`entries.json` の入口は 17 件。入口のないケースは 26 件で、次のどれか。

- ビルドされないファイルの site: IGN-002、CGO-002、CGO-003
- `//line` のあるファイル (オラクルは計装しない) の site か呼び出し先: LIN
- 負の対照 (unresolved を期待する site): SKP-001〜003、REP-003、REP-004、WS-006。IGD-001 は入口 `ignoredir` で動く。実行時の行き先 `generated.Banner` は ignore したディレクトリにあり、オラクルも計装しない (改訂 R21) ので `runtime` には書かない
- go コマンドが失敗するレイアウトかパッケージ: TCH-001〜003、DNG、BVD、SUM-002 (`nosum/caps`)
- モジュールのないファイル (オラクルは計装しない): NOM
- どの入口も動かさないモジュール: NAN-002 (`server` は公開版の api を使う)

## 再生成

`go.sum` と `vendor/` は `test_codes/_proxy` から作ったもの。手で編集しない。`_proxy` の zip が変わったら、作業用の写しで次の結果を作り、`go.sum` と `vendor/` だけを戻す。

| ファイル | 作り方 |
|---|---|
| `go.sum` (ws/app、ws/lib、wsvendor/svc、modvendor、nosum、badvendor、nonancestor/server、depnewgo/m) | `test_codes/_proxy/sums.txt` の 2 行ずつ (`nosum` は uuidlite の行だけ。`badvendor` は v1.1.0 の行) |
| `wsvendor/vendor/` | `wsvendor/svc` で `go work vendor` |
| `modvendor/vendor/` | `modvendor` で `GOWORK=off go mod vendor` |
| `badvendor/vendor/` | go.mod の yamlish を v1.0.0 に戻した写しで `GOWORK=off go mod vendor` (不一致は意図したもの) |

## 道具

`codedoc_go/` で実行する。

```bash
python3 test_codes/_gt/generate_manifest.py test_codes/go_layouts
python3 test_codes/_gt/check_ground_truth.py test_codes/go_layouts
python3 test_codes/_gt/run_oracle.py test_codes/go_layouts
```

## 守ること

- 囮と壊れたレイアウトは検証の本体。整理しない。`_old.go`、`.dot.go`、`gen.go`、cgo のファイル、二つのシンボリックリンク、`ws/extra`、存在しない `ws-missing/absent`、`go 1.28` の行、祖先でない `nonancestor/tools/go.work`、`twowork/scratch/go.work`、`badvendor` の古い `vendor/` が対象。
- フィクスチャの中で go コマンドを動かさない。ワークスペースでは `go.work.sum` が書かれる。写しで動かす。
- 製品コードにコメントを書かない。
