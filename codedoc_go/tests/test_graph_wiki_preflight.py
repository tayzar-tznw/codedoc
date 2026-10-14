"""Final core review #5-#7: the wiki preflight proves Gemini with one cheap
call (USER_PROJECT_DENIED stops also with --no-spanner); a changed ID_PREFIX
or GRAPH_NAME rebuilds the graph and stops --wiki also with --no-spanner; the
GCP project is part of the graph's identity."""
from __future__ import annotations

import json
import os
import types

import pytest

from graph_generator import __main__ as m
from graph_generator import config, pipeline
from graph_generator.pipeline import PipelineData, WikiPreconditionError, run_graph_track
# The real probe, captured before conftest's autouse fixture replaces it.
from graph_generator.pipeline import _gemini_probe as real_gemini_probe
from tests.conftest import SAMPLE_TREE, write_tree
from tests.pipeline_fakes import install_graph_fakes


@pytest.fixture
def adc(monkeypatch):
    import google.auth
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), "adc-project"))


def _probe(monkeypatch, error=None):
    calls = []

    def probe():
        calls.append(True)
        if error is not None:
            raise error
    monkeypatch.setattr(pipeline, "_gemini_probe", probe)
    return calls


# ── #5: one cheap Gemini call in the preflight ──────────────────────

def test_the_gemini_check_probes_the_model_once(adc, monkeypatch):
    calls = _probe(monkeypatch)
    row = pipeline._check_gemini(soft=False)
    assert row.status == "ok" and calls == [True]


def test_an_unusable_quota_project_stops_the_wiki_with_its_fix(adc, monkeypatch):
    monkeypatch.setattr(config, "GCP_PROJECT", "my-project")
    _probe(monkeypatch, RuntimeError(
        "403 PERMISSION_DENIED. {'error': {'status': 'PERMISSION_DENIED', "
        "'details': [{'reason': 'USER_PROJECT_DENIED'}]}}"))
    row = pipeline._check_gemini(soft=False)
    assert row.status == "error" and "quota project" in row.detail
    assert "gcloud auth application-default set-quota-project my-project" in row.fix
    assert pipeline._check_gemini(soft=True).status == "warn"   # doctor


def test_another_gemini_error_is_an_error_row_with_the_message(adc, monkeypatch):
    _probe(monkeypatch, RuntimeError("404 NOT_FOUND. Publisher model gemini-x was not found"))
    row = pipeline._check_gemini(soft=False)
    assert row.status == "error" and "gemini-x was not found" in row.detail
    assert "GEMINI_MODEL" in row.fix and "GOOGLE_CLOUD_LOCATION" in row.fix


def test_no_probe_without_credentials(monkeypatch):
    calls = _probe(monkeypatch)
    assert pipeline._check_gemini(soft=False).status == "error"   # conftest disables ADC
    assert calls == []


def test_the_probe_is_one_count_tokens_call_on_a_client_kept_open(monkeypatch):
    transport = {"closed": False}
    seen = []

    def count_tokens(**kw):
        if transport["closed"]:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        seen.append(kw)

    class Client:   # like google-genai's Client: __del__ closes the transport .models shares
        models = types.SimpleNamespace(count_tokens=count_tokens)

        def __del__(self):
            transport["closed"] = True

    monkeypatch.setattr(pipeline, "_genai_client", Client)
    real_gemini_probe()
    assert seen == [{"model": config.MODEL, "contents": "ping"}]


def test_the_probe_calls_before_the_real_client_closes(monkeypatch):
    from google import genai
    from google.genai import models
    events = []
    close = genai.Client.close
    monkeypatch.setattr(config, "GCP_PROJECT", "offline")   # no ADC lookup, no request
    monkeypatch.setattr(genai.Client, "close", lambda self: (events.append("close"), close(self)))
    monkeypatch.setattr(models.Models, "count_tokens",
                        lambda self, **kw: events.append("count_tokens"))
    real_gemini_probe()
    assert events == ["count_tokens", "close"]


def test_wiki_with_no_spanner_exits_3_on_a_quota_error(tmp_path, out_dir, adc, monkeypatch,
                                                       capsys):
    repo = write_tree(tmp_path / "web", SAMPLE_TREE)
    monkeypatch.setattr(config, "PEER_REPOS", None)
    _probe(monkeypatch, RuntimeError("400 USER_PROJECT_DENIED"))
    monkeypatch.setattr(pipeline, "load_graph_state", lambda *a: pytest.fail("work started"))
    with pytest.raises(SystemExit) as exc:
        m.main(["analyze", repo, "--wiki", "--no-spanner"])
    assert exc.value.code == 3
    assert "set-quota-project" in capsys.readouterr().out


# ── #6: ID_PREFIX and GRAPH_NAME are part of the graph, also locally ──

@pytest.fixture
def repo(tmp_path):
    return write_tree(tmp_path / "iryo", SAMPLE_TREE)


def _checkpoint(out_dir):
    with open(os.path.join(out_dir, "graph_checkpoint.json"), encoding="utf-8") as f:
        return json.load(f)


def test_the_checkpoint_records_the_prefix_graph_and_project(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    cp = _checkpoint(out_dir)
    assert (cp["id_prefix"], cp["graph"], cp["project"]) == (config.ID_PREFIX, config.GRAPH_NAME,
                                                              None)


@pytest.mark.parametrize("key,value", [("ID_PREFIX", "b"), ("GRAPH_NAME", "code_graph_b")])
def test_a_local_rerun_with_another_prefix_or_graph_rebuilds(repo, out_dir, monkeypatch, capsys,
                                                             key, value):
    calls = install_graph_fakes(monkeypatch)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    monkeypatch.setattr(config, key, value)
    calls.clear()
    capsys.readouterr()
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    assert "build_node_rows" in calls and "derive_edge_rows" in calls   # not resumed
    assert "ID_PREFIX, GRAPH_NAME or GCP project changed" in capsys.readouterr().out
    assert _checkpoint(out_dir)[{"ID_PREFIX": "id_prefix", "GRAPH_NAME": "graph"}[key]] == value


@pytest.mark.parametrize("key,value", [("ID_PREFIX", "b"), ("GRAPH_NAME", "code_graph_b")])
def test_wiki_refuses_a_graph_of_another_prefix_or_graph(repo, out_dir, monkeypatch, key, value):
    install_graph_fakes(monkeypatch)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    monkeypatch.setattr(config, key, value)
    data = PipelineData(target_dir=repo, repo="iryo")
    with pytest.raises(WikiPreconditionError, match="ID_PREFIX") as exc:
        pipeline.prepare_wiki(data, no_spanner=True)
    assert "--graph" in str(exc.value) and value in str(exc.value)


def test_an_older_checkpoint_without_the_keys_falls_back_to_graph_meta(repo, out_dir,
                                                                       monkeypatch):
    install_graph_fakes(monkeypatch)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    cp = _checkpoint(out_dir)
    for key in ("id_prefix", "graph", "project"):
        del cp[key]
    with open(os.path.join(out_dir, "graph_checkpoint.json"), "w", encoding="utf-8") as f:
        json.dump(cp, f)
    pipeline.prepare_wiki(PipelineData(target_dir=repo, repo="iryo"), no_spanner=True)  # accepted
    monkeypatch.setattr(config, "ID_PREFIX", "b")                 # graph_meta.json says "a"
    with pytest.raises(WikiPreconditionError, match="ID_PREFIX"):
        pipeline.prepare_wiki(PipelineData(target_dir=repo, repo="iryo"), no_spanner=True)


# ── #7: the GCP project ──────────────────────────────────────────────

def test_graph_meta_records_the_project_of_a_spanner_run(repo, out_dir, monkeypatch,
                                                         fake_spanner):
    install_graph_fakes(monkeypatch)
    monkeypatch.setattr(config, "GCP_PROJECT", "p1")
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    with open(os.path.join(out_dir, "graph_meta.json"), encoding="utf-8") as f:
        assert json.load(f)["project"] == "p1"
    assert _checkpoint(out_dir)["project"] == "p1"


def test_the_adc_default_project_counts_when_none_is_configured(repo, out_dir, monkeypatch,
                                                                fake_spanner, adc):
    install_graph_fakes(monkeypatch)
    monkeypatch.setattr(config, "GCP_PROJECT", None)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    assert _checkpoint(out_dir)["project"] == "adc-project"


def test_another_project_rebuilds_the_graph_and_stops_the_wiki(repo, out_dir, monkeypatch,
                                                              fake_spanner):
    calls = install_graph_fakes(monkeypatch)
    monkeypatch.setattr(config, "GCP_PROJECT", "p1")
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    monkeypatch.setattr(config, "GCP_PROJECT", "p2")
    with pytest.raises(WikiPreconditionError, match="project 'p1'") as exc:
        pipeline.prepare_wiki(PipelineData(target_dir=repo, repo="iryo"), no_spanner=False)
    assert "GOOGLE_CLOUD_PROJECT=p1" in str(exc.value) and "'p2'" in str(exc.value)
    calls.clear()
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))       # p2: not resumed
    assert "build_node_rows" in calls
    pipeline.prepare_wiki(PipelineData(target_dir=repo, repo="iryo"), no_spanner=False)


def test_a_local_wiki_ignores_the_project(repo, out_dir, monkeypatch, fake_spanner):
    install_graph_fakes(monkeypatch)
    monkeypatch.setattr(config, "GCP_PROJECT", "p1")
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    monkeypatch.setattr(config, "GCP_PROJECT", "p2")
    pipeline.prepare_wiki(PipelineData(target_dir=repo, repo="iryo"), no_spanner=True)
