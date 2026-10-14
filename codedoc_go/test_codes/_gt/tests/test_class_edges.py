"""cases.py CLASS_EDGES (spec R50): a fixture's exact ClassImplements / ClassEmbeds set goes into
the manifest key "class_edges" (only for a fixture that defines it), and check_ground_truth
validates it."""
import pytest

import check_ground_truth as cg
import generate_manifest as gm
from test_generate_manifest import manifest_of, replace_in_cases

CORE = "tiny:lib/core/core.go:"
DOG = ('{"table": "ClassImplements", "source": "' + CORE + 'Dog", "target": "' + CORE + 'Speaker", '
       '"pointer_receiver": False}')
CAT = ('{"table": "ClassImplements", "source": "' + CORE + 'Cat", "target": "' + CORE + 'Speaker", '
       '"pointer_receiver": True}')


def with_edges(root, *edges):
    replace_in_cases(root, "XPR_PAIRS = []\n", "XPR_PAIRS = []\nCLASS_EDGES = [" + ", ".join(edges) + "]\n")
    return root


def generate(root, capsys):
    assert gm.main(["generate_manifest.py", root]) == 0
    return capsys.readouterr().out


def check(root, capsys):
    rc = cg.main(["check_ground_truth.py", root])
    return rc, capsys.readouterr().out


def test_manifest_carries_class_edges_only_when_cases_define_them(tiny, capsys):
    out = generate(tiny, capsys)
    assert "class_edges" not in manifest_of(tiny) and "class edges" not in out
    rc, out = check(tiny, capsys)
    assert rc == 0 and "class edges" not in out
    out = generate(with_edges(tiny, DOG, CAT), capsys)
    assert "2 class edges" in out
    edges = manifest_of(tiny)["class_edges"]
    assert [(e["source"], e["pointer_receiver"]) for e in edges] == [(CORE + "Dog", False), (CORE + "Cat", True)]
    rc, out = check(tiny, capsys)
    assert rc == 0 and "2 class edges" in out


def test_manifest_must_equal_cases(tiny, capsys):
    generate(with_edges(tiny, DOG, CAT), capsys)
    replace_in_cases(tiny, '"pointer_receiver": True}', '"pointer_receiver": False}')
    rc, out = check(tiny, capsys)
    assert rc == 1 and "class_edges in manifest.json differ from cases.py CLASS_EDGES" in out


@pytest.mark.parametrize("edge, message", [
    ('{"table": "ClassInherits", "source": "' + CORE + 'Dog", "target": "' + CORE + 'Speaker"}', "bad table"),
    ('{"table": "ClassImplements", "source": "' + CORE + 'Dog", "target": "' + CORE + 'Speaker"}',
     "needs a bool 'pointer_receiver' and no 'pointer'"),
    ('{"table": "ClassEmbeds", "source": "' + CORE + 'Dog", "target": "' + CORE + 'Greeter", "pointer": False, '
     '"pointer_receiver": False}', "needs a bool 'pointer' and no 'pointer_receiver'"),
    ('{"table": "ClassImplements", "source": "' + CORE + 'Dog", "target": "' + CORE + 'Speaker", '
     '"pointer_receiver": False, "modes": ["online"]}', "modes must be a non-empty list"),
    ('{"table": "ClassImplements", "source": "' + CORE + 'Dog", "target": "' + CORE + 'Speaker", '
     '"pointer_receiver": False, "note": 1}', "unknown keys ['note']"),
    ('{"table": "ClassImplements", "source": "' + CORE + 'Wolf", "target": "' + CORE + 'Speaker", '
     '"pointer_receiver": False}', "names no type declared in its file: " + CORE + "Wolf"),
    ('{"table": "ClassImplements", "source": "' + CORE + 'Chorus", "target": "' + CORE + 'Speaker", '
     '"pointer_receiver": False}', "names no type declared in its file: " + CORE + "Chorus"),
    ('{"table": "ClassImplements", "source": "' + CORE + '<package>", "target": "' + CORE + 'Speaker", '
     '"pointer_receiver": False}', "names no type declared in its file"),
    ('{"table": "ClassImplements", "source": "lib/core/core.go:Dog", "target": "' + CORE + 'Speaker", '
     '"pointer_receiver": False}', "is not <repo>:<relpath>:<qualname>"),
    ('{"table": "ClassEmbeds", "source": "' + CORE + 'Dog", "target": "' + CORE + 'Dog", "pointer": False}',
     "from a type to itself"),
])
def test_bad_class_edges_are_problems(tiny, capsys, edge, message):
    generate(with_edges(tiny, edge), capsys)
    rc, out = check(tiny, capsys)
    assert rc == 1 and message in out


def test_duplicate_class_edge(tiny, capsys):
    generate(with_edges(tiny, DOG, DOG), capsys)
    rc, out = check(tiny, capsys)
    assert rc == 1 and "duplicate class edge" in out


def test_modes_limit_an_edge(tiny, capsys):
    env_only = DOG[:-1] + ', "modes": ["env"]}'
    generate(with_edges(tiny, env_only, CAT), capsys)
    rc, out = check(tiny, capsys)
    assert rc == 0 and manifest_of(tiny)["class_edges"][0]["modes"] == ["env"]
