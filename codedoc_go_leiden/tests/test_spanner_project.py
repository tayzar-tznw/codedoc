"""setup spanner without GOOGLE_CLOUD_PROJECT: resources are named after the
client library's default project (ADC / gcloud), never a hardcoded one."""

import types

import pytest

from graph_generator import setup_spanner_graph as s


class _Op:
    def result(self, timeout=None):
        return None


@pytest.mark.parametrize("given,expected", [(None, "adc-project"), ("my-project", "my-project")])
def test_create_instance_names_the_project_in_use(monkeypatch, given, expected):
    sent = {}

    def create_instance(**kw):
        sent.update(kw)
        return _Op()

    client = types.SimpleNamespace(
        project=given or "adc-project",
        instance=lambda iid: types.SimpleNamespace(exists=lambda: False),
        instance_admin_api=types.SimpleNamespace(create_instance=create_instance))
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: client)
    s.create_instance(given, "inst", "asia-northeast1")
    assert sent["parent"] == f"projects/{expected}"
    assert sent["instance"].name == f"projects/{expected}/instances/inst"
    assert sent["instance"].config == f"projects/{expected}/instanceConfigs/regional-asia-northeast1"
