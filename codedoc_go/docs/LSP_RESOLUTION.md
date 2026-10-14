# Phase 1.6: 呼び出し先の解決

Phase 1.6 は、呼び出し・埋め込み・実装・import の相手を go/types で確定します。言語サーバー（gopls などの LSP）は使いません。Go で書いた補助プログラム `gohelper` が `golang.org/x/tools/go/packages` でグループごとにパッケージを型付きで読み込み、Python がその答えを位置で分類してゲートを通します。ファイル名はほかの版と揃えて `LSP_RESOLUTION.md` のままにしています。
解決結果がどの表のどの行になるかは [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md)、モジュールとリポジトリの境界（グループの決め方、ピア、`crossref`）は [CROSS_PROJECT.md](CROSS_PROJECT.md)、フェーズの流れは [ARCHITECTURE.md](ARCHITECTURE.md#2-フェーズとトラック)、設定・オフラインとダウンロード・トラブルシューティングは [../README.md](../README.md) に譲ります。

## 1. 問題: 構文だけでは相手が分からない

go/parser は「どこで何という名前を呼んでいるか」までは正確に取れます（Phase 1.5）。しかし、その名前がどの宣言に着くかは、import、型、埋め込み、ビルド制約、モジュールの構成を知らないと決まりません。フィクスチャ `test_codes/iryo-kanri-system`（F1）は、名前だけで繋ぐと間違える場面を集めたものです。

| 場面 | F1 の例 | 名前だけで繋ぐと |
|---|---|---|
| 同名の宣言 | `Logger.Log`（`kernel/logging` と `pharmacy/logging`）、`Validator.Validate`、`ReceiptFormatter.Format` 3 種、`Calculate` 3 種 | どれか 1 つに繋ぐ |
| import だけが違う同じ行 | 受付の `closing/daily_close.go` と管理の `batch/daily_batch.go` は、対になる手続きの行（5 対）がバイト単位で同じで、import 先だけが違う | 両方を同じ相手に繋ぐ |
| 同じ import でもグループで行き先が違う | バイト単位で同じ 2 つの `textreport.go` の `textfmt.PadRight` は、受付ではリポジトリ内のフォーク、`go.work` の外にある管理コンソールでは公開版に着く | 両方をリポジトリ内に繋ぐ |
| 埋め込みと昇格 | `ip.Touch()` は 3 段下の `Entity.Touch` に着く。`Patient.Describe` が `Entity.Describe` を隠す | 宣言の場所を見誤る |
| インタフェース | `n.Send(msg)` の実装は `MailNotifier` と `*SmsNotifier`。監査の `l.Printf` に実行時に入るのは標準ライブラリの `*log.Logger` | 実装の 1 つに繋ぐ |
| ジェネリクス | `store.NewMemoryStore[patient.Patient]()`、制約 `Describer` を通した `it.Describe()` | 具体化した名前で探して見つからない |
| 関数値 | `round := billing.Round`、`Hooks["tax"](2000)`、関数型のフィールド `checker.Validate` | 何も繋がらないか、同名のメソッドに繋ぐ |
| ビルド制約 | `Name` は `platform_linux.go`、`platform_windows.go`、`platform_other.go` にある。`AddPoints` は amd64 ではアセンブリで実装する本体の無い宣言（`add_amd64.go`）、ほかでは `add_generic.go`。`//go:build iryodebug` のファイルは既定のビルドに入らない | どれか 1 つに繋ぐ |
| テストの変種 | `escalation.Evaluate` の `p.Level()` は、本体では `rule.Level`、テストでは `escalation_test.go` の `Policy.Level` に着く | 片方に固定する |
| `//go:linkname` | 本体の無い宣言 `bump` の呼び出しは、実行時に `internal/tally.bump` に着く | 手元の本体の無い宣言に繋ぐ |
| 変換 | `money.Yen(2880)` は呼び出しではない | 型を呼び出し先にする |
| オフライン | F1 にはない。2026-10-13 の 20 行の試験で、依存モジュールが欠けると埋め込みの昇格が別のリポジトリ内のメソッドにすり替わることを確かめた | 誤った線を張る |

このため CodeDoc は「相手を確定できた呼び出しだけをエッジにし、確定できないものは別扱いにする」方針を取ります。誤ったエッジを 0 本にすることを優先し、確かでないときは答えません（`unresolved`、`dynamic`、エッジなし）。

## 2. なぜ go/types か

ほかの版は呼び出しサイトごとに言語サーバーの `textDocument/definition` を送ります。Go 版は送りません。gohelper がグループのパッケージを 1 回読み込み、`go/types` の `types.Info`（`Uses`、`Selections`、`Instances`）から全サイトの相手をまとめて取ります。2026-10-13 に etcd v3.7.2 と Prometheus v3.15.0 で比べた結果です（go1.27.2、gopls v0.23.0、golang.org/x/tools v0.52.0）。

| 観点 | gohelper（go/packages と go/types） | gopls（サイトごとの `definition`） |
|---|---|---|
| 答え | 187,431 サイト（etcd 72,623、Prometheus 114,808）のうち、両方が相手を返したところはすべて一致した。gopls だけが答えた 422 サイトは計測側のトークン位置の誤りで、どちらも答えなかったのは 3,848 サイト | 同左 |
| 時間 | 2.3 秒（etcd）、5.7 秒（Prometheus）。インタフェースの実装の探索を含む | `definition` だけで 40〜45 秒、60〜65 秒（逐次） |
| 最大メモリ | 1.5〜1.7 GB、4.7〜5.3 GB | 4.7〜5.5 GB、4.9〜7.2 GB。`implementation` を 400 回送ると 12 GB |
| 型の事実 | 受け手の静的型、埋め込みの経路と深さ、変数の種類、具体化、不正な型が分かる | 位置しか返さない |
| オフラインの誤り | 誤りの原因（go/packages と go/types）は同じなので、どちらも誤る。型の事実で見つけてゲートで止められるのは gohelper だけ（[第 8 節](#8-ゲート)） | 見つけられない |

gopls の `definition` は、関数値の呼び出しには変数を返し、`//go:linkname` の呼び出しには手元の本体の無い宣言を返します。実行時に走るのはリンク先です。gohelper も素の答えは同じなので、[第 7 節](#7-規則)の規則で直します。

## 3. 読み込み

### 3.1 グループとビルド構成

- 一緒に読み込むモジュールの組をグループと呼びます。グループは go コマンドが実際にビルドする単位（`go.work` のワークスペース、または `GOWORK=off` の単一モジュール）に合わせます。決め方は [CROSS_PROJECT.md](CROSS_PROJECT.md#2-1-つのリポジトリの中のモジュール) にあります。
- グループが実行時の真実を決めます。モジュールをまたぐ呼び出しがリポジトリ内のエッジになるのは、go コマンドがリポジトリ内のソースをコンパイルするときだけです。F1 では、受付の `textfmt.PadRight` は `go.work` の `use` でリポジトリ内のフォークに着き（`resolved`）、管理コンソールの同じ行は公開版 v1.1.0 の `external` です。
- gohelper は（グループ × ビルド構成）ごとに 1 回動きます。ビルド構成の設定は [README](../README.md#設定リファレンス) にあります。主ビルド構成が先で、追加の構成（`GO_EXTRA_BUILDS`）は主ビルド構成が外したファイルだけのために動きます。ファイルは、そのファイルを含む最初の構成で解決します。

### 3.2 gohelper resolve の読み込み

| 項目 | 値 |
|---|---|
| API | `packages.Load` |
| モード | `NeedName`、`NeedFiles`、`NeedSyntax`、`NeedTypes`、`NeedTypesInfo`、`NeedDeps`、`NeedImports`、`NeedModule`、`Tests: true` |
| パターン | ワークスペースのグループは `work`、単一モジュールのグループは `./...` |
| ビルドフラグ | `-buildvcs=false`。単一モジュールのグループは `-modfile=<制御ディレクトリ>/go.mod`。タグは `-tags` |

`NeedDeps` で依存パッケージも型付きで読みます。標準ライブラリや依存モジュールの非公開の型がインタフェースを満たすかどうか（[第 6 節](#6-インタフェースの呼び出しと-open-world)の `open_world`）を調べるためです。テストも読むので、1 つのファイルが本体の変種（`p`）とテストの変種（`p [p.test]`）の両方に現れます。

### 3.3 環境と制御ディレクトリ

go コマンドと gohelper は次の環境で動きます。呼び出し元から引き継ぐのは `HOME` と `USER` だけです。

| 変数 | オフライン（既定） | ダウンロード |
|---|---|---|
| `PATH` | ツールチェーンの `bin`、`/usr/bin`、`/bin` | 同左 |
| `GOROOT`、`GOTOOLCHAIN` | そのツールチェーン、`local` | 同左 |
| `GOENV` | `off` | 同左 |
| `GOFLAGS` | `-mod=readonly -modcacherw -buildvcs=false`。vendor したグループは `-mod=vendor` | 同左 |
| `GOWORK` | 生成した `go.work` の絶対パス、または `off` | 同左 |
| `GOPROXY` | ユーザーのモジュールキャッシュを読む `file://<キャッシュ>/cache/download`。キャッシュが無ければ `off` | gohelper の読み込みは `GO_PROXY`。その前のモジュールの取得（`go list`）は、ユーザーのキャッシュの `file://` のあとに `GO_PROXY` |
| `GOSUMDB` | `off` | `GO_SUMDB` |
| `GOVCS`、`GOAUTH` | `*:off`、`off` | 同左 |
| `CGO_ENABLED`、`GOOS`、`GOARCH` | ビルド構成から | 同左 |
| `GOMODCACHE` | `OUTPUT_DIR/go/modcache` | 同左 |
| `GOCACHE`、`GOPATH`、`TMPDIR`、`GOTMPDIR` | `OUTPUT_DIR/go/` の下 | 同左 |
| `XDG_CONFIG_HOME` | `OUTPUT_DIR/go/xdg`。テレメトリは off | 同左 |

`GOPRIVATE`、`GONOPROXY`、`GONOSUMDB`、`GOINSECURE`、`GOCACHEPROG`、`GOEXPERIMENT`、`GODEBUG`、`CC`、`PKG_CONFIG`、`CGO_*` は設定しません。

読み込みはグループごとの制御ディレクトリ（置き場所は [README](../README.md#出力)）から行います。

- **ワークスペースのグループ**: リポジトリの `go.work` から `go`、`toolchain`、`godebug` の行を写し、`use` をディレクトリの絶対パスで書いた `go.work` を作ります。replace のローカルパスも絶対パスにし、ピアの replace を足し、リポジトリの `go.work.sum` を写します。ワークスペースの `vendor/` があればシンボリックリンクを置きます。存在しない `use` は外し、グループを縮退（`degraded`）として警告します。
- **単一モジュールのグループ**: `GOWORK=off` で、go.mod のコピー（vendor の無いグループではピアの replace を足したもの）と go.sum のコピーを `-modfile` で渡します。

### 3.4 読み込みの罠

| 罠 | 扱い |
|---|---|
| go/packages は、読み込めなかった import の代わりの空のパッケージ（スタブ）に、リポジトリ内のファイルを付けることがある（golang/go#39986） | （ファイル、パッケージ）の組は、パッケージがグループの主モジュールに属するときだけ採る |
| スタブが本物の import の名前を名乗る | 相手のパッケージが本物（ワークスペース、依存、標準ライブラリ）でなければ採らない（G2） |
| 1 つのファイルのパッケージ変種（本体 `p`、テスト `p [p.test]`、シンボリックリンクの別のパス） | サイトを（実パス、オフセット）で重ね合わせる。変種で相手が違えば G1 |

サイトは（リポジトリからの相対パス、呼び出し先のトークンのバイトオフセット）で entities.json と結び付けます。gohelper は読んだファイルの SHA-256 を返し、entities.json のハッシュと合わないファイルのサイトは使いません。鮮度の全体は [ARCHITECTURE.md](ARCHITECTURE.md#6-チェックポイント鮮度キャッシュ) にあります。

## 4. サイトごとの分類

gohelper は Phase 1.5 のサイト（呼び出し式、`go` 文、`defer` 文、range-over-func）ごとに、`types.Info` から呼び出し先の種類（クラス）を決めます。

| 呼び出し先 | クラス | 扱い |
|---|---|---|
| 組み込み（`len`、`append`、`min`、`clear`、`new`、`panic`、`unsafe.Sizeof`）と、宣言済みの `error` の `Error` | `builtin` | `builtin` |
| 名前付きの型（`T(x)`、`(*T)(p)`） | `conversion` | サイトから外し、`stats.conversions` に数える。どの率にも入れない |
| パッケージの関数（ジェネリックを含む） | `func` | 相手はジェネリックの原型（`Origin()`） |
| 具体型のメソッド（昇格したもの、メソッド式を含む） | `method` | 埋め込みの深さと経路を記録する |
| インタフェースのメソッド（無名インタフェース、構造体に埋め込んだインタフェースのフィールド経由を含む） | `iface` | [第 6 節](#6-インタフェースの呼び出しと-open-world) |
| 型パラメータの制約のメソッド | `typeparam` | `iface` と同じに扱う |
| 関数型の変数、引数、フィールド、map やスライスの要素、呼び出しの結果 | `value` | `dynamic`。[第 7 節](#7-規則)の func-alias だけが例外 |
| その場で呼ぶ関数リテラル | `funclit` | 相手はクロージャ |
| `//go:linkname` を持つ本体の無い宣言 | `linkname` | リンク先に付け替える |
| 不正な型の上のセレクタ、引けない名前 | `unknown` | `unresolved`（`missing-module` か `load-error`） |

- range 文の右辺が関数値かメソッド値のときだけ range サイトです（`for slot := range book.Each`）。右辺がスライスや map なら呼び出しではないので外し、`stats.notcalls` に数えます。`for slot := range book.All()` は内側の `All()` の呼び出しだけがサイトです。
- 各サイトには、相手の宣言の名前の位置（実パス、行、列）、パッケージ、モジュールパスと版、ゲートに要る事実（`promoted_depth`、`embed_invalid`、`stub`、`pkg_errors`、`variants`、`asm`）が付きます。

## 5. 定義位置の分類

gohelper が返した相手の位置を、Python が実パスで次の順に分類します。最初に当たった行を使います。

| # | 位置 | 結果 |
|---|---|---|
| 1 | `$GOROOT/src/builtin/` か `$GOROOT/src/unsafe/` | `builtin` |
| 2 | ほかの `$GOROOT/src/` | `external`、モジュール `std` |
| 3 | ピアのリポジトリのモジュールのディレクトリ | `external`。`{repo, relpath, qualname}` を持ち、`crossref` がリポジトリ間のエッジにする（[CROSS_PROJECT.md](CROSS_PROJECT.md)） |
| 4 | 隔離したモジュールキャッシュ（`<module>@<version>/…`） | `external`。モジュールパスと版はキャッシュのパスから取る |
| 5 | `vendor/` | `external`。モジュールは `vendor/modules.txt` の最長一致 |
| 6 | このリポジトリのスキャンしたファイル | 宣言の名前の範囲でノードを引く |
| 7 | このリポジトリのスキャンしないファイル（`testdata/`、`_x/`、`.x/`、go.mod の `ignore`、シンボリックリンクの重複） | `unresolved`（`unscanned`） |
| 8 | `$GOCACHE` の cgo のファイル（`_Cfunc_…`） | `external`、モジュール `C` |
| 9 | それ以外（ピアでない、リポジトリの外の `use` や `replace` のディレクトリ） | `unresolved`（`outside-repo`） |

行 6 は、相手の位置が期待する種類のノードの名前の範囲と一致するときだけノードに着きます（関数・メソッド・インタフェースのメソッドは Methods、型は Classes）。囲む宣言に寄せることはしません。関数の本体の中の型や、型リテラルとインラインの制約の中の位置が、囲む関数のノードに着くことはありません。

- パッケージレベルの名前付きの型でないインタフェース（無名インタフェース、関数の中で宣言したインタフェース、インラインの制約）は、gohelper が `recv_type` を空で返し、[第 6 節](#6-インタフェースの呼び出しと-open-world)の無名インタフェースとして扱います。
- それ以外で一致するノードが無い相手は G7（`no-declaration`）です。

## 6. インタフェースの呼び出しと open world

Go のインタフェースは宣言なしに満たされます。正しいメソッドの組を持つ型なら、どこで宣言されていても実装です。そこで次のように扱います。

1. **静的な相手。** インタフェースがこのリポジトリにあれば、`iface` のサイトはインタフェースのメソッドに `resolved` します（`via` は `types`）。MethodCalls はこのメソッドに張ります。インタフェースがリポジトリの外なら `external` です。C#、Java、Python の版が抽象メソッドに張るのと同じ、静的な束縛です。
2. **候補。** 受け手の静的型を満たす型を、そのグループの読み込みにある、リポジトリ内のパッケージレベルの非ジェネリックな具体型から集めます（T と `*T` を `types.Implements` で調べる）。候補はその型のメソッドで、昇格していれば宣言したメソッドです。
   - go/types は、型の不正な埋め込みフィールドを持つ型を、どのインタフェースも満たすと答えます。そこで gohelper は、インタフェースの要るメソッドを 1 つずつ、T か `*T` のメソッドの組で確かめます（R52）。勝つメソッドの型が違う型は実装ではなく、候補にも実装の組にもしません。候補の集合は閉じたままで、`candidates_partial` も付けません。要るメソッドが無い、勝つメソッドがスタブのパッケージにあるかその型に不正な型が絡む、勝つメソッドが昇格したもので [G3](#8-ゲート) の条件に当たる、のどれかなら、実装かどうか分かりません。リポジトリ内にそういう型が 1 つでもあれば、そのインタフェースの呼び出しは候補を出さず（[G4](#8-ゲート)）、その型とインタフェースの組には `incomplete` が付きます（[第 10 節](#10-resolutionsjson)）。F1 の `Tracker` は `uuidlite.Sequence` を埋め込み、オフラインではこの型が不正になります。それでも `*Tracker` では自分の `Get() float64` が勝ち、型が `Source.Get` と違うので、`lookup.Source` の実装ではありません。
   - 型が自分の埋め込みインタフェースのフィールドでメソッドを得るときは、二段目のディスパッチです。候補は埋め込まれたインタフェースのメソッドで、`via_embedded_iface` が付きます。F1 の `Publish(j Journal, …)` の `j.Write` では、`AuditTrail` がこれに当たり、候補は `Sink.Write` です。
   - 構造体に埋め込んだインタフェースのフィールド経由の呼び出し（`AuditTrail.Note` の `t.Write(...)`）は、そのフィールドの型 `Sink` でディスパッチします。インタフェース型は候補になりません。
   - `_test.go` で宣言した型の候補には `test_only` が付きます（F1 の `fakeClock.Now`）。
   - 候補はグループの読み込みにある型だけです。F1 の管理コンソールのプログラムは予約モジュールを読み込まないので、そこでの `n.Send(...)` の候補は `MailNotifier.Send` だけです。ワークスペースのグループでの同じインタフェースの呼び出し（`kernel/notify` の `n.Send(msg)`）には `SmsNotifier.Send` も入ります。
3. **`open_world`。** 候補で尽くせない型もサイトに来うるとき true です。候補と `candidates_partial` は変わりません。
   - リポジトリの外の型（標準ライブラリ、依存モジュール。非公開の型を含む）が満たす。実装かどうか分からないリポジトリの外の型も、満たす型として数えます。F1 では、監査の `Logger` を `*log.Logger` が、フォークした textfmt の `Formatter` を `time.Time` が満たします。
   - 別のグループが、サイトのモジュールのリポジトリ内のディレクトリを、go コマンドが適用するローカルの `replace`（go.mod か go.work）でビルドに入れる（R54）。そのグループの型はサイトのグループの読み込みに無く、引数として渡りうるからです。モジュール単位で決め、そのグループがサイトのパッケージを import するかどうかは問いません。そのグループの型から実装は探さず、控えめに true にします。F1 では、管理コンソールの go.mod の replace が kernel、domain、billing、pharmacy、reporting をビルドに入れるので、これらのモジュールのインタフェース呼び出しはすべて true です。
4. **エッジ。** インタフェースのメソッドに MethodCalls を、グラフにある各候補に PossiblyCalls（`reason` は `implementation`、列 `open_world` 付き）を張ります。候補が `POSSIBLY_CALLS_MAX_CANDIDATES` を超えるサイトは、PossiblyCalls を 1 本も書かず、数だけを数えます。
5. **格上げしない。** リポジトリ内の実装が 1 つしかなくても、その実装を MethodCalls にはしません。etcd の `testutil.TB` はリポジトリ内の実装が 1 つですが、`*testing.T` も満たします。F1 の監査の `l.Printf` に実行時に入るのは `*log.Logger` です。
6. **無名インタフェース。** 無名インタフェース、関数の中で宣言したインタフェース、インラインの制約のメソッドはノードを持ちません。リポジトリ内の実装を候補にした `ambiguous`（`reason` は `implementation`）にし、実装が無ければ `dynamic` にします。どちらも MethodCalls を張らず、囲む関数を相手にしません。
7. **宣言済みの `error`。** `err.Error()` は `builtin` で、候補を持ちません。

## 7. 規則

規則は契機に当たるときだけ働き、`via` に `convention:<規則>` を記録します。

| 規則 | 契機 | 結果 | F1 の例 |
|---|---|---|---|
| `funclit` | その場で呼ぶ関数リテラル（`go func(){…}()`、`defer func(){…}()` を含む） | クロージャ（`F.func1` など）に `resolved` | `go func() {` → `Run.func3` |
| `func-alias` | 一度だけ束縛したローカル変数の関数値の呼び出し。初期化子が関数、具体型の受け手のメソッド値、メソッド式、関数リテラルのどれかで、再代入、アドレスの取得、クロージャの中での代入がどれも無い（すべて go/types で確かめる） | 束縛した相手に `resolved` | `round := billing.Round` の `round(1234)` → `Round` |
| `linkname` | `//go:linkname local target` を持つ本体の無い宣言 | リンク先がリポジトリ内なら `resolved`、外なら `external` | `bump` → `internal/tally.bump`、`nanotime` → `runtime.nanotime`（`std`） |
| `range` | 関数値かメソッド値を range する暗黙の呼び出し | その関数・メソッドの呼び出しと同じに扱う（リポジトリ内なら `resolved`、外なら `external`、インタフェースのメソッド値ならインタフェース呼び出し） | `for slot := range book.Each` → `AppointmentBook.Each` |

- func-alias とメソッド式は、選ばれたメソッドの宣言で決まります。インタフェースのメソッドなら、インタフェース呼び出しと同じに扱います（相手はインタフェースのメソッドで、候補付き）。F1 の `send := notifiers[0].Send`、埋め込みインタフェース経由の `write := trail.Write`、`kind := records.Record.Kind` がこれに当たります。具体型のメソッド式 `stamp := pharmacy.RubberStamp.Stamp` は `RubberStamp.Stamp` に `resolved` します。
- `//go:linkname` を持たない本体の無い宣言はアセンブリの実装で、宣言そのものに `resolved` します（gohelper の記録に `asm` が付きます）。F1 の `AddPoints`（`add_amd64.s`）です。
- パッケージレベルの関数変数は、テストが書き換えられるので単一の束縛とみなさず `dynamic` です。フィールド、引数、map やスライスの要素の関数値も `dynamic` です。F1 の負例は、再代入される `pick`、アドレスを取られる `keep`、クロージャの中で代入される `late`、パッケージ変数の `Fallback` です。

## 8. ゲート

| ゲート | 条件 | 結果 |
|---|---|---|
| G1 `variant` | 1 つのファイルのパッケージ変種で相手が食い違う | `ambiguous`（`variant`）。候補は変種ごとの相手 |
| G2 `missing-module` | 相手がスタブのパッケージにある、または受け手の型が不正 | `unresolved`（`missing-module`） |
| G3 `incomplete-type` | 昇格した選択（呼び出し、func-alias のメソッド値、range するメソッド値、メソッド式）の受け手の埋め込みの木に、同じ深さか浅いところで型の不正な埋め込みフィールドがある。または、受け手の型と、選んだ深さか浅いところの埋め込みの型を宣言したパッケージのどれかが、宣言を失ったかもしれない（下記）。呼び出す側のパッケージに import の誤りが無くても、どのパッケージでも調べる。パッケージ変種ごとに調べ、最も慎重な答えを採る | `unresolved`（`incomplete-type`） |
| G4 `iface-offline` | 次のどれかに当たるインタフェースの呼び出し。サイトのパッケージ（変種のどれか）に誤り（import の誤りを含む）がある。インタフェースのメソッドの組に不正な型が絡む。インタフェースのパッケージか、それが埋め込むインタフェース（入れ子を含む）のパッケージが、宣言を失ったかもしれない（下記）。埋め込むインタフェースの型が不正か、スタブのパッケージのもの。ディスパッチするインタフェースが分からない。実装かどうか分からないリポジトリ内の型がある（[第 6 節](#6-インタフェースの呼び出しと-open-world)） | 候補を出さない（PossiblyCalls なし）。静的な相手は残し、`open_world` は true、`candidates_partial` は `suppressed` |
| G5 `excluded` | サイトのファイルをどのビルド構成もコンパイルしない | `unresolved`（`excluded-by-build`）。名前で推測しない |
| G6 `toolchain` | グループの `go` 行がツールチェーンより新しい | `unresolved`（`toolchain`） |
| G7 `no-declaration` | 相手がスキャンしたファイルにあるが、名前の範囲が一致するノードが無い | `unresolved`（`no-declaration`） |
| G8 `stale` | 実行中にサイトのファイルか相手のファイルが変わった | `unresolved`（`stale`） |
| G9 `syntax-error` | サイトが最初の構文エラーの行か、それより後ろにある。昇格した選択と func-alias のメソッド値は、サイトのディレクトリか相手のディレクトリに構文エラーのファイルがあっても当てはまる（エラーで消えた宣言が、実行時に勝つ浅いメソッドかもしれないため） | `unresolved`（`syntax-error`） |
| G10 `build-variant` | 相手がビルドの双子（同じパッケージで同じ qualname の宣言が、互いに排他のビルド制約のファイルにある） | そのビルド構成がコンパイルする双子に `resolved`。ほかの双子は `build_variants` に記録し、エッジにしない |

- **G3 が要る理由。** 埋め込んだフィールドの型が欠けたモジュールのものだと、実行時に勝つ浅いメソッドが見えず、深いリポジトリ内のメソッドに着きます。2026-10-13 の 20 行の試験では、gopls も gohelper もこの誤ったリポジトリ内のエッジを返しました。
- **宣言を失ったかもしれないパッケージ（G3、G4、R43）。** 次のどれかに当たるパッケージです。構文エラーがある。別の package 句を持つために、型検査がファイルを丸ごと無視した。構造体の型の埋め込みフィールドが、名前の重複で落ちた。宣言をすべて残す誤り（解決できない import、本体の中や型の中の未知の名前、パッケージのどの型も指さない受け手のメソッド）は数えません。例: `p.S` が `r.Base` を埋め込み、`p` の構文エラーが `func (S) M()` を飲み込むと、別のパッケージの `p.S{}.M()` は `r.Base.M` に着いてしまいます。
- **読み込みの失敗。** 依存モジュールの新しい `go` 行、gohelper の異常終了、時間切れ、メモリ不足でグループの読み込みが失敗すると、グループの status は `error` になり、その全サイトが `unresolved` です。理由は、go コマンドが `requires go >=` と言えば `toolchain`、それ以外は `load-error` です。ほかのグループは続けます。
- **オフラインの go.sum。** 単一モジュールのグループで go.sum に要る行が無いと、そのパッケージは `unresolved`（`missing-module`）です。
- **モジュールの外。** どの go.mod の下にも無いファイルは抽出だけして解決しません（`no-module`）。

## 9. status、via、理由

| status | 意味 | グラフ |
|---|---|---|
| `resolved` | このリポジトリの相手が 1 つに決まった（関数、メソッド、クロージャ、インタフェースのメソッド） | MethodCalls。インタフェースなら候補が PossiblyCalls |
| `external` | 相手がリポジトリの外（標準ライブラリ、モジュール、C、ピア） | モジュールごとに数える。リポジトリの外のインタフェースでも、リポジトリ内の候補は PossiblyCalls。ピアの相手は `crossref` が使う |
| `builtin` | 組み込み | 数える。エッジ率の分母に入れない |
| `ambiguous` | 相手が 1 つに決まらない（G1 の変種、無名インタフェース） | 候補ごとに PossiblyCalls |
| `dynamic` | 関数値の呼び出し。リポジトリ内に実装の無い無名インタフェースの呼び出し | 数えるだけ |
| `unresolved` | 確かな答えが無い | 数えるだけ。名前の推測で PossiblyCalls を作らない。型の事実が欠けた場所なので、推測は雑音になる |

変換は status を持ちません。記録を作らず、`stats.conversions` に数えます。型リテラルを呼び出し先にする変換（`[]byte(s)`、`(*[N]byte)(p)`、map・chan・func・interface・struct のリテラル。括弧と `*` を通したものを含む）は、抽出（Phase 1.5）の時点で全ファイル（ビルドから外れるファイルを含む）のサイトから外し、左括弧のオフセットを FileRecord の `conversions` に残して、同じく `stats.conversions` に数えます。名前付きの型の変換は、上の表のとおりサイトに残り、解決で `conversion` に分類されます。

| `via` | 意味 |
|---|---|
| `types` | go/types が相手を確定した。無名インタフェースの `ambiguous` と `dynamic` も `types` |
| `convention:funclit` | その場で呼ぶ関数リテラル |
| `convention:func-alias` | 一度だけ束縛したローカル変数の関数値 |
| `convention:linkname` | `//go:linkname` のリンク先 |
| `convention:range` | range-over-func の暗黙の呼び出し |
| `none` | 規則に依らない外部への静的な呼び出し、`builtin`、関数値の `dynamic`、`unresolved` |

規則で決めた `external`（`//go:linkname` のリンク先が標準ライブラリなど）は、その規則の `via` を持ちます。PossiblyCalls の `reason` は `implementation`（インタフェースの候補）と `variant`（G1）です。

| `unresolved` の理由 | 意味 |
|---|---|
| `missing-module` | 要るモジュールがオフラインで手に入らない、スタブのパッケージ（G2） |
| `incomplete-type` | 型の欠けた埋め込みか宣言を失ったかもしれないパッケージがあり、昇格したメソッドが実行時に別の相手になりうる（G3）。実装の組では、gohelper が `incomplete` を付けた組（[第 10 節](#10-resolutionsjson)） |
| `excluded-by-build` | どのビルド構成もコンパイルしないファイル（G5） |
| `toolchain` | `go` 行がツールチェーンより新しいグループ（G6） |
| `no-declaration` | 名前の範囲が一致するノードが無い（G7） |
| `stale` | 実行中にファイルが変わった（G8） |
| `syntax-error` | 構文エラーより後ろの位置（G9） |
| `unscanned` | スキャンしないリポジトリ内のファイル（[第 5 節](#5-定義位置の分類)の行 7） |
| `outside-repo` | ピアでない、リポジトリの外のディレクトリ（行 9） |
| `no-module` | go.mod の下に無いファイル |
| `load-error` | パッケージを読み込めなかった |

## 10. `resolutions.json`

置き場所は `OUTPUT_DIR/repos/<repo>/resolutions.json` です。F1 の患者ポータルの `handleFees` から会計の `FeeCalculator.Calculate` への呼び出しは次の記録になります。この JSON は形を示す例で、`groups` と `stats` の数は実測値ではありません。

```json
{"version": 1,
 "engine": {"name": "go/types", "toolchain": "go1.27.2", "x_tools": "v0.52.0", "helper": "sha256:…",
            "entities_version": 1, "mode": "offline", "builds": ["linux-amd64"]},
 "repo": "iryo-kanri-system",
 "groups": {"ws0": {"status": "ok", "reason": null, "packages": 0, "errors": 0,
                    "builds": ["linux-amd64"], "message": ""}},
 "files": {"patient-portal/internal/server/server.go": {
   "sha256": "…",
   "calls": [{"site": {"caller": "Portal.handleFees", "kind": "call", "name": "Calculate",
                       "line": 36, "col": 43},
              "status": "resolved", "via": "types",
              "target": {"unit": "example.com/iryo/billing", "package": "example.com/iryo/billing",
                         "relpath": "billing/fee.go", "qualname": "FeeCalculator.Calculate",
                         "line": 13, "kind": "method"},
              "static_target": null, "external": null, "candidates": [], "open_world": false,
              "build_variants": [], "reason": null, "build": "linux-amd64"}],
   "imports": [], "embeds": [], "implements": []}},
 "stats": {"sites_total": 0, "by_status": {}, "by_via": {}, "by_reason": {}, "by_class": {},
           "external_by_module": {}, "conversions": 0, "notcalls": 0, "range_dropped": 0,
           "join_misses": 0, "stale": 0, "groups": {}, "seconds": 0.0}}
```

| フィールド | 内容 |
|---|---|
| `version` | 記録の形式の版（現在 1） |
| `engine` | 使ったツールチェーン、golang.org/x/tools の版、gohelper のソースのハッシュ、オフラインかダウンロードか、ビルド構成 |
| `groups.<id>` | グループの `status`（`ok`、`toolchain`、`error`）、失敗の理由、読み込んだパッケージ数、誤りの数、読み込んだビルド構成、警告か失敗の最初のメッセージ |
| `files.<relpath>.sha256` | 解決に使ったエンティティのハッシュ。実行中にファイルが変わったときは読み込みで見たハッシュ（消えていれば `deleted`）で、そのファイルの記録は G8 |
| `calls[]` | サイトごとの記録。`site` は呼び出し元の qualname、種類（`call`、`go`、`defer`、`range`）、名前、1 始まりの行、1 始まりのバイト列 |
| `target` | `{unit, package, relpath, qualname, line, kind}`。`kind` は `method` か `class`。ピアの相手は `repo` も持つ |
| `external` | `{module, version, package, qualname, peer}`。標準ライブラリは `std`、cgo は `C` |
| `candidates[]` | インタフェースの候補と `ambiguous` の候補。`target` の形に `test_only` と `via_embedded_iface` を足したもの |
| `candidates_partial` | 候補の集合が閉じていない `resolved`、`external`、`ambiguous` の記録だけが持つ。値は当てはまる最初の理由で、`suppressed`（G4 で候補を出さなかった）か `via_embedded_external`（埋め込みインタフェースを通る候補がリポジトリの外にあり、ノードが無いので外した。そのフィールドの実行時の値はリポジトリ内の型かもしれない）。記録を `unresolved` にするゲートはこのフィールドを消す。PossiblyCalls の行は変わらない |
| `open_world` | [第 6 節](#6-インタフェースの呼び出しと-open-world) |
| `static_target` | ほかの版との互換のための列で、Go 版では常に null |
| `build_variants` | G10 で選ばなかった双子 |
| `reason` | `unresolved` の理由、`ambiguous` の `variant` か `implementation` |
| `build` | そのサイトを解決したビルド構成の ID |
| `imports[]`、`embeds[]`、`implements[]` | import の相手（リポジトリ内のパッケージか外部モジュール）、埋め込んだ型、（具体型、リポジトリ内のインタフェース）の組。実装かどうかは候補と同じくメソッドごとに確かめ（[第 6 節](#6-インタフェースの呼び出しと-open-world)の R52）、実装でない型の組は作らない。実装かどうか分からない組と、インタフェースのパッケージか、それが埋め込むインタフェースのパッケージが宣言を失ったかもしれない組（[第 8 節](#8-ゲート)）は、gohelper が `incomplete` を付け、Phase 1.6 が `status` を `unresolved`、`reason` を `incomplete-type` にして残す。ClassImplements にも CrossRepoRef にもしない |
| `stats` | status、`via`、理由、クラス、外部モジュールごとの件数、変換の数、呼び出しでない range の数（`notcalls`）、gohelper の答えが無かった range サイトと通常のサイトの数（`range_dropped`、`join_misses`）、G8 にした記録の数、グループごとの時間とメモリ、所要秒数 |

このファイルを再利用しないことと、グラフのチェックポイントとの関係は [ARCHITECTURE.md](ARCHITECTURE.md#6-チェックポイント鮮度キャッシュ) にあります。

## 11. 正しさの検証

### 11.1 `evaluate`

`python -m graph_generator evaluate` は 3 つのフィクスチャを、GCP もネットワークも使わずに採点します。作業ディレクトリは毎回作り直します。

| モード | モジュールの出どころ |
|---|---|
| 既定 | 空のモジュールキャッシュと `GOPROXY=off`。第三者モジュールへの呼び出しは `unresolved`（`missing-module`）になり、未回答として数える（誤りではない） |
| `--env` | `GOPROXY=file:///<codedoc_go>/test_codes/_proxy`、`GOSUMDB=off`、隔離したキャッシュ。ネットワークは使わない |

| 指標 | 数えるもの | F1 | F2 | F3 |
|---|---|---|---|---|
| 誤り | 下の誤りの規則に当たる記録を持つケース | 0 | 0 | 0 |
| 完全性 | 正解か、記載された代替と判定されたケース。インタフェースのケースに代替は無い | ≥ floor(0.85·N)+1 | 同左 | 同左 |
| ノードカバレッジ | 宣言アンカー（`DECL_PINS`）のうちノードとして存在するもの | ≥ 0.85 | — | — |
| エッジ率 | エンティティのサイトから、変換、呼び出しでない range、捨てた range の候補、`builtin` を除いたもののうち、`resolved` か `external` の記録を持つもの。記録を失ったサイトは未回答として数える | ≥ 0.85 | — | — |
| QA | `qa_questions.json` の質問を導出した行から答えた正答率 | ≥ 85% | — | — |
| import | 確定したリポジトリ内の import の記録すべてに PackageImports の行がある（ディレクトリの複数のパッケージ句に結び付きうるものは除く） | 一致 | — | 一致 |
| 読み込み | 1 つ以上のグループを読み込み、どのエンティティのサイトもちょうど 1 つの記録を持ち、エンティティと gohelper の答えの突き合わせ漏れ（`join_misses`）が 0。F1 と F3 は全グループが読み込みの後に `ok` であること | 一致 | 一致 | 一致 |
| オラクル | goracle のサイトの表と全記録を照らす。名前のあるサイトの `via` が `types` の記録は、相手と各候補の qualname の最後の部分がサイトの名前と一致すること。記録の呼び出し元は、表がそのサイトを囲む関数と一致すること（goracle が理由付きで飛ばすファイルは名前だけを調べる）。表にあるファイルで表に無いサイトと、表が知らないファイルも問題に数える。F1 と F3 は加えて、`resolved` のメソッドの記録すべてを入口の観測で `goracle match` にかけ（候補の渡し方と判定は[第 11.3 節](#113-実リポジトリの試験)）、誤りの判定、NO-SITE、NO-TARGET、失敗した入口、オラクル自体の失敗が 0 | 一致 | 一致 | 一致 |
| クラスの辺 | `ClassImplements` と `ClassEmbeds` の行が、manifest の `class_edges` と（ポインタの列を含めて）完全に一致。`modes` のある辺はそのモードだけで数える | 一致 | — | — |
| 発見 | モジュール、グループなどの発見の結果（各グループの読み込みの後の status を含む）が `go_layouts/ground_truth/expected_units.json` と一致 | — | 一致 | — |
| リポジトリ間エッジ | 正解の集合との完全一致 | — | — | 一致 |

率の分母が 0 のときは不合格です。import のゲートも、確定したリポジトリ内の import の記録が 0 なら不合格です。

ケースの判定は正解、代替、誤り、未回答のどれかです。次のどれかに当たる記録があるケースは誤りです。

- distractor（もっともらしい誤答）を、相手、静的な相手、候補のどれかに名指しする。ほかの規則より先に調べる。
- ケースが許さない相手へのエッジ（MethodCalls か CrossRepoCalls）になる。許される相手は正解の相手と静的な相手で、インタフェースのケースでなければ `runtime_targets` も許す。インタフェースのケースで実装を相手にした記録は誤りで、実装は PossiblyCalls に載せる。
- PossiblyCalls の行になる候補（`resolved`・`external`・`ambiguous` の記録の候補。`POSSIBLY_CALLS_MAX_CANDIDATES` を超えて行を書かないサイトのものは除く）に、期待した候補の外のものがある。候補を期待しないケースで行になる候補を出すのも誤り。
- 変換のサイトで相手を名指しするか、`qualname` 付きの `external` を返す。変換のケースは、サイトに記録が無ければ正解。
- リポジトリ内の相手が正解なのに `external` を返す。別のモジュールを返す。

ケースにオフラインの期待（`expect_offline`）があれば、既定のモードはそれで採点します。第三者モジュールの `external` がオフラインで `unresolved` になるのは未回答のままで、オフラインの期待は書きません。

F1 は `iryo-kanri-system`（医療管理システムの Go 版。13 モジュールで、12 個を `go.work` がまとめる）、F2 は `go_layouts`（モジュールとワークスペースの配置）、F3 は `multi_repo`（3 つのリポジトリ）です。F1 の場面と期待値の決め方は [test_codes/iryo-kanri-system/README.md](../test_codes/iryo-kanri-system/README.md) にあります。

正解は gohelper の出力から作りません。`ground_truth/cases.py` が正本で、`generate_manifest.py` が行と列を補った `manifest.json` を作り、`check_ground_truth.py`（標準ライブラリだけで、`graph_generator` を import しない）が整合性を調べます。

### 11.2 実行時オラクル（goracle）

`test_codes/_gt/run_oracle.py` は、標準ライブラリだけで書いた `goracle` でフィクスチャの写しを計装し、`entries.json` の入口（デモとテスト）を動かして、実行時に各サイトから入った関数を観測します。計装の仕組みと知られた限界は [goracle の README](../test_codes/_gt/oracle/README.md) にあります。

- 全関数本体と関数リテラルの先頭に、呼び出し元を記録するフックを入れる。
- サイトごとに `/*line*/` 指示子で ID を付ける。同じ行に複数の呼び出しがあっても取り違えない。
- 呼び出しを含む単純な文の後ろに完了の計数を置く。到達の計数は、その文の前と、`return`、`if`・`for`・`switch` の見出し、`go`、`defer`、`panic` の前に置く。単純な文の前の到達の計数が示すのは、最初に動く呼び出し（ブロックの最後の文と `panic` では一番外の呼び出しも）の実行だけで、ほかの到達の計数はその文のサイトすべての実行を示す。この 2 つで、計装していないコード（標準ライブラリ、依存モジュール）に入った実行を見分ける。
- `defer` が 2 つ以上ある関数では、各 `defer` 文の直後に、その遅延呼び出しの直前に動く実行の計数を置く（文が呼び出しを積めなかったとき、たとえば引数の評価で panic したときは積まれない）。動かなかった遅延呼び出し（`os.Exit`、終了時に止まったままのゴルーチン、積まれなかった呼び出し）は見分け、誤りにしない。
- `range` の暗黙の呼び出しは、コンパイラが呼び出しを置く `for` のキーワードで見分け、その range サイトの観測にする。
- `goracle judge` は正解のケースを、codedoc_python と同じ規則で判定する。全入口の観測を合わせ、観測した関数が正解の相手と `runtime_targets` の和（インタフェースのケースと `ambiguous` のケースでは候補も）に含まれ、`runtime_targets` がすべて観測されることを求める。

| 判定（`goracle judge`） | 規則 | 扱い |
|---|---|---|
| CONFIRMED、CONFIRMED-NOEXEC | 観測した関数が許される関数（上記）に収まり、そのどれかに入った。CONFIRMED-NOEXEC は、完了の計数が無いサイト（`return`、`if` などの見出し、`go`、`defer`）か、完了が入った回数より少ないサイト | 一致 |
| IFACE-CONSISTENT | 相手がインタフェースのメソッドで、観測した関数がすべてその名前のメソッドで、候補か `runtime_targets` に含まれ、`runtime_targets` がすべて観測された。`open_world` が false のサイトで計装していない実装が入れば不一致 | 一致 |
| DEFER-GROUP-CONSISTENT | `defer` が 2 つ以上ある関数の `defer` のサイトで、期待した関数がすべてその集まりの観測に含まれる | 一致 |
| CONSISTENT、CONSISTENT-NOEXEC | `external`（ピアの相手を除く）、`builtin`、変換、`dynamic`、`unresolved` のケースで、リポジトリ内の関数は `runtime_targets` に書いたものだけが入り、`runtime_targets` はすべて入った。CONSISTENT は完了の計数でサイトの実行が分かったもの（`external` では、完了が入った回数より多いことも求める）。CONSISTENT-NOEXEC は到達の計数か入った関数だけで分かったもの | 一致 |
| MISS、IFACE-OTHER、DEFER-GROUP-OTHER、NO-SITE、TABLE-MISMATCH、NOT-RUN、ENTRY-FAILED | 書いていない関数、distractor、計装していない関数（`open_world` が false のサイト）が入った、正解の相手に入らなかった、`runtime_targets` が観測されなかった、サイトや宣言が見つからない、入口が走らせるはずのサイトが実行されなかった、入口が失敗した | 不一致 |
| UNEXEC、SKIPPED、UNVERIFIABLE | UNEXEC は入口が null で実行されなかったケース、SKIPPED はその入口を動かさなかったケース。UNVERIFIABLE は、計装していないファイル、フックの無い相手（アセンブリ、nosplit）、計数の届かないサイト（パッケージ変数の初期化式など）、オラクルにサイトの無い変換、実行されたのに計装した相手が観測されなかったインタフェース・`ambiguous`・`go`・`defer` のサイト、遅延呼び出しが動かなかった `defer` のサイト（実行の計数が 0）、期待した相手が panic か Goexit の中でだけ動いた defer の集まり（[第 12 節](#12-制限)） | 何も言わない |

2026-10-14 に F1 の正解を `goracle judge` で確かめた結果は、157 件のうち 150 件が一致し、不一致は 0 件でした。残る 7 件は UNEXEC 4 件（入口の無いもの: ビルドから外れるファイル 3 件、実装の無いインタフェース 1 件）と UNVERIFIABLE 3 件（実行時に標準ライブラリの `*log.Logger` だけが入るインタフェースの呼び出し、計数の届かないパッケージ変数の初期化式、アセンブリの相手）です。

### 11.3 実リポジトリの試験

`python -m graph_generator trial <dir> --repo-name N`（オプションは [README](../README.md#cli-リファレンス)）は、グラフのトラックを `--no-spanner` で動かします。

- オフラインの回は、空の専用のモジュールキャッシュと `GOPROXY=off` で読みます。`--download-modules` を付けると、別のキャッシュでダウンロードの回も動かします。
- 各回は、グループを 1 つも読み込めない、読み込みの後に `ok` でないグループがある、MethodCalls が 0 本、のどれかで失敗です。
- オフラインで `resolved` のサイトで、ダウンロードの回の記録が確定した別の答え（別の相手、`external`、`builtin`、`dynamic`、`ambiguous`）なら、変わった答えとして試験は失敗です。オフラインの候補がダウンロードの回に無いとき、オフラインで確定した import（ディレクトリとパッケージ句）・埋め込み・実装（型、相手、ポインタ）をダウンロードの回が同じに確定しないときも、変わった答えです（オフライン ⊆ ダウンロード）。
- オフラインで答えた（`resolved` か `external` の）サイトで、ダウンロードの回の記録が `unresolved` か無いものは、調べるために一覧にするだけです（`summary.json` の `resolved_only_offline`）。オフラインの `external` にダウンロードの回が別のモジュール、版、答えを返したものは `external_changes` に報告するだけで、失敗にしません。オフラインの `builtin` と `dynamic` は相手もエッジも無いので比べず、オフラインで失った答えの割合にだけ入ります。
- 試験の前後で対象のリポジトリの木が変われば失敗です。ファイル（モード、大きさ、mtime、ctime）、ディレクトリ（モード、mtime、ctime）、シンボリックリンク（リンク先、ctime）を、`.git` と、`.git` ファイルが指す gitdir を含めて比べます。
- 続けて goracle でリポジトリの写しを計装し、選んだパッケージの単体テストを `GO_BUILD_TAGS` 付きで動かして、ダウンロードの回（無ければオフラインの回）の MethodCalls の各エッジを `goracle match` で採点します。採点の入力は評価と同じ規則で作ります。インタフェースのエッジに記録の候補（空でも）を渡すのは、`open_world` が false で `candidates_partial`（[第 10 節](#10-resolutionsjson)）の無い記録だけで、観測したリポジトリ内の関数はすべて候補に入っていなければなりません。open world のエッジと `candidates_partial` がある記録は候補を渡さず、その名前のメソッドなら一致とします（理由と限界は[第 12 節](#12-制限)）。既定のパッケージがあるのは etcd と Prometheus で、ほかのリポジトリは `--packages` か `--skip-oracle` が要ります。
- 失敗したテストのパッケージは 1 回だけやり直します。やり直しで通れば `flaky` として通し、2 回とも失敗か時間切れなら試験は失敗です。
- テストが通ったパッケージ（`flaky` を含む）は、自分のディレクトリに、goracle が実行を観測したサイトを 1 つ以上持たなければなりません。実行の観測とは、完了の計数が 1 以上か、入った関数が見えたことです。判定は使わず、そのサイトに採点するエッジがあるかどうかも問いません（`resolved` の記録が無いパッケージでも、サイトが実行されていれば通ります）。到達の計数だけでは数えません。1 つも無ければ、オラクルが実行を記録できていないので試験は失敗です。そういうサイトの数は `summary.json` の `oracle.packages.<パッケージ>.executed` にあります。
- 採点する回の全記録について、呼び出し元と名前を goracle のサイトの表と照らします（[第 11.1 節](#111-evaluate)のオラクルと同じ照合）。
- 誤りの判定（下の表）、やり直しても失敗か時間切れのテストのパッケージ、goracle が自分のサイトで実行を観測しなかったパッケージ、オラクル自体の失敗、採点したエッジが 0 本、判定の数とエッジの数の食い違い、理由付きで飛ばすファイルの外での NO-SITE と NO-TARGET、表との照合に通らない記録のどれかがあれば、試験は失敗です。
- PossiblyCalls については、観測したインタフェースの相手のうち候補に入っていた割合を報告します。
- `goracle report` の WARNING の行（表が予測しない実行時の名前で動いた関数など）は、`summary.json` の `oracle.report_warnings` と `summary.md` に載せるだけで、失敗にしません。

| 判定（`goracle match`） | 規則 | 扱い |
|---|---|---|
| CONFIRMED | 入った関数が正解の相手だけで、完了の計数と入った回数が一致する | 正解 |
| CONFIRMED-NOEXEC | 入った関数が正解の相手だけ。サイトに完了の計数が無いか、完了が入った回数より少ない（入った後の panic や終了） | 正解 |
| IFACE-CONSISTENT | 相手がインタフェースのメソッドで、計装した関数で観測したものがすべてその名前のメソッドで、候補を渡したエッジでは候補に入っている。ジェネリック型のメソッド（候補にならない。観測した数を `generic_observations` に報告する）と、`via_embedded_iface` の候補の先（二段目のディスパッチ）は名前だけで決める | 正解 |
| DEFER-GROUP-CONSISTENT | `defer` が 2 つ以上ある関数の `defer` のサイトで、相手がその集まりの観測に含まれる | 集まりの単位で正解 |
| WRONG-OTHER、WRONG-MULTI、IFACE-OTHER、DEFER-GROUP-OTHER | WRONG-OTHER は相手に入らず、別の関数か計装していないコードだけが入った。WRONG-MULTI は相手に加えて別の関数も入った、または完了の計数が入った回数より多い。IFACE-OTHER はその名前のメソッドでない関数か、渡した候補の外の実装が入った。DEFER-GROUP-OTHER は相手が集まりの観測に無い | 誤り |
| NO-SITE、NO-TARGET | オラクルの表にサイトか相手の宣言が無い。goracle が理由付きで飛ばすファイル（`//line` 指示子、cgo、構文エラー、モジュールの外）なら記録するだけで、ほかは失敗 | 採点しない |
| UNEXERCISED、UNVERIFIABLE-TARGET | サイトが実行されなかった（遅延呼び出しが一度も動かなかった `defer` と、defer の集まりの相手が panic か Goexit の中でだけ動いたもの（[第 12 節](#12-制限)）を含む）、相手にフックが無い（アセンブリ、nosplit） | 何も言わない |

[第 11.4 節](#114-検証の数値)の「確認した辺」は正解の 4 つの判定の合計、「誤った辺」は WRONG で始まる判定と末尾が OTHER の判定の合計です。「誤った辺 0」の主張はこの合計を数えます。判定ごとの数は `summary.md` にあります。

### 11.4 検証の数値

次の 2 つの表は `python -m scripts.verification_table --output-dir <試験の OUTPUT_DIR>` の出力です。「未測定」の行は、最終検証でこのコマンドの出力に差し替え、測った日付とツールチェーンの版を添えます。

- 評価の表の「インポート」は、確定したリポジトリ内の import の記録のうち、PackageImports の行になったものの数と全体の数です。`go_layouts` はゲートでないので「参考」が付きます。「曖昧 N」は、ディレクトリの複数のパッケージ句に結び付きうるので除いた記録の数です。
- 評価の表の「オラクル」は、`goracle match` にかけた `resolved` のメソッドの記録の数と、誤りの判定（WRONG-OTHER、WRONG-MULTI、IFACE-OTHER、DEFER-GROUP-OTHER）の数で、形は「記録の数 / 誤りの数」です。サイトの表との照合だけで採点しない `go_layouts` は「—」です。
- ノードカバレッジ、エッジ率、QA、インポート、オラクルがゲートを満たさないときは「未達」が付きます。オラクルの未達の原因は、誤りの判定、NO-SITE、NO-TARGET、表との照合の問題、失敗した入口、オラクル自体の失敗です。完全性には必要な数が付きます。当てはまらない欄は「—」です。
- 試験の表の「インポート」は、回ごとの同じ割合です。

| フィクスチャ | モード | ケース | 正解 | 代替 | 誤り | 未回答 | 完全性 | ノードカバレッジ | エッジ率 | QA | リポジトリ間 | インポート | オラクル | 結果 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `iryo-kanri-system` | 既定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |
| `iryo-kanri-system` | `--env` | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |
| `go_layouts` | 既定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |
| `go_layouts` | `--env` | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |
| `multi_repo` | 既定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |
| `multi_repo` | `--env` | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |

| リポジトリ | モード | ファイル | パッケージ | サイト | resolved | external | unresolved | MethodCalls | PossiblyCalls | インポート | 秒 | 最大メモリ | 変化した答え | 確認した辺 | 誤った辺 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `etcd` | オフライン | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |
| `etcd` | ダウンロード | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |
| `prometheus` | オフライン | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |
| `prometheus` | ダウンロード | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 |

## 12. 制限

- **インタフェースの相手は近似。** points-to 解析（VTA、RTA）はしないので、候補は「満たす型」の集まりで、実行時に入る型の集まりではありません。`open_world` が true なら、実際の相手が候補のどれでもないことがあります。別のグループだけがビルドに入れる型（F1 の管理コンソールの型など）は、サイトのグループの候補にならず、`open_world` を true にするだけです（[第 6 節](#6-インタフェースの呼び出しと-open-world)）。
- **open world のインタフェース呼び出しでは、オラクルはメソッド名しか確かめない。** リポジトリの外の実装は、リポジトリ内のどのメソッドにも転送できます（`io.NopCloser(r)` の `Read` は `r.Read` に入る）。候補の外の関数が入っても、エッジが誤りとは言えません。そのため評価と試験は、open world のエッジでは観測した関数がその名前のメソッドかだけを調べます。同じ名前のメソッドを持つ 2 つのインタフェースの取り違えは、そこでは見つかりません。正解を書いたケース（[第 11.1 節](#111-evaluate)）は、この取り違えも採点します。
- **panic か Goexit の中で動いた引数の無い `defer` は、defer の集まりの誤りを見逃しうる。** panic 中か `runtime.Goexit`（`t.Fatal` など）の中で動いた遅延呼び出しは、コンパイラの deferwrap が見えればそれを積んだ関数の分として数えます。引数の無い関数値の `defer c()` は deferwrap を持たないので、goracle は積んだ関数を見分けられず、スタック上の `defer` を持つ関数すべての分として数えます。その関数たちの defer の集まりでは、`c` への誤ったエッジが DEFER-GROUP-OTHER にならず、UNEXERCISED（`goracle judge` では UNVERIFIABLE）になります。誤りを隠すことはあっても、誤りを作ることはありません（[goracle の README](../test_codes/_gt/oracle/README.md#知られた限界)）。
- **cgo は既定で解決しない。** `CGO_ENABLED=0` で読み込みます。`GO_BUILD_CGO=1` にすると go コマンドが gcc と pkg-config を実行します（[README](../README.md#設定リファレンス)）。cgo の呼び出しはモジュール `C` の `external` です。
- **GOPATH モードは扱わない。** `GO111MODULE=off` のコードと、どの go.mod の下にも無いファイルは抽出だけして解決しません（`no-module`）。
- **Bazel と `GOPACKAGESDRIVER` は扱わない。**
- **フレームワークの規約は解釈しない。** DI コンテナ（wire、fx）、リフレクションによる振り分け、`go:generate` は、`dynamic` か `external` になります。
- **range-over-func の本体はノードを持たない。** 反復子の中の `yield(…)` は引数の関数値で `dynamic` です。実行時に入る合成関数には宣言がありません。
- **非公開モジュール。** ダウンロードのモードでも取得しません（[README](../README.md#オフラインとダウンロード)）。
- **メモリ。** `NeedDeps` でグループの全パッケージを型付きで読むので、大きなリポジトリではメモリを使います（[README](../README.md#トラブルシューティング)）。
