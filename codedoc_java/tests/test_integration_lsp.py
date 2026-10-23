"""Integration: drive the REAL Eclipse JDT language server (jdtls) against the
built iryo-kanri-system corpus.

Requires jdtls (JAVA_LSP_CMD / on PATH / the pinned tools install) and the
built corpus (_feed/ populated by the per-project build.sh chain).
Run: .venv/bin/python -m pytest tests/test_integration_lsp.py -m integration
"""

import glob
import os
import shutil

import pytest

from graph_generator import config
from graph_generator.resolution import run_resolution

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO, "test_codes", "iryo-kanri-system")
_TOOLS_JDTLS = "/home/admin/tools/jdt-language-server/bin/jdtls"

pytestmark = pytest.mark.integration


def _jdtls_cmd() -> str | None:
    for cand in (os.environ.get("JAVA_LSP_CMD"), shutil.which("jdtls"), _TOOLS_JDTLS):
        if cand and (os.path.isfile(cand) or shutil.which(cand)):
            return cand
    return None


def _java_files(project: str) -> list[str]:
    return sorted(glob.glob(os.path.join(CORPUS, project, "src", "main", "java",
                                         "**", "*.java"), recursive=True))


@pytest.fixture(scope="module")
def corpus_resolution(tmp_path_factory):
    """One resolution run over two project roots: iryo-billing (in-project
    overload identity) and reception-desk (cross-jar boundary)."""
    if not os.path.isdir(os.path.join(CORPUS, "_feed")):
        pytest.skip("corpus not built (_feed missing)")
    cmd = _jdtls_cmd()
    if cmd is None:
        pytest.skip("jdtls not installed (set JAVA_LSP_CMD)")
    files = _java_files("iryo-billing") + _java_files("reception-desk")
    assert files, "corpus sources missing"
    prev_cmd, prev_conc = config.JAVA_LSP_CMD, config.JAVA_LSP_CONCURRENCY
    config.JAVA_LSP_CMD = cmd
    # one session at a time: both roots share the Maven local repository
    config.JAVA_LSP_CONCURRENCY = 1
    out = tmp_path_factory.mktemp("resolution_out")
    try:
        return run_resolution(CORPUS, files, str(out))
    finally:
        config.JAVA_LSP_CMD, config.JAVA_LSP_CONCURRENCY = prev_cmd, prev_conc


def _site(res, file_suffix, token):
    for s in res["sites"]:
        if s["token"] == token and s["file"].endswith(file_suffix):
            return s
    return None


def test_both_project_roots_load(corpus_resolution):
    res = corpus_resolution
    slns = {s["sln"]: s for s in res["solutions"]}
    assert set(slns) == {"iryo-billing/pom.xml", "reception-desk/pom.xml"}, slns
    for sln, entry in slns.items():
        assert entry["status"] == "resolved", (sln, entry["reason"])
    assert res["fallback_files"] == []
    assert res["version"] == 2


def test_billing_declarations_carry_exact_overload_identity(corpus_resolution):
    symbols = {d["symbol"] for d in corpus_resolution["declarations"]
               if d["artifact"] == "iryo-billing"}
    expected = {
        "iryo.billing.FeeCalculator.calculate(int, int)",
        "iryo.billing.FeeCalculator.calculate(long, long)",
        "iryo.billing.FeeCalculator.calculate(double, double)",
        "iryo.billing.FeeCalculator.calculate(int, int, int)",
        "iryo.billing.FeeCalculator.calculate(int...)",
        "iryo.billing.PointMath.toYen(int)",
    }
    missing = expected - symbols
    assert not missing, f"missing: {missing}"


def test_sites_have_no_fabricated_targets(corpus_resolution):
    """Zero-wrong invariant: every resolved in-repo target must exist in the
    declaration index — never an invented symbol."""
    declared = {(d["artifact"], d["symbol"]) for d in corpus_resolution["declarations"]}
    for s in corpus_resolution["sites"]:
        if s["status"] == "resolved" and (s["target"] or {}).get("file"):
            key = (s["target"]["artifact"], s["target"]["symbol"])
            assert key in declared, f"fabricated target: {key} at {s['file']}:{s['line']}"


def test_sites_carry_their_enclosing_artifact(corpus_resolution):
    artifacts = {s["enclosing_artifact"] for s in corpus_resolution["sites"]}
    assert artifacts == {"iryo-billing", "reception-desk"}, artifacts


def test_cross_jar_site_binds_into_a_feed_artifact(corpus_resolution):
    """reception-desk -> iryo-kernel jar: the jdt:// bridge must name the
    exact member and label it with the Maven artifactId."""
    site = _site(corpus_resolution, "DailyCloseFlow.java", "log")
    assert site is not None
    assert site["status"] in ("resolved", "external"), site
    assert site["target"]["artifact"] == "iryo-kernel", site
    assert site["target"]["symbol"] == "iryo.kernel.logging.Logger.log(String)", site


def test_cross_jar_site_maps_back_to_the_in_repo_declaration(corpus_resolution):
    """reception-desk -> iryo-billing jar, and iryo-billing IS in the analyzed
    tree: the GAV map must re-anchor the target onto the real source."""
    site = _site(corpus_resolution, "DailyCloseFlow.java", "toYen")
    assert site is not None
    assert site["status"] == "resolved", site
    assert site["target"]["artifact"] == "iryo-billing"
    assert site["target"]["symbol"] == "iryo.billing.PointMath.toYen(int)"
    assert site["target"]["file"].endswith(os.path.join("iryo", "billing", "PointMath.java"))
    assert site["target"]["line"] == 10
