# review2

goracle の 2 回目のレビューで見つかった誤判定の回帰例（`TestReview2Probes`）。`q` から `s` までのエッジはどれも正しく、どれもかつて誤りの判定を受けた。`w` は誤ったエッジで、panic 中の遅延呼び出しの数え方を試す。

| パッケージ | 試すこと |
|---|---|
| `q` | 1 行に 2 つのメソッドを書いたインタフェース。相手は 2 つ目を行と qualname だけで指す |
| `v` | panic を recover した後に runtime.deferreturn が動かす遅延呼び出し。itab のラッパー、`-fm` のメソッド値、昇格メソッドを通る |
| `x` | 動かない遅延呼び出し（`os.Exit`）。`cmd/stop` がそれで終わる |
| `y` | `cmd/serve` が戻るとき、まだ止まっているゴルーチンの遅延呼び出し |
| `k` | range-over-func の本体にある関数リテラルで、runtime.gopanic の下で動く遅延呼び出し。実行時の名前は `Loop-range1.1` |
| `n` | nosplit の実装。フックが無い |
| `pm` | go test で動くパッケージ main。実行時の名前は import パスで始まる |
| `m` | 計装していない呼び出しが panic して（recover される）、文が終わらない |
| `r` | 引数の評価で panic する `defer report(compute())`。`report` は積まれず、実行の計数も積まれない |
| `s` | `keep(boom(), strings.ToUpper("x"))` で `boom` が panic する。文の前の到達の計数は最初に動く `boom` の分だけで、`ToUpper` と `keep` は実行されたことにならない |
| `w` | panic 中に動く遅延呼び出し。`defer c(1)` は deferwrap が持ち主の `G` を示すので、`F` の `defer a()` から `c` への誤ったエッジは DEFER-GROUP-OTHER。`defer d()` は deferwrap を持たないので、`E` の `defer a()` から `d` への誤ったエッジは何も言わない（README の知られた限界） |
