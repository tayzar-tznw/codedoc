#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expected_resolutions.json を cases.py から再生成する。

cases.py の各ケースは「ファイル + 一意スニペット」で呼び出し箇所を指すため、
テストコードを編集して行番号がずれても、本スクリプトを再実行するだけで
ground truth を最新の行番号・宣言アンカー付きで作り直せる。

決定的(同じツリーからは同じ JSON)。標準ライブラリのみ使用。
実行後は必ず check_ground_truth.py(独立検証)を通すこと。
"""

import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "expected_resolutions.json")

SYMBOL_GRAMMAR = (
    "型: Namespace.Type(入れ子型は '+'、ジェネリック定義は宣言どおり ChartCache<T> / "
    "ChartCache<TKey,TValue>)。メソッド: Type.Name(仮引数型リスト)(C# キーワード型名、"
    "引数名なし、params は保持)。コンストラクタ: Type..ctor(...)(裸の型シンボルは型そのもの: "
    "暗黙コンストラクタの new・nameof・DI 型引数)。プロパティ: Type.Name { get }、"
    "インデクサ: Type.this[TKey] { get }。演算子: Type.operator +(A, B)、変換: "
    "Type.implicit operator T(S)。拡張メソッドは宣言側静的クラスに、レシーバを第 1 引数として"
    "表記。明示的インタフェース実装: Type.Interface.Method(...)。ローカル関数: "
    "Type.Method(...)/Local(引数型)。既定インタフェース実装(DIM)はインタフェース側に表記。"
    "特殊値: RUNTIME(dynamic)、AMBIGUOUS(n)(リフレクション候補集合)、"
    "EXTERNAL(<assembly>) <display>(リポジトリ外が正解)。"
)

# プロジェクト判定(最長一致)。via: UI 内=source、ライブラリ=nuget(全依存が
# _feed からの PackageReference 復元であるため)。
PROJECTS = [
    ("Iryo.Legacy.Claims", "nuget"),
    ("Iryo.ReceptionDesk", "source"),
    ("Iryo.AdminConsole", "source"),
    ("Iryo.Portal.Web", "source"),
    ("Iryo.Scheduling", "nuget"),
    ("Iryo.Reporting", "nuget"),
    ("Iryo.Pharmacy", "nuget"),
    ("Iryo.Billing", "nuget"),
    ("Iryo.Interop", "nuget"),
    ("Iryo.Kernel", "nuget"),
    ("Iryo.Domain", "nuget"),
    ("Iryo.Data", "nuget"),
]

# category 先頭セグメント → (today.failure_mode 既定, today.note 既定, why 既定)
TODAY_DEFAULTS = {
    "dependency-injection": ("missing-edge",
        "DI 登録はジェネリック呼び出しのため今日は落ちる(V6)。インタフェース型呼び出しは単純名一致でどこへでも繋がり得る(V11)",
        "束縛先はソース上のどこにも書かれていない。合成ルートの登録が決める"),
    "overloads": ("overload-collapse",
        "同名オーバーロードは今日 1 ノードに縮退する(method_id は file|class|name のハッシュ)",
        "C# の betterness 規則が必要。名前一致では判別不能"),
    "operators": ("missing-node",
        "演算子・変換演算子の宣言は今日抽出されず、呼び出しにも名前トークンがない",
        "ソースにメソッド名が現れない束縛"),
    "cross-package-twins": ("global-name-collision",
        "methodname_to_id / classname_to_id は全体で単純名の先勝ちマップ",
        "同名同シグネチャの別パッケージ宣言。呼び出し側ファイルの using 構成だけが区別する"),
    "default-interface-method": ("id-collision",
        "インタフェース既定実装とクラス上書きは名前一致では区別できない",
        "既定実装か上書きかは実装クラスの宣言有無で決まる"),
    "binary-boundary": ("global-name-collision",
        "3 つの同名 ReceiptFormatter が先勝ちで縮退。参照機構(.csproj/NuGet)は今日いっさい読まれない",
        "コンパイル済みパッケージ越しの束縛。パッケージ id からリポジトリ内ソースへ橋を架ける必要がある"),
    "namespace-tricks": ("namespace-lost",
        "名前空間はファイルに 1 つ(最後の宣言が勝つ)しか記録されず、同名クラスは 1 行に併合される(V8)",
        "C# の名前解決規則(型がネームスペースを影で覆う・global::・別名)が正解を決める"),
    "linq": ("missing-edge",
        "クエリ構文の Where/Select は今日いっさい捕捉されない(V5)。メソッド構文は BCL 呼び出しと衝突する",
        "束縛先はレシーバ型と拡張メソッドの betterness で決まる"),
    "extension-methods": ("global-name-collision",
        "レシーバが捨てられるため、インスタンス版と拡張版の Log は同じ裸の名前になる",
        "インスタンス優先・拡張は署名の隙間のみ、という意味論は構文からは見えない"),
    "explicit-interfaces": ("id-collision",
        "public 版と 2 つの明示的実装はすべて同じ method_id に併合される(V7)",
        "同じ見た目の 3 呼び出しがレシーバの静的型だけで別メンバーに分岐する"),
    "generics": ("missing-edge",
        "ジェネリック呼び出しは落ちるか型引数付きで捕捉されて照合に失敗する(V6)。アリティ三兄弟は同名に縮退",
        "別名展開とアリティの区別が必要"),
    "inheritance": ("global-name-collision",
        "Save/Describe/Clone が階層内で名前衝突。ctor・base 連鎖は呼び出しとして出ない(V3)",
        "束縛先はレシーバの静的型が継承鎖のどこで解決されるかで決まる"),
    "delegates": ("missing-edge",
        "メソッドグループ・イベント購読・?.Invoke は invocation 式を持たず、今日は何も抽出されない(V3)",
        "括弧なしの参照でも委譲シグネチャによる完全なオーバーロード解決が起きる"),
    "parser-blind-spots": ("missing-node",
        "入れ子型・partial・ローカル関数・nameof はノード化されないか幻の呼び出しになる",
        "構文抽出には不可視だが、正確な宣言位置を持つ"),
    "unresolvable": ("phantom-call",
        "dynamic とリフレクション文字列は静的な単一解を持たない",
        "正しいツールは RUNTIME / 候補集合 と答えるべきで、単一のターゲットをでっち上げてはならない"),
    "cross-project-divergence": ("global-name-collision",
        "同一ソーステキストがプロジェクトごとに別パッケージへ束縛される。今日のグラフはプロジェクト境界を持たない",
        "対になるケースと 1 文字違わぬ行。プロジェクトの using / 参照構成だけが束縛先を分ける"),
    "var-inference": ("global-name-collision",
        "レシーバが捨てられるため Find/Ping/Describe は先勝ちで任意の同名へ繋がる",
        "レシーバに型トークンがない。戻り値型の伝播が必要"),
    "web": ("global-name-collision",
        "Razor PageModel の呼び出しも通常の C# と同様に名前照合のみ。cshtml 側は今日ツリーシッター解析されない",
        "Web UI からパッケージ境界越しに束縛する通常ケース"),
}

EXTENSION_CLASSES = {"KarteExtensions", "VitalSequenceLinq"}


def load_cases():
    spec = importlib.util.spec_from_file_location("gt_cases", os.path.join(HERE, "cases.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def member_name(symbol):
    if symbol.endswith("{ get }"):
        head = symbol[: symbol.rindex("{ get }")].strip().rstrip(".")
        return head.rsplit(".", 1)[-1]
    if ".implicit operator" in symbol:
        return "implicit"
    if ".operator " in symbol:
        return "+"
    if "/" in symbol and symbol.endswith(")"):
        return symbol.rsplit("/", 1)[-1].split("(", 1)[0]
    if "..ctor(" in symbol:
        return symbol.split("..ctor(", 1)[0].rsplit(".", 1)[-1]
    if "(" in symbol:
        return symbol.split("(", 1)[0].rsplit(".", 1)[-1]
    simple = symbol.rsplit(".", 1)[-1]
    return simple.split("<", 1)[0].split("+", 1)[-1]


def kind_of(symbol):
    if symbol.endswith("{ get }"):
        return "indexer_get" if ".this[" in symbol else "property_get"
    if ".implicit operator" in symbol:
        return "conversion_operator"
    if ".operator " in symbol:
        return "operator"
    if "/" in symbol and symbol.endswith(")"):
        return "local_function"
    if "..ctor(" in symbol:
        return "constructor"
    if "(" not in symbol:
        return "type"
    head = symbol.split("(", 1)[0]
    parts = head.rsplit(".", 2)
    if len(parts) >= 2 and parts[-2] in EXTENSION_CLASSES:
        return "extension_method"
    if ".IPaymentPolicy.Fee" in symbol:
        return "interface_default_method"
    return "method"


def project_of(symbol):
    for prefix, via in sorted(PROJECTS, key=lambda p: -len(p[0])):
        if symbol == prefix or symbol.startswith(prefix + "."):
            return prefix, via
    raise SystemExit("プロジェクト判定不能: %s" % symbol)


def locate_snippet(case_id, relpath, snippet):
    full = os.path.join(ROOT, relpath)
    with open(full, encoding="utf-8") as fh:
        lines = fh.readlines()
    hits = [i + 1 for i, line in enumerate(lines) if snippet in line]
    if len(hits) != 1:
        raise SystemExit("%s: スニペットが一意でない (%d 件): %s : %r"
                         % (case_id, len(hits), relpath, snippet))
    return hits[0], lines[hits[0] - 1].strip()


def locate_pin(pins, symbol):
    if symbol not in pins:
        raise SystemExit("宣言アンカー未登録: %s" % symbol)
    relpath, pattern, occurrence = pins[symbol]
    full = os.path.join(ROOT, relpath)
    seen = 0
    with open(full, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            hay = line.strip() if pattern.startswith("^") else line
            if re.search(pattern, hay):
                seen += 1
                if seen == occurrence:
                    return relpath, lineno
    raise SystemExit("宣言アンカーが見つからない: %s (%s)" % (symbol, pattern))


def parse_expect(expect):
    if isinstance(expect, list):
        return "resolved", expect
    if expect == "RUNTIME":
        return "runtime", []
    m = re.fullmatch(r"AMBIGUOUS\((\d+)\)", expect)
    if m:
        return "ambiguous", [{"candidate_count": int(m.group(1))}]
    m = re.match(r"EXTERNAL\(([^)]+)\)\s+(.*)", expect)
    if m:
        return "external", [{
            "symbol": m.group(2), "kind": None, "project": None,
            "assembly": m.group(1), "via": "bcl", "file": None, "line": None,
        }]
    raise SystemExit("expect の形式不正: %r" % expect)


def default_site_kind(case, resolution, symbols):
    if "site_kind" in case:
        return case["site_kind"]
    if resolution != "resolved":
        return "invocation"
    kind = kind_of(symbols[0])
    if kind in ("type", "constructor"):
        return "object_creation"
    return "invocation"


def main():
    cases_mod = load_cases()
    all_pins = dict(cases_mod.DECL_PINS)
    known_static = dict(cases_mod.STATIC_TARGET_PINS)

    inventory = {}
    for sym in list(all_pins) + list(known_static):
        inventory.setdefault(member_name(sym), set()).add(sym)

    ids = [c["id"] for c in cases_mod.CASES]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit("ケース ID 重複: %s" % dupes)

    out_cases = []
    for case in sorted(cases_mod.CASES, key=lambda c: c["id"]):
        resolution, raw_targets = parse_expect(case["expect"])
        if resolution == "external":
            raw_targets[0]["kind"] = case.get("external_kind", "method")
        lineno, source_line = locate_snippet(case["id"], case["file"], case["snippet"])
        if case["token"] not in source_line.split("//")[0]:
            raise SystemExit("トークンが行内にない: %s token=%r line=%r" % (case["id"], case["token"], source_line))

        targets = []
        if resolution == "resolved":
            for sym in raw_targets:
                project, via = project_of(sym)
                decl_file, decl_line = locate_pin(all_pins, sym)
                targets.append({
                    "symbol": sym, "kind": kind_of(sym), "project": project,
                    "assembly": project, "via": via,
                    "file": decl_file, "line": decl_line,
                })
        else:
            targets = raw_targets

        distractors = []
        if resolution == "resolved":
            # 正解(targets)に加え、static_target / runtime_target として
            # 文書化されるシンボルも distractors から除外する。
            expected = {t["symbol"] for t in targets}
            expected |= {case[k] for k in ("static_target", "runtime_target") if k in case}
            for t in targets:
                for other in sorted(inventory.get(member_name(t["symbol"]), ())):
                    if other not in expected and other not in distractors:
                        distractors.append(other)
            distractors = distractors[:8]

        top_category = case["category"].split("/")[0]
        mode_default, note_default, why_default = TODAY_DEFAULTS[top_category]

        entry = {
            "id": case["id"],
            "category": case["category"],
            "file": case["file"],
            "line": lineno,
            "token": case["token"],
            "site_kind": default_site_kind(case, resolution, raw_targets if resolution == "resolved" else []),
            "snippet": case["snippet"],
            "source_line": source_line,
            "expected": {"resolution": resolution, "targets": targets},
            "distractors": distractors,
            "today": {
                "failure_mode": case.get("today_mode", mode_default),
                "note": case.get("today_note", note_default),
            },
            "why_hard": case.get("why", why_default),
        }
        for extra in ("static_target", "runtime_target"):
            if extra in case:
                sym = case[extra]
                pins = {**all_pins, **known_static}
                decl_file, decl_line = locate_pin(pins, sym)
                entry["expected"][extra] = {"symbol": sym, "file": decl_file, "line": decl_line}
        out_cases.append(entry)

    manifest = {
        "version": 2,
        "root": "test_codes/iryo-kanri-system",
        "language_note": "why_hard / today.note は日本語。symbol は英語の C# シンボル文法。",
        "symbol_grammar": SYMBOL_GRAMMAR,
        "case_count": len(out_cases),
        "cases": out_cases,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("wrote %s (%d cases)" % (os.path.relpath(OUT, ROOT), len(out_cases)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
