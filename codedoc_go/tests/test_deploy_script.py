"""mcp_server/deploy.sh against a fake gcloud: the defaults are this variant's.

The fake `gcloud` and `git` on PATH record every call; no Google Cloud command
runs. The script reads `.env` from its working directory, so each test builds
a throwaway repository root holding only `mcp_server/Dockerfile` (the script's
"run me from the root" check) and, when the case needs one, a `.env`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "mcp_server" / "deploy.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_GCLOUD = '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GCLOUD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
if args[:2] == ["auth", "list"]:
    print("dev@example.com")
elif args[:3] == ["config", "get-value", "project"]:
    print("stub-project")
elif args[:3] in (["artifacts", "repositories", "describe"],
                  ["iam", "service-accounts", "describe"]):
    sys.exit(1)
elif args[:3] == ["run", "services", "describe"]:
    print("https://stub.a.run.app")
elif args[:2] == ["projects", "describe"]:
    print("123456789")
'''

FAKE_GIT = '''
import sys
if sys.argv[1:] == ["rev-parse", "--short", "HEAD"]:
    print("abc1234")
else:
    sys.exit(1)
'''


def _exe(path: Path, body: str) -> None:
    path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _deploy(tmp_path: Path, env_extra: dict[str, str] | None = None,
            dotenv: str | None = None) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    root = tmp_path / "repo"
    (root / "mcp_server").mkdir(parents=True)
    (root / "mcp_server" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    if dotenv is not None:
        (root / ".env").write_text(dotenv, encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _exe(fakebin / "gcloud", FAKE_GCLOUD)
    _exe(fakebin / "git", FAKE_GIT)
    log = tmp_path / "gcloud.log"
    env = {"PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
           "HOME": str(tmp_path), "GCLOUD_LOG": str(log)}
    env.update(env_extra or {})
    proc = subprocess.run(["bash", str(DEPLOY)], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)
    calls = ([json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
             if log.exists() else [])
    return proc, calls


def _call(calls: list[list[str]], *prefix: str) -> list[str]:
    for c in calls:
        if c[:len(prefix)] == list(prefix):
            return c
    raise AssertionError(f"no gcloud call starts with {prefix}: {calls}")


def _flag(call: list[str], name: str) -> str:
    return call[call.index(name) + 1]


def _service_env(call: list[str]) -> dict[str, str]:
    raw = _flag(call, "--set-env-vars")
    assert raw.startswith("^##^")
    return dict(item.split("=", 1) for item in raw[4:].split("##"))


def test_defaults_name_the_go_service_image_and_database(tmp_path):
    proc, calls = _deploy(tmp_path)
    assert proc.returncode == 0, proc.stderr
    image_repo = "asia-northeast1-docker.pkg.dev/stub-project/codedoc/codedoc-go-mcp"
    deploy = _call(calls, "beta", "run", "deploy")
    assert deploy[3] == "codedoc-go-mcp"
    assert _flag(deploy, "--image") == f"{image_repo}:abc1234"
    assert _flag(deploy, "--service-account") == \
        "codedoc-mcp-sa@stub-project.iam.gserviceaccount.com"
    assert _flag(deploy, "--functional-type") == "mcp-server"
    assert "--no-iap" in deploy and "--no-allow-unauthenticated" in deploy
    assert _service_env(deploy) == {
        "GOOGLE_CLOUD_PROJECT": "stub-project", "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "SPANNER_INSTANCE": "codedoc-instance", "SPANNER_DATABASE": "codedoc-go",
        "GRAPH_NAME": "code_graph_a"}
    build = _call(calls, "builds", "submit")
    assert _flag(build, "--substitutions") == f"_IMAGE={image_repo},SHORT_SHA=abc1234"
    assert _flag(build, "--config") == "mcp_server/cloudbuild.yaml"
    assert _flag(build, "--ignore-file") == "mcp_server/.gcloudignore"
    assert _call(calls, "artifacts", "repositories", "create")[3] == "codedoc"
    assert _call(calls, "iam", "service-accounts", "create")[3] == "codedoc-mcp-sa"
    assert _call(calls, "run", "services", "describe")[3] == "codedoc-go-mcp"
    assert "codedoc-go-mcp" in proc.stdout


@pytest.mark.parametrize("env_extra, dotenv, expected", [
    ({}, 'GOOGLE_CLOUD_PROJECT=dot-project\nSPANNER_DATABASE="dot-db"  # comment\n',
     {"GOOGLE_CLOUD_PROJECT": "dot-project", "SPANNER_DATABASE": "dot-db"}),
    ({"SPANNER_DATABASE": "env-db", "GRAPH_NAME": "g2"}, "SPANNER_DATABASE=dot-db\n",
     {"SPANNER_DATABASE": "env-db", "GRAPH_NAME": "g2"}),
    ({"GEMINI_MODEL": "m1"}, "export GOOGLE_CLOUD_LOCATION=global\r\n",
     {"GEMINI_MODEL": "m1", "GOOGLE_CLOUD_LOCATION": "global"}),
])
def test_root_dotenv_and_exported_values_reach_the_service(tmp_path, env_extra, dotenv, expected):
    proc, calls = _deploy(tmp_path, env_extra, dotenv)
    assert proc.returncode == 0, proc.stderr
    env = _service_env(_call(calls, "beta", "run", "deploy"))
    for key, value in expected.items():
        assert env[key] == value


@pytest.mark.parametrize("env_extra, present, absent", [
    ({"FUNCTIONAL_TYPE": ""}, ["--service-account", "--no-iap"], ["--functional-type"]),
    ({"IAP": "1"}, ["--iap", "--functional-type"], ["--no-iap"]),
    ({"IDENTITY_TYPE": "agent-identity"}, ["--identity-type"], ["--service-account"]),
])
def test_switches_change_the_deploy_command(tmp_path, env_extra, present, absent):
    proc, calls = _deploy(tmp_path, env_extra)
    assert proc.returncode == 0, proc.stderr
    deploy = next(c for c in calls if "deploy" in c[:3])
    assert ("beta" in deploy[:1]) == (env_extra.get("FUNCTIONAL_TYPE", "mcp-server") != "")
    for flag in present:
        assert flag in deploy
    for flag in absent:
        assert flag not in deploy


def test_iap_grants_the_iap_agent_invoker(tmp_path):
    proc, calls = _deploy(tmp_path, {"IAP": "1"})
    assert proc.returncode == 0, proc.stderr
    binding = _call(calls, "run", "services", "add-iam-policy-binding")
    assert binding[3] == "codedoc-go-mcp"
    assert _flag(binding, "--member") == \
        "serviceAccount:service-123456789@gcp-sa-iap.iam.gserviceaccount.com"


@pytest.mark.parametrize("env_extra, message", [
    ({"IAP": "2"}, "IAP must be 0 or 1"),
    ({"FUNCTIONAL_TYPE": "agent"}, "FUNCTIONAL_TYPE must be"),
    ({"IDENTITY_TYPE": "x"}, "IDENTITY_TYPE must be"),
])
def test_invalid_switches_stop_before_any_cloud_change(tmp_path, env_extra, message):
    proc, calls = _deploy(tmp_path, env_extra)
    assert proc.returncode == 1
    assert message in proc.stderr
    assert not [c for c in calls if c[:2] in (["services", "enable"], ["builds", "submit"])]


def test_upload_allow_list_and_build_config():
    ignore = (ROOT / "mcp_server" / ".gcloudignore").read_text(encoding="utf-8").splitlines()
    for line in ("*", "!mcp_server/**", "!graph_query_agent/**", "**/.env", "**/.env.*"):
        assert line in ignore
    build = (ROOT / "mcp_server" / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "codedoc-go-mcp" in build and "codedoc-mcp," not in build


def test_mcp_readme_states_this_variants_defaults():
    text = (ROOT / "mcp_server" / "README.md").read_text(encoding="utf-8")
    for fact in ("codedoc-go-mcp", "`codedoc-instance` / `codedoc-go` / `code_graph_a`",
                 "`graph_query_agent/config.py`", "there is no `graph_query_agent/.env`"):
        assert fact in text
    assert "codedoc-db" not in text and "development-459201" not in text
    assert not re.search(r"codedoc-mcp(?!-sa)", text)


def test_service_name_override_keeps_this_variants_image(tmp_path):
    proc, calls = _deploy(tmp_path, {"SERVICE_NAME": "my-mcp", "AR_REPO": "images"})
    assert proc.returncode == 0, proc.stderr
    deploy = _call(calls, "beta", "run", "deploy")
    assert deploy[3] == "my-mcp"
    assert _flag(deploy, "--image") == \
        "asia-northeast1-docker.pkg.dev/stub-project/images/codedoc-go-mcp:abc1234"


@pytest.mark.parametrize("rel", ["mcp_server/deploy.sh", "mcp_server/cloudbuild.yaml",
                                 "mcp_server/README.md", "mcp_server/server.py",
                                 "mcp_server/Dockerfile", "mcp_server/runner.py"])
def test_no_other_variants_names_in_the_mcp_server(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for stale in ("codedoc-python", "codedoc_python", "Python variant", "inheritance"):
        assert stale not in text, (rel, stale)
