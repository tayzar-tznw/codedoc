#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expected_resolutions.json の独立検証ゲート。

generate_manifest.py とコードを共有せずに検証する:
  1. 往復: 各ケースの file:line が実在し、その行がスニペットとトークンを含む。
  2. 一意性: スニペットはそのファイル内で正確に 1 回だけ出現する。
  3. アンカー: 解決済みターゲットの宣言 file:line が実在し、その行に
     メンバー名(または operator / this[ / 型名 / ローカル関数名)が現れる。
  4. 全単射: manifest の ID 集合 == cases.py の ID 集合。
テストコード編集後は generate_manifest.py → 本スクリプトの順で必ず実行する。
終了コード 0 = 問題なし。
"""

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(HERE, "expected_resolutions.json")

problems = []


def problem(msg):
    problems.append(msg)


def load_lines(relpath, cache={}):
    if relpath not in cache:
        full = os.path.join(ROOT, relpath)
        if not os.path.isfile(full):
            cache[relpath] = None
        else:
            with open(full, encoding="utf-8") as fh:
                cache[relpath] = fh.readlines()
    return cache[relpath]


def anchor_token(symbol):
    if ".implicit operator" in symbol:
        return "implicit operator"
    if ".operator " in symbol:
        return "operator +"
    if ".this[" in symbol:
        return "this["
    if "/" in symbol and symbol.endswith(")"):
        return symbol.rsplit("/", 1)[-1].split("(", 1)[0]
    if "..ctor(" in symbol:
        return symbol.split("..ctor(", 1)[0].rsplit(".", 1)[-1]
    if symbol.endswith("{ get }"):
        head = symbol[: symbol.rindex("{ get }")].strip().rstrip(".")
        return head.rsplit(".", 1)[-1]
    if "(" in symbol:
        return symbol.split("(", 1)[0].rsplit(".", 1)[-1]
    simple = symbol.rsplit(".", 1)[-1]
    return simple.split("<", 1)[0].split("+", 1)[-1]


def check_anchor(case_id, label, symbol, relpath, lineno):
    lines = load_lines(relpath)
    if lines is None:
        problem("%s: %s の宣言ファイルがない: %s" % (case_id, label, relpath))
        return
    if not (1 <= lineno <= len(lines)):
        problem("%s: %s の宣言行が範囲外: %s:%d" % (case_id, label, relpath, lineno))
        return
    token = anchor_token(symbol)
    if token not in lines[lineno - 1]:
        problem("%s: 宣言行 %s:%d に %r がない (%s)" % (case_id, relpath, lineno, token, symbol))


def main():
    with open(MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)

    if manifest["case_count"] != len(manifest["cases"]):
        problem("case_count %d != len(cases) %d" % (manifest["case_count"], len(manifest["cases"])))

    for case in manifest["cases"]:
        cid = case["id"]
        lines = load_lines(case["file"])
        if lines is None:
            problem("%s: ファイルがない: %s" % (cid, case["file"]))
            continue
        if not (1 <= case["line"] <= len(lines)):
            problem("%s: 行 %d が範囲外 (%s)" % (cid, case["line"], case["file"]))
            continue
        line = lines[case["line"] - 1]
        if case["snippet"] not in line:
            problem("%s: 行 %s:%d にスニペットがない: %r" % (cid, case["file"], case["line"], case["snippet"]))
        if case["token"] not in line.split("//")[0]:
            problem("%s: 行にトークン %r がない" % (cid, case["token"]))
        occurrences = sum(1 for l in lines if case["snippet"] in l)
        if occurrences != 1:
            problem("%s: スニペットが %d 回出現 (一意でない): %r" % (cid, occurrences, case["snippet"]))
        if case["source_line"] != line.strip():
            problem("%s: source_line が実ファイルとずれている" % cid)

        exp = case["expected"]
        for t in exp["targets"]:
            if t.get("via") in ("source", "nuget"):
                check_anchor(cid, "target", t["symbol"], t["file"], t["line"])
        for extra in ("static_target", "runtime_target"):
            if extra in exp:
                check_anchor(cid, extra, exp[extra]["symbol"], exp[extra]["file"], exp[extra]["line"])

    spec = importlib.util.spec_from_file_location("gt_cases", os.path.join(HERE, "cases.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_ids = {c["id"] for c in module.CASES}
    manifest_ids = {c["id"] for c in manifest["cases"]}
    for cid in sorted(source_ids - manifest_ids):
        problem("cases.py の %s が manifest にない" % cid)
    for cid in sorted(manifest_ids - source_ids):
        problem("manifest の %s が cases.py にない" % cid)

    if problems:
        for p in problems:
            print("MISMATCH:", p)
        print("%d 件の不一致" % len(problems))
        return 1
    print("ground truth OK: %d cases, all round-trips clean" % len(manifest["cases"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
