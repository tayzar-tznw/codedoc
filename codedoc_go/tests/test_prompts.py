"""prompts: placeholders match the wiki track's arguments; Japanese prompts;
topic prompts ask for repo-relative paths; PROMPT_VERSION keys the caches."""

import re
import string

import pytest

from graph_generator import prompts

_FORMAT_ARGS = {
    "NODE_BATCH_PROMPT": {"relpath": "libs/billing/fee.go", "package": "example.com/iryo/billing",
                          "source": "package billing", "declarations": "- Fee（func）"},
    "NODE_SINGLE_PROMPT": {"relpath": "libs/billing/fee.go", "package": "example.com/iryo/billing",
                           "qualname": "Fee", "kind": "func", "context": "呼び出し元: x.Y",
                           "source": "func Fee() {}"},
    "FILE_FROM_NODES_PROMPT": {"relpath": "libs/billing/fee.go",
                               "package": "example.com/iryo/billing", "node_summaries": "- Fee: 料金"},
    "PACKAGE_SUMMARY_PROMPT": {"import_path": "example.com/iryo/billing", "directory": "libs/billing",
                               "name": "billing", "file_summaries": "s"},
    "DIR_SUMMARY_PROMPT": {"dir_name": "libs/billing", "child_file_summaries": "a",
                           "child_dir_summaries": "b"},
    "TOPIC_EXTRACTION_PROMPT": {"summaries": "s"},
    "TOPIC_SUMMARY_PROMPT": {"topic_name": "料金計算", "file_contents": "c", "file_summaries": "s"},
    "TOPIC_MERGE_PROMPT": {"topics": "1. 料金計算（2 ファイル）: 診療報酬を計算する"},
}


@pytest.mark.parametrize("name,args", sorted(_FORMAT_ARGS.items()))
def test_prompt_fields_match_pipeline_arguments(name, args):
    template = getattr(prompts, name)
    fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
    assert fields == set(args)
    assert template.format(**args)


@pytest.mark.parametrize("name", sorted(_FORMAT_ARGS))
def test_prompts_are_japanese(name):
    text = getattr(prompts, name)
    assert len(re.findall(r"[぀-ヿ一-鿿]", text)) > 20
    assert "日本語" in text


def test_prompt_set_and_wording():
    names = [n for n in dir(prompts) if n.endswith("_PROMPT")]
    assert sorted(names) == sorted(_FORMAT_ARGS)
    joined = "\n".join(getattr(prompts, n) for n in names)
    for word in (".php", "Phinx", "マイグレーション", '"fee.py"', "linked_files", "クラス"):
        assert word not in joined
    assert "JSON形式" in prompts.TOPIC_EXTRACTION_PROMPT  # the conftest fake keys on it
    assert "統合してください" in prompts.TOPIC_MERGE_PROMPT  # the wiki tests key on it
    assert prompts.PROMPT_VERSION == "go-wiki-2"  # bumped for R49


def test_batched_prompt_asks_for_every_declaration_in_json():
    text = prompts.NODE_BATCH_PROMPT
    assert '"file"' in text and '"nodes"' in text and "qualname をそのまま" in text
    assert "ONLY valid JSON" in text
    # R49: the real run's answers keyed nodes by import path + qualname
    assert "インポートパスやパッケージ名を前に付けないでください" in text


def test_extraction_asks_for_relpaths_and_a_summary():
    template = prompts.TOPIC_EXTRACTION_PROMPT
    assert '"files": ["libs/billing/fee.go"' in template
    assert "リポジトリ相対パス" in template
    assert '"summary"' in template  # the merge reads it (R49)


def test_merge_answers_with_member_numbers_not_files():
    """R49: the merge answer stays small at any repo size."""
    template = prompts.TOPIC_MERGE_PROMPT
    assert '"members": [1, 3]' in template and '"summary"' in template
    assert ".go" not in template and '"files"' not in template
    assert "すべての番号" in template


def test_topics_are_not_called_modules():
    """In Go a module is a go.mod module; topics are Gemini's feature groups (D7)."""
    for name in _FORMAT_ARGS:
        assert "モジュール" not in getattr(prompts, name)
