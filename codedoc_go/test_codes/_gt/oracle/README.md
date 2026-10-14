# goracle

`run_oracle.py`、評価（`evaluate`）、実リポジトリの試験（`trial`）が使う実行時オラクル。標準ライブラリだけの Go で書き、go1.27 の linux/amd64 に合わせてある。判定の規則と表は [LSP_RESOLUTION.md 第 11.2 節](../../../docs/LSP_RESOLUTION.md#112-実行時オラクルgoracle)にあり、ここには仕組みと知られた限界だけを書く。

## コマンド

| コマンド | すること |
|---|---|
| `instrument` | 木を写して計装し、サイトの表 `_goracle/sites.jsonl` を書く |
| `scan` | 計装せずにサイトの表だけを作る |
| `report` | 入口が書いた `hits-*.jsonl` から観測 `obs.jsonl` を作る |
| `match` | 解決の記録（エッジ）を観測で採点する |
| `judge` | 正解のケースを観測で判定する |

## 計装

- 関数本体と関数リテラルの先頭に `__goracle.E(fid)` を入れる。呼び出し元の戻り番地を frame pointer で読み、初めて見た組だけ `runtime.Callers` で確かめる。
- サイトには `(` の前後の `/*line*/` 指示子で ID を付ける。トークンの位置は helper の `extract.Sites` と同じで、名前の無い呼び出し（`(*fp)()`、型リテラルへの変換）は `(` の位置。
- 計数 `__goracle.D(id)` は 3 種類ある。

| 種類 | 置く場所 | 数えるもの |
|---|---|---|
| 完了（`stmt`） | 呼び出しを含む単純な文の後ろ | 文が終わった回数 |
| 到達（`reach`） | その文の前、`return`、`if`・`for`・`switch` の見出し、`go`、`defer`、`panic` の前 | 文が始まった回数。単純な文では、最初に動く呼び出し（ブロックの最後の文と `panic` では一番外の呼び出しも）の分だけ |
| 実行（`run`） | `defer` が 2 つ以上ある関数の各 `defer` 文の直後に置く `defer __goracle.D(k)` | その文の遅延呼び出しが動く回数（後に積んだものが先に動くので、利用者の呼び出しの直前に動く。文が呼び出しを積めなかったとき、たとえば引数の評価で panic したときは積まれない） |

## 帰属（`report`）

- runtime.deferreturn が動かした遅延呼び出しは、frame pointer の連鎖をラッパー（deferwrap、itab のポインタラッパー、昇格メソッド、`-fm` のメソッド値）越しに最大 8 段たどって見分け、その関数の defer の集まりに入れる。
- 関数は hits が報告する実行時の名前で探す。表の予測と違うのは、range-over-func の本体にある関数リテラル（`Loop-range1.1`）と、go test で動くパッケージ main（名前が import パスで始まる）。それ以外の違いがあれば `report` が WARNING の行を出す。
- runtime.gopanic か runtime.Goexit が動かした遅延呼び出しは、どの集まりにも入れない。deferwrap のクロージャが連鎖に見えればその親の関数の分として数え、見えなければ panic したスタック上の `defer` を持つ関数すべての分として数える。

## 知られた限界

| 限界 | 起きること | 判定への影響 |
|---|---|---|
| 引数の無い関数値の `defer`（`defer c()`）は deferwrap を持たない | panic 中に動くと、スタック上の `defer` を持つ関数すべての分になる | その関数たちの defer の集まりで、`c` への誤ったエッジが DEFER-GROUP-OTHER にならず UNEXERCISED（`judge` は UNVERIFIABLE）になる。誤りを隠すことはあっても、誤りを作ることはない |
| リポジトリの外の型に埋め込まれたインタフェースを通る二度目の振り分けと、リポジトリの外の実装による転送 | 実行時に見えない（`(*W).M` は末尾呼び出し） | 解決の記録が `candidates_partial` を持つか open world なので、評価と試験は候補を渡さず、メソッド名だけを確かめる |
| 到達の計数は文が始まったことしか示さない | 単純な文の前の到達の計数は、最初に動く呼び出し（ブロックの最後の文と `panic` では一番外の呼び出しも）の分だけ。(a) ブロックの最後の文で内側の呼び出しが panic すると、一番外の呼び出しも実行されたことになる（`fmt.Println(boom())` は CONSISTENT-NOEXEC、`keep2(boom())` は MISS）。(b) 前の呼び出しが戻った後で後ろの呼び出しが panic すると、後ろの呼び出しは実行されたことにならない（`regexp.MustCompile(fmt.Sprintf(…))` は NOT-RUN。最初の呼び出しが変換 `string(b)` のときも同じ）。`return` や `if` の見出しは見出しのサイトすべての分 | どれも文の途中で panic したときだけ起きる。正確にするには、呼び出しごとに最後の引数を恒等関数で包む計数（`f(a, __goracle.R(k, b))`）が要るが、型情報が無いと型を推論できない引数（型の無い定数など）がある |
| 実行の計数は `defer` が 2 つ以上ある関数にだけ置く | `defer` が 1 つの関数は、その `defer` のサイトの観測で判定する | なし |
| nosplit、アセンブリの関数にはフックが無い | 相手として観測できない | UNVERIFIABLE |
| ビルドに `-gcflags=all=-l -ldflags=-checklinkname=0` が要る | インライン化された関数にはフレームが無い。終了時の書き出しは internal/runtime/exithook への linkname を使う | `run_oracle.py` と試験が付ける |

panic 中の遅延呼び出しの数え方は、誤ったエッジを見逃すことはあっても、正しいエッジを誤りにすることはない。持ち主の分からない呼び出しをスタック上の `defer` を持つ関数すべての分として数えるので、その関数たちの `defer` の集まりでは「panic の下で動いた」として何も言わない（`match` は UNEXERCISED、`judge` は UNVERIFIABLE）。例: `G` が `defer c()` をして panic し、`G` を呼んだ `F` が recover するとき、`F` の `defer a()` から `c` への誤ったエッジは DEFER-GROUP-OTHER にならない（`testdata/review2/w`）。

回帰の例は `testdata/review2/`（`TestReview2Probes`）と `testdata/constructs/`（`TestConstructsJudge`）にある。
