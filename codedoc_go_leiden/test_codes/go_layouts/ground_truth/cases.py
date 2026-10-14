# -*- coding: utf-8 -*-
"""Ground truth for go_layouts (F2, source of record).

Each top-level directory is one layout, analyzed as its own repo (repo name =
directory name, master plan C10). `_outside/` is never a layout: it holds the
target of an out-of-repo replace. File paths are fixture-relative; lines, byte
columns and offsets are derived into manifest.json by
test_codes/_gt/generate_manifest.py. Expectations follow the go command
(go1.27.2, GOOS=linux GOARCH=amd64 CGO_ENABLED=0, GOTOOLCHAIN=local) and the
group rule of spec §3.2 with amendment R4: a module is loaded in workspace
mode when the go.work found by walking up from its root lists it, otherwise
alone with GOWORK=off. `expect` is the answer of evaluate's env mode (download
mode against test_codes/_proxy). The discovery facts per layout are in
ground_truth/expected_units.json.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_gt"))
from gt_lib import ambiguous, case, external, plain, resolved, target  # noqa: E402

M = "example.com/layouts/"

LAYOUTS = ["badvendor", "buildignore", "cgo", "depnewgo", "dotimport", "ignoredir", "linedir", "modvendor",
           "nested", "newtoolchain", "nomodule", "nonancestor", "nosum", "replace-in", "replace-out", "skipdirs",
           "symlinks", "toonew", "twowork", "ws", "ws-missing", "ws-toonew", "wsvendor"]
REPOS = [{"name": name, "dir": name} for name in LAYOUTS]


def fn(qualname, module, file):
    return target(qualname, M + module, "method", file)


CASES: list[dict] = []
DECL_PINS: list[dict] = []

# ── nested: a module with a nested module, joined by replace => ./inner ─────
CALC_TWICE = fn("Twice", "nested/inner", "nested/inner/calc/calc.go")
REPORT_TWICE = fn("Twice", "nested", "nested/report/report.go")
CASES += [
    case("NEST-001", "nested/main.go", "calc.Twice(21)", "Twice", "call", resolved(CALC_TWICE),
         "外側のモジュールは require と replace => ./inner で入れ子のモジュールを使う。単一モジュールの"
         "グループでも replace の相対パスはモジュールのルートから解決される。外側の report.Twice は同名の別関数",
         "nested", distractors=[REPORT_TWICE]),
    case("NEST-002", "nested/report/report.go", "strconv.Itoa(Twice(n))", "Twice", "call", resolved(REPORT_TWICE),
         "外側のモジュールの中の呼び出し。入れ子のモジュールの calc.Twice ではない", "nested", distractors=[CALC_TWICE]),
    case("NEST-003", "nested/inner/calc/calc.go", "return double(n)", "double", "call",
         resolved(fn("double", "nested/inner", "nested/inner/calc/calc.go")),
         "nested/inner/ のファイルは一番深い go.mod (inner) に属する。外側のグループでは依存パッケージ"
         "なので、この site は inner のグループの答えだけが使われる", "nested"),
]

# ── ws: go.work without vendor/; MVS across the use modules; a module it does not list ─
CASES += [
    case("WS-001", "ws/app/main.go", "c := tally.New()", "New", "call",
         resolved(fn("New", "ws/lib", "ws/lib/tally/tally.go")),
         "go.work の use 同士。app の go.mod に lib の require はないが、ワークスペースではリポジトリ内の"
         "ソースが使われる (GOWORK=off ではビルドできない)", "ws-app"),
    case("WS-002", "ws/app/main.go", "c.Add(2)", "Add", "call",
         resolved(fn("Counter.Add", "ws/lib", "ws/lib/tally/tally.go")),
         "別のワークスペースモジュールの型のポインタレシーバのメソッド", "ws-app"),
    case("WS-003", "ws/lib/tally/tally.go", "c.n = Sum(c.n, k)", "Sum", "call",
         resolved(fn("Sum", "ws/lib", "ws/lib/tally/tally.go")),
         "ワークスペースのもう一つのモジュールの中の呼び出し", "ws-app"),
    case("WS-004", "ws/app/headline.go", "rows := t.Len()", "Len", "call",
         external("example.com/thirdparty/textfmt"),
         "app は textfmt v1.0.0、lib は v1.1.0 を require する。ワークスペースの MVS は v1.1.0 を選ぶので、"
         "v1.1.0 にだけある Table.Len がコンパイルされる。オフラインでは unresolved (missing-module) で未回答",
         "ws-app"),
    case("WS-005", "ws/app/headline.go", "t := textfmt.NewTable()", "NewTable", "call",
         external("example.com/thirdparty/textfmt"), "第三者モジュールのパッケージ関数。オフラインでは未回答",
         "ws-app"),
    case("WS-006", "ws/extra/main.go", "tally.Sum(40, 2)", "Sum", "call", plain("unresolved"),
         "ws/extra から上へたどって見つかる ws/go.work は extra を use しない。単一モジュール (GOWORK=off) の"
         "グループで、lib を require しないので go コマンドは import を拒む。ワークスペースに入れると誤った辺になる",
         None, distractors=[fn("Sum", "ws/lib", "ws/lib/tally/tally.go")]),
]

# ── wsvendor: go.work with a workspace vendor/ (go work vendor) ─────────────
CASES += [
    case("WSV-001", "wsvendor/svc/main.go", "id := uuidlite.New()", "New", "call",
         external("example.com/thirdparty/uuidlite"),
         "ワークスペースの vendor/ (modules.txt の 1 行目が ## workspace) から読む。モジュールキャッシュが空でも"
         "オフラインで答えが出る (§5.3 行 5)", "wsvendor"),
    case("WSV-002", "wsvendor/svc/main.go", "id.String()", "String", "call",
         external("example.com/thirdparty/uuidlite"), "vendor にある型のメソッド", "wsvendor"),
    case("WSV-003", "wsvendor/svc/main.go", "pad.Left(id.String(), 40)", "Left", "call",
         resolved(fn("Left", "wsvendor/util", "wsvendor/util/pad/pad.go")),
         "vendor モードでもワークスペースのモジュール同士はリポジトリ内のソース", "wsvendor"),
    case("WSV-004", "wsvendor/util/pad/pad.go", "gap(len(s), width)", "gap", "call",
         resolved(fn("gap", "wsvendor/util", "wsvendor/util/pad/pad.go")), "util の中の呼び出し", "wsvendor"),
]

# ── modvendor: module vendor/ (go mod vendor) ───────────────────────────────
CASES += [
    case("VND-001", "modvendor/main.go", "doc := yamlish.NewDocument()", "NewDocument", "call",
         external("example.com/thirdparty/yamlish"),
         "単一モジュールの vendor/。-mod=vendor で読むのでオフラインでも external", "modvendor"),
    case("VND-002", "modvendor/main.go", "doc.Keys()", "Keys", "call", external("example.com/thirdparty/yamlish"),
         "v1.1.0 にだけあるメソッド。vendor のコピーは v1.1.0", "modvendor"),
    case("VND-003", "modvendor/main.go", "fmt.Print(render(doc))", "render", "call",
         resolved(fn("render", "modvendor", "modvendor/main.go")), "モジュール自身の関数", "modvendor"),
]

# ── replace-in / replace-out: local replaces inside and outside the repo ───
HELLO = fn("Hello", "common", "replace-in/common/greet/greet.go")
CASES += [
    case("REP-001", "replace-in/app/main.go", 'greet.Hello("in")', "Hello", "call", resolved(HELLO),
         "replace => ../common はリポジトリ (replace-in) 内の隣のモジュールを指す。呼び出し先はスキャンした"
         "ファイルなのでリポジトリ内の辺になる", "replace-in"),
    case("REP-002", "replace-in/common/greet/greet.go", "polish(name)", "polish", "call",
         resolved(fn("polish", "common", "replace-in/common/greet/greet.go")),
         "replace 先のモジュール common は自分の単一モジュールのグループでも解決される", "replace-in"),
    case("REP-003", "replace-out/main.go", "extlib.NewLabel(120)", "NewLabel", "call",
         plain("unresolved", "outside-repo"),
         "replace => ../_outside/extlib はリポジトリの外の実在するディレクトリ。go コマンドはそこをビルドするが、"
         "呼び出し先はこのリポジトリのノードでもピアでもない (§5.3 行 9)", None),
    case("REP-004", "replace-out/main.go", "l.Text()", "Text", "call", plain("unresolved", "outside-repo"),
         "リポジトリの外の型のメソッドも同じ", None),
    case("REP-005", "replace-out/main.go", "describe(extlib.NewLabel", "describe", "call",
         resolved(fn("describe", "replaceout", "replace-out/main.go")),
         "リポジトリの外を replace していても、モジュール自身の中の呼び出しは解決される", "replace-out"),
]

# ── ws-missing: a go.work use of a missing directory (degraded) ────────────
CASES += [
    case("DEG-001", "ws-missing/present/main.go", 'banner("present")', "banner", "call",
         resolved(fn("banner", "wsmissing/present", "ws-missing/present/main.go")),
         "go.work の use ./absent は存在しない。go コマンドは go.work ごと失敗するが、生成する go.work は"
         "その行を落として degraded にし、残りのモジュールは解決する", "ws-missing"),
    case("DEG-002", "ws-missing/present/main.go", 'strings.Repeat("=", 3)', "Repeat", "call",
         external("std"), "degraded なグループでも標準ライブラリは external", "ws-missing"),
]

# ── toolchain: newer go lines fail closed; a newer toolchain line alone does not ─
CASES += [
    case("TCH-001", "toonew/main.go", "fmt.Println(level())", "level", "call", plain("unresolved", "toolchain"),
         "go.mod の go 1.28 は go1.27.2 より新しい。go コマンドは go.mod requires go >= 1.28 で止まる。"
         "グループの全 site が unresolved (toolchain)", None,
         distractors=[fn("level", "toonew", "toonew/main.go")]),
    case("TCH-002", "toonew/main.go", "fmt.Println(level())", "Println", "call", plain("unresolved", "toolchain"),
         "標準ライブラリの呼び出しも同じグループなので答えない", None),
    case("TCH-003", "ws-toonew/member/main.go", "fmt.Println(member())", "member", "call",
         plain("unresolved", "toolchain"),
         "go.work の go 1.28 (モジュール自身は go 1.26)。go コマンドは ../go.work requires go >= 1.28 で止まる",
         None, distractors=[fn("member", "wstoonew/member", "ws-toonew/member/main.go")]),
    case("TCH-004", "newtoolchain/main.go", "fmt.Println(pinned())", "pinned", "call",
         resolved(fn("pinned", "newtoolchain", "newtoolchain/main.go")),
         "go 1.26 と toolchain go1.28.1。GOTOOLCHAIN=local では go 行が満たされていればローカルの"
         "ツールチェーンでビルドされる。状態は ok で警告だけ (仕様 §3.3、改訂 R23)", "newtoolchain"),
    case("TCH-005", "newtoolchain/main.go", "fmt.Println(pinned())", "Println", "call", external("std"),
         "toolchain 行が新しくても標準ライブラリは external。警告は標準ライブラリの行き先が違いうることを言う",
         "newtoolchain"),
]

# ── depnewgo: a workspace whose dependency says go 1.28 (amendment R5) ─────
CASES += [
    case("DNG-001", "depnewgo/m/main.go", "fmt.Println(newgo.F(), local())", "F", "call",
         plain("unresolved", "toolchain"),
         "依存 example.com/thirdparty/newgo v1.0.0 の go.mod が go 1.28。env モードでは go コマンドが "
         "module example.com/thirdparty/newgo@v1.0.0 requires go >= 1.28 でロード全体を止める (toolchain)。"
         "オフラインでは newgo が無いので missing-module", None, offline=plain("unresolved", "missing-module")),
    case("DNG-002", "depnewgo/m/main.go", "local())", "local", "call", plain("unresolved", "toolchain"),
         "ロードが失敗したグループでは、依存と無関係なリポジトリ内の呼び出しも unresolved (toolchain)。"
         "グループの状態は error (改訂 R5)。オフラインでは依存の go.mod が見えないのでロードは成功し、"
         "local に解決する", None, offline=resolved(fn("local", "depnewgo/m", "depnewgo/m/main.go"))),
]

# ── nosum: missing go.sum lines (amendment R12) ─────────────────────────────
CASES += [
    case("SUM-001", "nosum/main.go", "uuidlite.FromInt(7)", "FromInt", "call",
         external("example.com/thirdparty/uuidlite"),
         "go.sum に uuidlite の行はある。env モードでは external、オフラインでは unresolved (missing-module)",
         "nosum", offline=plain("unresolved", "missing-module")),
    case("SUM-002", "nosum/caps/caps.go", "textfmt.Title(s)", "Title", "call",
         external("example.com/thirdparty/textfmt"),
         "go.sum に textfmt の行がない。-mod=readonly の go コマンドは missing go.sum entry で import を拒むが、"
         "ダウンロードモードは制御ディレクトリの go.mod のコピーに -mod=mod でダウンロードして go.sum のコピーを"
         "埋める (改訂 R12)。go.mod のコピーは元に戻す (改訂 R45) ので、readonly の読み込みで external になる。"
         "オフラインでは unresolved (missing-module)", None,
         offline=plain("unresolved", "missing-module")),
    case("SUM-003", "nosum/main.go", "tag())", "tag", "call", resolved(fn("tag", "nosum", "nosum/main.go")),
         "同じモジュールの中の呼び出しは go.sum と無関係に解決する", "nosum"),
]

# ── nomodule: no go.mod at all ──────────────────────────────────────────────
CASES += [
    case("NOM-001", "nomodule/loose.go", 'fmt.Println(greet("loose"))', "greet", "call",
         plain("unresolved", "no-module"),
         "どの go.mod にも属さないファイル。抽出はするが解決しない (GOPATH モードは対象外)", None),
    case("NOM-002", "nomodule/loose.go", 'fmt.Println(greet("loose"))', "Println", "call",
         plain("unresolved", "no-module"), "モジュールがないので標準ライブラリの呼び出しも答えない", None),
]

# ── skipdirs: testdata/, _x/ and .x/ are skipped but importable explicitly ──
CASES += [
    case("SKP-001", "skipdirs/main.go", "draft.Sketch()", "Sketch", "call", plain("unresolved", "unscanned"),
         "_draft/ は ./... からもスキャンからも外れるが、明示的な import ならコンパイルされる。"
         "呼び出し先はスキャンしていないリポジトリ内のファイル (§5.3 行 7)", None),
    case("SKP-002", "skipdirs/main.go", "golden.Load()", "Load", "call", plain("unresolved", "unscanned"),
         "testdata/ の下のパッケージも同じ", None),
    case("SKP-003", "skipdirs/main.go", "cache.Get()", "Get", "call", plain("unresolved", "unscanned"),
         ".cache/ の下のパッケージも同じ", None),
    case("SKP-004", "skipdirs/main.go", "local())", "local", "call",
         resolved(fn("local", "skipdirs", "skipdirs/main.go")),
         "_old.go と .dot.go は go コマンドが黙って読み飛ばすファイルで、同名の local を持つ。スキャンもしないので"
         "ノードにならない", "skipdirs"),
]

# ── ignoredir: go.mod ignore (amendment R21) ────────────────────────────────
CASES += [
    case("IGD-001", "ignoredir/main.go", "generated.Banner()", "Banner", "call",
         plain("unresolved", "unscanned"),
         "go.mod の ignore ./generated は ./... から外すだけで、明示的な import ならコンパイルされる。"
         "ignore したディレクトリはスキャンしないので、呼び出し先は unscanned。実行時に走るのは generated.Banner だが、"
         "オラクルも ignore したディレクトリを計装しない (改訂 R21) ので runtime には書かない",
         "ignoredir"),
    case("IGD-002", "ignoredir/main.go", "local())", "local", "call",
         resolved(fn("local", "ignoredir", "ignoredir/main.go")), "ignore と無関係な呼び出しは解決する",
         "ignoredir"),
]

# ── buildignore: //go:build ignore, a second package in one directory ───────
TOOL_HELPER = fn("Helper", "buildignore", "buildignore/tool.go")
GEN_HELPER = fn("Helper", "buildignore", "buildignore/gen.go")
CASES += [
    case("IGN-001", "buildignore/tool.go", '"run:" + Helper()', "Helper", "call", resolved(TOOL_HELPER),
         "ディレクトリのパッケージは tool (パス要素 buildignore と違う名前)。gen.go の Helper は"
         "//go:build ignore の package main にある", "buildignore", distractors=[GEN_HELPER]),
    case("IGN-002", "buildignore/gen.go", "fmt.Println(Helper())", "Helper", "call",
         plain("unresolved", "excluded-by-build"),
         "どの構成もコンパイルしないファイル。ノードは残るが site は unresolved。名前で tool.go の Helper に"
         "結び付けない", None, distractors=[TOOL_HELPER, GEN_HELPER]),
    case("IGN-003", "buildignore/cmd/run/main.go", "tool.Run()", "Run", "call",
         resolved(fn("Run", "buildignore", "buildignore/tool.go")),
         "import パスの最後の要素 (buildignore) とパッケージ名 (tool) が違うパッケージへの呼び出し",
         "buildignore"),
]

# ── symlinks: a symlinked file compiled in two packages ─────────────────────
DESCRIBE = fn("Describe", "symlinks", "symlinks/a/real.go")
A_HELPER = fn("helper", "symlinks", "symlinks/a/helper.go")
B_HELPER = fn("helper", "symlinks", "symlinks/b/helper.go")
CASES += [
    case("SYM-001", "symlinks/a/real.go", 'return helper() + "!"', "helper", "call",
         ambiguous(A_HELPER, B_HELPER, runtime=[A_HELPER, B_HELPER]),
         "b/real.go は a/real.go へのシンボリックリンクで、パッケージ a と b の両方でコンパイルされる。"
         "実パスが同じ site で二つのパッケージの答えが食い違う (G1 variant)。オラクルはリンクを別ファイルとして"
         "計装し、実パスの site とリンクの写しの site の観測を合わせて判定する。a.helper と b.helper の両方が走る",
         "symlinks"),
    case("SYM-002", "symlinks/main.go", "ka.Describe()", "Describe", "call", resolved(DESCRIBE),
         "実ファイル a/real.go の宣言", "symlinks"),
    case("SYM-003", "symlinks/main.go", "kb.Describe()", "Describe", "call", resolved(DESCRIBE),
         "パッケージ b の Describe の宣言位置はリンク b/real.go。実パスにすると a/real.go のノード", "symlinks"),
]

# ── cgo under CGO_ENABLED=0 ─────────────────────────────────────────────────
NOCGO_VERSION = fn("Version", "cgo", "cgo/native/native_nocgo.go")
CASES += [
    case("CGO-001", "cgo/native/describe.go", '"native " + Version()', "Version", "call", resolved(NOCGO_VERSION),
         "CGO_ENABLED=0 では import \"C\" のファイルは外れ、//go:build !cgo の双子がコンパイルされる (G10)。"
         "native_cgo.go の Version は build_variants に載るだけで辺にしない", "cgo"),
    case("CGO-002", "cgo/native/native_cgo.go", 'C.GoString(C.CString("cgo"))', "GoString", "call",
         plain("unresolved", "excluded-by-build"), "cgo のファイルは主構成に含まれない", None),
    case("CGO-003", "cgo/native/native_cgo.go", "strings.ToUpper(C.GoString", "ToUpper", "call",
         plain("unresolved", "excluded-by-build"), "外れたファイルの中では標準ライブラリの呼び出しも答えない", None),
    case("CGO-004", "cgo/main.go", "native.Describe()", "Describe", "call",
         resolved(fn("Describe", "cgo", "cgo/native/describe.go")), "cgo とは関係ないファイルの宣言", "cgo"),
]

# ── linedir: //line directives (amendment R3: positions are never adjusted) ─
MARK = fn("mark", "linedir", "linedir/view.go")
CASES += [
    case("LIN-001", "linedir/view.go", "strings.ToUpper(name) + suffix()", "suffix", "call",
         resolved(fn("suffix", "linedir", "linedir/view.go")),
         "//line view.tmpl:1 の後ろの site と //line view.tmpl:30 の後ろの宣言。位置は物理的な行と"
         "バイト列 (PositionFor(p, false))。オラクルは //line のあるファイルを計装しない", None),
    case("LIN-002", "linedir/view.go", "strings.ToUpper(name)", "ToUpper", "call", external("std"),
         "//line の後ろでも標準ライブラリの呼び出し", None),
    case("LIN-003", "linedir/view.go", 'mark("!")', "mark", "call", resolved(MARK),
         "インラインの /*line view.tmpl:40:9*/ の直後の呼び出し", None),
    case("LIN-004", "linedir/cmd/show/main.go", 'linedir.Render("go")', "Render", "call",
         resolved(fn("Render", "linedir", "linedir/view.go")),
         "呼び出し先の宣言が //line の後ろにある。調整後の位置 view.tmpl:1 はリポジトリのファイルではない", None),
    case("LIN-005", "linedir/lined.go", "return Lined()", "Lined", "call",
         resolved(fn("Lined", "linedir", "linedir/lined.go")),
         "lined.go の //line view.go:15:1 は別のリポジトリ内のファイルを指す。Lined の調整後の位置 view.go:15:6 は "
         "func mark の名前の位置と同じなので、調整後の位置で引くと誤った辺 CallLined -> mark になる (改訂 R3)", None,
         distractors=[MARK]),
    case("LIN-006", "linedir/cmd/show/main.go", "linedir.CallLined()", "CallLined", "call",
         resolved(fn("CallLined", "linedir", "linedir/lined.go")), "//line の後ろにある宣言への呼び出し", None),
]

# ── dotimport ───────────────────────────────────────────────────────────────
STR_REVERSE = fn("Reverse", "dotimport", "dotimport/strutil/strutil.go")
OTHER_REVERSE = fn("Reverse", "dotimport", "dotimport/other/other.go")
CASES += [
    case("DOT-001", "dotimport/main.go", 'Reverse(ToUpper("dot"))', "Reverse", "call", resolved(STR_REVERSE),
         "ドット import した strutil の関数を修飾なしで呼ぶ。同名の other.Reverse は別パッケージ",
         "dotimport", distractors=[OTHER_REVERSE]),
    case("DOT-002", "dotimport/main.go", 'ToUpper("dot")', "ToUpper", "call", external("std"),
         "標準ライブラリ strings のドット import", "dotimport"),
    case("DOT-003", "dotimport/main.go", 'other.Reverse("plain")', "Reverse", "call", resolved(OTHER_REVERSE),
         "修飾した呼び出しは other の方", "dotimport", distractors=[STR_REVERSE]),
    case("DOT-004", "dotimport/strutil/strutil.go", "swap(r, i, j)", "swap", "call",
         resolved(fn("swap", "dotimport", "dotimport/strutil/strutil.go")), "ドット import される側の中の呼び出し",
         "dotimport"),
]

# ── nonancestor: a go.work that lists a module but is not its ancestor (amendment R4) ─
CASES += [
    case("NAN-001", "nonancestor/server/main.go", "api.Version()", "Version", "call",
         external("example.com/layouts/nonancestor/api"),
         "tools/go.work は ../api と ../server を use するが、server から上へたどっても見つからない。"
         "server は単一モジュールのグループで、require した公開版 api v1.0.0 (_proxy) がビルドされる。"
         "tools のワークスペースで読むとリポジトリ内の api になり誤答。オフラインでは公開版が無いので "
         "unresolved (missing-module)", "nonancestor-server",
         distractors=[fn("Version", "nonancestor/api", "nonancestor/api/api.go")], offline=plain("unresolved", "missing-module")),
    case("NAN-002", "nonancestor/api/api.go", 'label("in-repo")', "label", "call",
         resolved(fn("label", "nonancestor/api", "nonancestor/api/api.go")),
         "リポジトリ内の api は自分の単一モジュールのグループで解決される。server からは呼ばれない", None),
]

# ── twowork: a module listed by two go.work files (amendment R4) ────────────
CASES += [
    case("TWO-001", "twowork/app/main.go", "calc.Triple(14)", "Triple", "call",
         resolved(fn("Triple", "twowork/core", "twowork/core/calc/calc.go")),
         "core はルートの go.work と scratch/go.work の両方に並ぶ。core のグループは上へたどって見つかる"
         "ルートの go.work の一つだけ (scratch/go.work は警告)", "twowork"),
    case("TWO-002", "twowork/core/calc/calc.go", "add(n, add(n, n))", "add", "call",
         resolved(fn("add", "twowork/core", "twowork/core/calc/calc.go")),
         "グループが一つなので site の答えも一つ", "twowork"),
]

# ── badvendor: vendor/ out of sync with go.mod (amendment R35) ──────────────
CASES += [
    case("BVD-001", "badvendor/main.go", "yamlish.Marshal(", "Marshal", "call", plain("unresolved", "load-error"),
         "vendor/ は yamlish v1.0.0 で作ったまま、go.mod は v1.1.0 に上げてある。go コマンドは inconsistent "
         "vendoring で止まる。グループの状態は error で、vendor を無視した readonly のビルドはしない", None),
    case("BVD-002", "badvendor/main.go", "local())", "local", "call", plain("unresolved", "load-error"),
         "失敗したグループではモジュール自身の呼び出しも unresolved", None,
         distractors=[fn("local", "badvendor", "badvendor/main.go")]),
]

DECL_PINS += [
    {"qualname": "Helper", "module": M + "buildignore", "kind": "method", "file": "buildignore/gen.go"},
    {"qualname": "Version", "module": M + "cgo", "kind": "method", "file": "cgo/native/native_cgo.go"},
    {"qualname": "Render", "module": M + "linedir", "kind": "method", "file": "linedir/view.go"},
    {"qualname": "Lined", "module": M + "linedir", "kind": "method", "file": "linedir/lined.go"},
    {"qualname": "Describe", "module": M + "symlinks", "kind": "method", "file": "symlinks/a/real.go"},
    {"qualname": "Twice", "module": M + "nested/inner", "kind": "method", "file": "nested/inner/calc/calc.go"},
    {"qualname": "Counter", "module": M + "ws/lib", "kind": "class", "file": "ws/lib/tally/tally.go"},
    {"qualname": "level", "module": M + "toonew", "kind": "method", "file": "toonew/main.go"},
]

LAYOUT_EXCEPTIONS = [
    "ws-missing/go.work: use ./absent has no go.mod",
    "ws/extra/main.go: imports example.com/layouts/ws/lib/tally, which no module reachable",
]
