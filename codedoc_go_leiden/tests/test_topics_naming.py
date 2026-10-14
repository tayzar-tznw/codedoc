"""naming: the batched Japanese prompt, answer validation, retry, hub fallback
and inherited names (leiden spec §5.1), with a fake model."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph_generator.topics import naming  # noqa: E402
from graph_generator.topics.community import detect  # noqa: E402
from graph_generator.topics.graph_builder import build_projection  # noqa: E402
from graph_generator.topics.synthetic import RowBuilder  # noqa: E402


class FakeModel:
    """Returns scripted answers in order; a callable answer gets the prompt."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer(prompt) if callable(answer) else answer


def ids_in(prompt: str) -> list[str]:
    return [line[5:].strip() for line in prompt.splitlines() if line.startswith("#### ")]


def echo(prefix: str):
    return lambda prompt: json.dumps({tid: f"{prefix}{i}号機能" for i, tid in enumerate(ids_in(prompt))},
                                     ensure_ascii=False)


def entry(tid, level=1, parent=None, hub="billing.FeeCalculator", inherited_from=None):
    return {"topic_id": tid, "level": level, "parent": parent, "size": 5, "file_count": 2, "hub": hub,
            "members": [{"name": "billing.FeeCalculator", "kind": "struct",
                         "package": "example.com/iryo/billing", "summary": "診療報酬を計算する"}],
            "packages": ["example.com/iryo/billing"], "modules": ["example.com/iryo"],
            "neighbours": [], "inherited_from": inherited_from}


def two_feature_rows():
    b = RowBuilder()
    names = {}
    for code, path in (("FeeCalculator", "billing/fee.go"), ("Points", "billing/points.go"),
                       ("Ledger", "billing/ledger.go"), ("Reception", "reception/desk.go"),
                       ("Queue", "reception/queue.go"), ("Slot", "reception/slot.go")):
        names[code] = b.type(path, code, summary=f"{code} の要約\n二行目")
    n = 0
    for x, y in (("FeeCalculator", "Points"), ("Points", "Ledger"), ("FeeCalculator", "Ledger"),
                 ("Reception", "Queue"), ("Queue", "Slot"), ("Reception", "Slot")):
        for _ in range(3):
            n += 1
            b.call(b.method(names[x], f"m{n}"), b.method(names[y], f"m{n}"))
    b.call(b.method(names["Reception"], "Bill"), b.method(names["FeeCalculator"], "Calculate"))
    return b, names


def test_naming_inputs_and_prompt_carry_the_structural_evidence():
    b, names = two_feature_rows()
    proj = build_projection(*b.rows())
    data = detect(proj, {"band": (2, 4)}).data
    entries = naming.naming_inputs(data, proj, summaries={names["Points"]: "ポイントを丸める\n詳細"})
    assert [e["level"] for e in entries] == [1, 1]
    billing = next(e for e in entries if any(m["name"] == "billing.FeeCalculator" for m in e["members"]))
    other = next(e for e in entries if e is not billing)
    assert billing["neighbours"] == [other["topic_id"]]
    assert billing["packages"] == ["example.com/iryo/billing"] and billing["modules"] == ["example.com/iryo"]
    assert {m["summary"] for m in billing["members"]} >= {"ポイントを丸める", "FeeCalculator の要約"}
    assert billing["size"] == 6 and billing["file_count"] == 3
    prompt = naming.build_naming_prompt(entries, known={other["topic_id"]: "患者受付"})
    for needle in ("機能・特徴別", "2〜5語", f"#### {billing['topic_id']}", "規模: 型・疑似型 6 個、ファイル 3 個",
                   "ハブ:", "主なメンバー（トピック内 PageRank 順）", "パッケージ: example.com/iryo/billing",
                   "モジュール: example.com/iryo", "関係の強い隣接トピック: 患者受付", "JSON オブジェクトだけ"):
        assert needle in prompt


def test_naming_inputs_keep_the_top_twelve_members():
    b = RowBuilder()
    hub = b.type("big/hub.go", "Hub")
    for i in range(15):
        leaf = b.type(f"big/leaf{i}.go", f"Leaf{i}")
        b.call(b.method(hub, f"m{i}"), b.method(leaf, "m"))
    proj = build_projection(*b.rows())
    data = detect(proj, {"band": (1, 3)}).data
    (e,) = naming.naming_inputs(data, proj)
    assert len(e["members"]) == 12 and e["members"][0]["name"] == "big.Hub"


@pytest.mark.parametrize("name,problem", [
    ("診療報酬計算", None), ("", "empty"), ("   ", "empty"), ("トピック1", "placeholder"),
    ("Topic 2", "placeholder"), ("topic12", "placeholder"), ("トピック１２", "placeholder"),
    ("会計トピック", None), ("未分類", "placeholder"), ("モジュール名", "placeholder"),
    ("a_0123456789abcdef", "placeholder"), ("{name}", "placeholder"), ("x" * 41, "too long"),
    ("一行目\n二行目", "too long"), (42, "not a string"),
])
def test_name_problem(name, problem):
    assert naming.name_problem(name) == problem


def test_names_are_unique_after_normalisation():
    taken = {naming.normalize_name("患者 受付")}
    assert naming.name_problem("患者受付", taken) == "duplicate"
    assert naming.name_problem("「患者受付」", taken) == "duplicate"
    assert naming.normalize_name("ＡＢＣ　会計") == "abc会計"


def test_parse_names_validates_each_id():
    text = "```json\n" + json.dumps({"a_1": "診療報酬計算", "a_2": "診療報酬計算", "a_3": "トピック3",
                                     "zzz": "extra"}, ensure_ascii=False) + "\n```"
    valid, problems = naming.parse_names(text, ["a_1", "a_2", "a_3", "a_4"])
    assert valid == {"a_1": "診療報酬計算"}
    assert problems == {"a_2": "duplicate", "a_3": "placeholder", "a_4": "missing"}
    assert naming.parse_names("not json", ["a_1"])[1]["a_1"].startswith("unparsable")
    assert naming.parse_names("[1, 2]", ["a_1"])[1] == {"a_1": "unparsable answer (no JSON object)"}
    assert naming.parse_names('{"a_1": }', ["a_1"])[1]["a_1"].startswith("unparsable")
    assert naming.parse_names('答えです: {"a_1": "会計"} 以上', ["a_1"])[0] == {"a_1": "会計"}


def test_valid_answer_needs_one_call_per_batch():
    model = FakeModel(echo("会計"))
    result = naming.name_topics([entry("a_1"), entry("a_2")], model)
    assert result["names"] == {"a_1": {"name": "会計0号機能", "label_source": "llm"},
                               "a_2": {"name": "会計1号機能", "label_source": "llm"}}
    assert result["stats"]["calls"] == 1 and result["stats"]["llm"] == 2


def test_invalid_names_get_one_retry_with_the_reasons():
    model = FakeModel(json.dumps({"a_1": "会計"}, ensure_ascii=False),
                      json.dumps({"a_2": "受付"}, ensure_ascii=False))
    result = naming.name_topics([entry("a_1"), entry("a_2")], model)
    assert result["names"]["a_2"] == {"name": "受付", "label_source": "llm"}
    assert ids_in(model.prompts[1]) == ["a_2"]
    assert "前回の回答の問題" in model.prompts[1] and "a_2: missing" in model.prompts[1]
    assert "使用済み" in model.prompts[1] and "会計" in model.prompts[1]
    assert result["stats"]["retries"] == 1 and result["stats"]["calls"] == 2


def test_still_invalid_names_fall_back_to_unique_hub_labels():
    model = FakeModel("{}", json.dumps({"a_1": "トピック1"}, ensure_ascii=False))
    result = naming.name_topics([entry("a_1", hub="billing.Fee"), entry("a_2", hub="billing.Fee")], model)
    assert result["names"]["a_1"] == {"name": "billing.Fee", "label_source": "hub"}
    assert result["names"]["a_2"] == {"name": "billing.Fee (2)", "label_source": "hub"}
    assert result["stats"]["fallbacks"] == 2


def test_failed_calls_are_recorded_and_fall_back():
    model = FakeModel(RuntimeError("quota"), RuntimeError("quota"))
    result = naming.name_topics([entry("a_1")], model)
    assert result["names"]["a_1"]["label_source"] == "hub"
    assert result["stats"]["errors"] == ["RuntimeError: quota", "RuntimeError: quota"]


def test_batches_hold_at_most_sixty_topics():
    model = FakeModel(echo("一"), echo("二"), echo("三"))
    entries = [entry(f"a_{i:03d}") for i in range(130)]
    result = naming.name_topics(entries, model)
    assert [len(ids_in(p)) for p in model.prompts] == [60, 60, 10]
    assert result["stats"]["llm"] == 130


def test_subtopics_are_named_after_their_parent():
    model = FakeModel(json.dumps({"a_1": "診療報酬"}, ensure_ascii=False),
                      json.dumps({"a_2": "点数計算"}, ensure_ascii=False))
    result = naming.name_topics([entry("a_2", level=2, parent="a_1"), entry("a_1")], model)
    assert ids_in(model.prompts[0]) == ["a_1"]
    assert "上位トピック: 診療報酬" in model.prompts[1]
    assert result["names"]["a_2"]["name"] == "点数計算"


def test_inherited_names_are_reused_and_not_asked():
    model = FakeModel(echo("新"))
    previous = {"names": {"old_1": {"name": "診療報酬計算", "label_source": "llm"},
                          "old_2": {"name": "billing.Fee", "label_source": "hub"}}}
    entries = [entry("a_1", inherited_from="old_1"), entry("a_2", inherited_from="old_2"), entry("a_3")]
    result = naming.name_topics(entries, model, previous_names=previous)
    assert result["names"]["a_1"] == {"name": "診療報酬計算", "label_source": "inherited", "inherited_from": "old_1"}
    assert ids_in(model.prompts[0]) == ["a_2", "a_3"]   # a hub fallback is asked again
    assert result["stats"]["inherited"] == 1
    assert naming.name_topics([entry("b_1", inherited_from="x")], FakeModel(),
                              previous_names={"x": "受付"})["names"]["b_1"]["name"] == "受付"


def test_members_without_summary_show_their_functions():
    b = RowBuilder()
    t = b.type("billing/fee.go", "Fee")
    pkg_funcs = [b.func("billing/fee.go", name) for name in ("NewFee", "Round", "Total", "Clamp", "Add", "Sub")]
    for i, fn in enumerate(pkg_funcs):
        b.call(fn, b.method(t, f"M{i}"))
    other = b.type("billing/points.go", "Points")
    b.call(b.method(t, "X"), b.method(other, "Y"))
    proj = build_projection(*b.rows())
    data = detect(proj, {"band": (1, 3)}).data
    (e,) = naming.naming_inputs(data, proj)
    pkg = next(m for m in e["members"] if m["name"] == "billing/fee.go")
    assert pkg["kind"] == "ファイルの関数"
    assert pkg["summary"] == "関数: Add, Clamp, NewFee, Round, Sub ほか"


def test_unassigned_topic_is_never_sent_to_the_model():
    b, _names = two_feature_rows()
    b.type("dto/lonely.go", "Lonely")
    proj = build_projection(*b.rows())
    data = detect(proj, {"band": (2, 4)}).data
    assert data["unassigned_topic"]
    entries = naming.naming_inputs(data, proj)
    assert len(entries) == 2 and data["unassigned_topic"] not in {e["topic_id"] for e in entries}


def test_a_cut_answer_falls_back_to_hub_labels_without_a_re_ask():
    model = FakeModel(naming.AnswerCut("cut at the output budget"))
    result = naming.name_topics([entry("a_1", hub="billing.Fee"), entry("a_2", hub="reception.Desk")], model)
    assert len(model.prompts) == 1 and result["stats"]["retries"] == 0
    assert {t: n["label_source"] for t, n in result["names"].items()} == {"a_1": "hub", "a_2": "hub"}
    assert result["stats"]["errors"] == ["AnswerCut: cut at the output budget"]
