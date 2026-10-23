"""Phase 1.6 engine: resolve Java call sites to exact symbols via the Eclipse
JDT language server (jdtls).

One server session per discovered project root (the topmost contiguous
``pom.xml`` chain — the Maven reactor root); artifact attribution is per file
via its NEAREST ancestor pom's ``<artifactId>``. Every extracted call site is
resolved through textDocument/definition (+ hover as an arity/type
cross-check); binary-boundary targets (jars, the JDK) come back as ``jdt://``
URIs and are bridged through the ``java/classFileContents`` request, parsed by
the same declaration-index path and mapped back to in-repo declarations by
Maven GAV. The resolver NEVER guesses: anything ambiguous degrades to
unresolved/external — that is what keeps wrong edges at zero.

Output contract (resolution.json v2, paths relative to target_dir):
  {version, target_dir, lsp:{enabled,cmd}, solutions:[{sln,status,reason,files}],
   fallback_files:[...], declarations:[{artifact,symbol,kind,file,line,
   container,namespace}], sites:[{file,line,col,token,site_kind,enclosing,
   enclosing_artifact,status,target,static_target,resolved_via,
   candidate_count}], ambiguous_symbols:[...], stats:{...}}

`sln` holds the project-root pom.xml; every `artifact` key carries a Maven
artifactId (in-repo projects), a jar-derived label, or a JDK module name —
the same identity the Spanner `artifact` columns and the graph node IDs use.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import unquote

from . import config
from .java_decls import (
    CallSite,
    Decl,
    DeclIndex,
    build_decl_index,
    extract_call_sites,
)
from .lsp_client import LspClient, LspConnectionError, LspError, LspTimeout

GUIDANCE_JA = """\
[Phase 1.6] Java セマンティック解決を有効にするには、対象コードが Eclipse JDT Language Server (jdtls) で解析できる必要があります:
  1. JDK 21 をインストール(java -version で確認。jdtls 1.44 以降は JRE 21 が必須)
  2. jdtls スナップショットを導入:
       curl -LO https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz
       mkdir -p ~/tools/jdt-language-server && tar xzf jdt-language-server-latest.tar.gz -C ~/tools/jdt-language-server
     PATH に無い場合は .env で JAVA_LSP_CMD=~/tools/jdt-language-server/bin/jdtls を指定
  3. 各プロジェクトに pom.xml を用意(Maven プロジェクトとして認識されます)
  4. 依存ライブラリを先にビルド: mvn install(または deploy)
     (未ビルドの依存があるとシンボル解決できません)
  5. pom.xml はソースツリー内に置いてください(各ファイルは最寄りの pom.xml の
     <artifactId> に帰属し、連続する親 pom の最上位が 1 セッションになります)
なお .env で JAVA_LSP_ENABLED=false にすると、この機能を無効化した
ノードのみモード(呼び出し/継承エッジなし)で実行できます。"""

# jdtls ≥1.44 needs a JRE 21 runtime; the system default JDK may be older, so
# the launcher env pins it (the wrapper reads JAVA_HOME).
_JDK21_HOME = os.environ.get("JAVA21_HOME", "/usr/lib/jvm/java-21-openjdk-amd64")

_STATUS_NOTIFICATION = "language/status"
_POM = "pom.xml"

# micro-DI container type name; a registration is only trusted when the
# add*() invocation itself binds to a container with this simple name
# (corpus: iryo.kernel.ServiceCollection in artifact iryo-kernel).
_DI_CONTAINER_SIMPLE = "ServiceCollection"
_DI_ROLES = ("service", "impl")


# ---------------------------------------------------------------------
# project discovery (reactor root session + nearest-pom artifact identity)
# ---------------------------------------------------------------------


@dataclass
class Project:
    pom: str                    # abs path to the project-root (reactor) pom.xml
    dir: str                    # abs project-root directory
    artifact: str               # reactor-root artifactId (session label)
    files: list[str] = field(default_factory=list)   # abs .java paths


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _pom_gav(pom_path: str) -> tuple[str, str, str]:
    """(groupId, artifactId, version) of one pom; groupId/version may be
    inherited from <parent>, artifactId falls back to the directory name."""
    direct: dict[str, str] = {}
    parent: dict[str, str] = {}
    try:
        root = ET.parse(pom_path).getroot()
    except (ET.ParseError, OSError):
        root = None
    if root is not None:
        for el in root:
            tag = _local_tag(el.tag)
            if tag == "parent":
                for p in el:
                    parent.setdefault(_local_tag(p.tag), (p.text or "").strip())
            else:
                direct.setdefault(tag, (el.text or "").strip())
    artifact = direct.get("artifactId") or os.path.basename(os.path.dirname(pom_path))
    group = direct.get("groupId") or parent.get("groupId") or artifact
    version = direct.get("version") or parent.get("version") or ""
    return group, artifact, version


@dataclass
class Discovery:
    projects: list[Project]
    fallback_files: list[str]
    artifact_of_file: dict[str, str]
    gav_map: dict[tuple[str, str, str], str]        # GAV -> artifactId


def discover_projects(target_dir: str, java_files: list[str]) -> Discovery:
    """Map every .java file to its nearest ancestor pom (artifact identity) and
    to the topmost contiguous ancestor pom (jdtls session root)."""
    target_dir = os.path.abspath(target_dir)
    pom_dirs: set[str] = set()
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in config.SKIP_DIRS]
        if _POM in files:
            pom_dirs.add(root)

    gav_cache: dict[str, tuple[str, str, str]] = {}

    def gav(pom_dir: str) -> tuple[str, str, str]:
        path = os.path.join(pom_dir, _POM)
        if path not in gav_cache:
            gav_cache[path] = _pom_gav(path)
        return gav_cache[path]

    def nearest_pom_dir(path: str) -> str | None:
        cur = os.path.dirname(os.path.abspath(path))
        while True:
            if cur in pom_dirs:
                return cur
            if cur == target_dir:
                return None
            parent = os.path.dirname(cur)
            if parent == cur:
                return None
            cur = parent

    def reactor_root(pom_dir: str) -> str:
        cur = pom_dir
        while True:
            parent = os.path.dirname(cur)
            if parent == cur or parent not in pom_dirs:
                return cur
            cur = parent

    projects: dict[str, Project] = {}
    artifact_of_file: dict[str, str] = {}
    fallback: list[str] = []
    for fp in java_files:
        fp = os.path.abspath(fp)
        own = nearest_pom_dir(fp)
        if own is None:
            fallback.append(fp)
            continue
        artifact_of_file[fp] = gav(own)[1]
        root_dir = reactor_root(own)
        pom = os.path.join(root_dir, _POM)
        proj = projects.get(pom)
        if proj is None:
            proj = Project(pom=pom, dir=root_dir, artifact=gav(root_dir)[1])
            projects[pom] = proj
        proj.files.append(fp)

    gav_map: dict[tuple[str, str, str], str] = {}
    for d in sorted(pom_dirs):
        g, a, v = gav(d)
        if a:
            gav_map[(g, a, v)] = a

    return Discovery(
        projects=sorted(projects.values(), key=lambda p: p.pom),
        fallback_files=sorted(fallback),
        artifact_of_file=artifact_of_file,
        gav_map=gav_map,
    )


# ---------------------------------------------------------------------
# LSP session per project root
# ---------------------------------------------------------------------


def _uri(path: str) -> str:
    return "file://" + path


def _uri_to_path(uri: str) -> str:
    return uri[len("file://"):] if uri.startswith("file://") else uri


_INIT_CAPABILITIES = {
    "workspace": {"configuration": True, "workspaceFolders": True},
    "window": {"workDoneProgress": True},
    "textDocument": {
        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
        "definition": {},
        "hover": {"contentFormat": ["markdown", "plaintext"]},
    },
}

# m2e reads each pom's <repositories>; userSettings defaults to None so the
# user's own ~/.m2/settings.xml (if any) applies unchanged. Repos that keep an
# isolated local repository (e.g. the corpus _m2) can point m2e at their own
# settings.xml via JAVA_LSP_MAVEN_SETTINGS.
def _java_settings() -> dict:
    return {
        "maven": {"downloadSources": True},
        "import": {"gradle": {"enabled": False}},
        "configuration": {"maven": {"userSettings": config.JAVA_LSP_MAVEN_SETTINGS or None}},
    }


def _init_options() -> dict:
    return {
        "extendedClientCapabilities": {"classFileContentsSupport": True},
        "settings": {"java": _java_settings()},
    }


def _is_service_ready(params) -> bool:
    """jdtls emits many language/status events; only ServiceReady counts."""
    return isinstance(params, dict) and params.get("type") == "ServiceReady"


class ProjectSession:
    def __init__(self, project: Project, client_factory=None):
        self.project = project
        factory = client_factory or LspClient
        # unique -data AND -configuration per session: the shipped wrapper's
        # cascaded configuration area is read-only/shared and two sessions
        # sharing it deadlock (spike ①)
        self._workspace = tempfile.mkdtemp(prefix="codedoc-jdtls-")
        self.config_dir = os.path.join(self._workspace, "configuration")
        self.data_dir = os.path.join(self._workspace, "data")
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)
        env = dict(os.environ)
        env["JAVA_HOME"] = _JDK21_HOME
        self.client = factory(
            [config.JAVA_LSP_CMD,
             "-configuration", self.config_dir,
             "-data", self.data_dir,
             f"--jvm-arg=-Dosgi.configuration.area={self.config_dir}",
             "--jvm-arg=-Xms128m",
             "--jvm-arg=-Xmx700m"],
            cwd=project.dir,
            config_sections={"java": _java_settings()},
            default_timeout=60.0,
            watch_notifications={_STATUS_NOTIFICATION},
            workspace_folders=[project.dir],
            env=env,
        )
        self._opened: set[str] = set()
        self._hover_cache: dict[tuple[str, int, int], str] = {}
        self._class_file_cache: dict[str, str] = {}

    def _service_ready_seen(self) -> bool:
        counter = getattr(self.client, "notification_count", None)
        if counter is None:
            return False
        try:
            return counter(_STATUS_NOTIFICATION, _is_service_ready) > 0
        except TypeError:      # duck-typed clients without the predicate arg
            return False

    async def start_and_wait_ready(self):
        await self.client.start()
        init_params = {
            "processId": os.getpid(),
            "rootUri": _uri(self.project.dir),
            "workspaceFolders": [{"uri": _uri(self.project.dir),
                                  "name": os.path.basename(self.project.dir)}],
            "capabilities": _INIT_CAPABILITIES,
            "initializationOptions": _init_options(),
        }
        await self.client.request("initialize", init_params, timeout=120.0)
        await self.client.notify("initialized", {})
        try:
            await self.client.wait_notification(
                _STATUS_NOTIFICATION, predicate=_is_service_ready,
                timeout=min(config.JAVA_LSP_TIMEOUT_S, 180))
        except LspTimeout:
            pass  # fall through to the documentSymbol probe
        # probe the LARGEST file: walk order can put a symbol-less file first
        probe = max(self.project.files,
                    key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
        await self.open_file(probe)
        deadline = asyncio.get_event_loop().time() + config.JAVA_LSP_TIMEOUT_S
        while True:
            try:
                syms = await self.client.request(
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": _uri(probe)}}, timeout=15.0)
                if syms:
                    return
                if self._service_ready_seen():
                    # the server says the workspace is up; an empty symbol list
                    # is a legitimate answer, not "not ready"
                    return
            except (LspError, LspTimeout):
                pass
            if asyncio.get_event_loop().time() > deadline:
                raise LspTimeout(f"project not ready within {config.JAVA_LSP_TIMEOUT_S}s")
            await asyncio.sleep(2.0)

    async def open_file(self, path: str):
        if path in self._opened:
            return
        with open(path, encoding="utf-8") as f:
            text = f.read()
        await self.client.notify("textDocument/didOpen", {
            "textDocument": {"uri": _uri(path), "languageId": "java",
                             "version": 1, "text": text}})
        self._opened.add(path)

    async def definition(self, path: str, line0: int, col16: int):
        return _as_locations(await self.client.request(
            "textDocument/definition",
            {"textDocument": {"uri": _uri(path)},
             "position": {"line": line0, "character": col16}}, timeout=30.0))

    async def hover_text(self, path: str, line0: int, col16: int) -> str:
        try:
            hv = await self.client.request(
                "textDocument/hover",
                {"textDocument": {"uri": _uri(path)},
                 "position": {"line": line0, "character": col16}})
        except (LspError, LspTimeout):
            return ""
        return _hover_signature(hv)

    async def class_file_contents(self, uri: str) -> str:
        """jdtls custom request: decompiled (or attached-source) text of a
        binary target. The jdt:// URI is opaque — pass it back VERBATIM."""
        if uri in self._class_file_cache:
            return self._class_file_cache[uri]
        try:
            src = await self.client.request(
                "java/classFileContents", {"uri": uri}, timeout=30.0)
        except (LspError, LspTimeout):
            src = None
        text = src if isinstance(src, str) else ""
        self._class_file_cache[uri] = text
        return text

    async def stop(self):
        try:
            await self.client.stop()
        finally:
            shutil.rmtree(self._workspace, ignore_errors=True)


# ---------------------------------------------------------------------
# jdt:// URI contract (frozen from the S2 spike fixtures)
# ---------------------------------------------------------------------


_GAV_GROUP_RE = re.compile(r"=/maven\.groupId=/([^=/]+)=/")
_GAV_ARTIFACT_RE = re.compile(r"=/maven\.artifactId=/([^=/]+)=/")
_GAV_VERSION_RE = re.compile(r"=/maven\.version=/([^=/]+)=/")
_JAR_VERSION_RE = re.compile(r"^(?P<name>.+?)-\d[0-9A-Za-z.\-_]*\.jar$")


def _jdt_uri_parts(uri: str) -> tuple[str, str, str]:
    """(container, package, class simple name) of a jdt:// URI:
    jdt://contents/mini-lib-1.0.0.jar/jp.iryo.spike.lib/Calculations.java?=…
      -> ('mini-lib-1.0.0.jar', 'jp.iryo.spike.lib', 'Calculations')"""
    body = uri.split("?", 1)[0]
    for prefix in ("jdt://contents/", "jdt:/contents/"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    else:
        body = body.split("://", 1)[-1]
    segs = [unquote(s) for s in body.split("/") if s]
    if not segs:
        return "", "", ""
    container = segs[0]
    cls = segs[-1] if len(segs) > 1 else ""
    for ext in (".java", ".class"):
        if cls.endswith(ext):
            cls = cls[: -len(ext)]
    package = segs[-2] if len(segs) > 2 else ""
    return container, package, cls


def _uri_type_name(uri: str) -> str:
    """Fully-qualified type embedded in a jdt:// URI."""
    _, package, cls = _jdt_uri_parts(uri)
    if not cls:
        return ""
    return f"{package}.{cls}" if package else cls


def _uri_gav(uri: str) -> tuple[str, str, str] | None:
    g = _GAV_GROUP_RE.search(uri)
    a = _GAV_ARTIFACT_RE.search(uri)
    v = _GAV_VERSION_RE.search(uri)
    if not (g and a and v):
        return None
    return g.group(1), a.group(1), v.group(1)


def _jar_label(container: str) -> str:
    """Container basename with a parseable version suffix removed."""
    m = _JAR_VERSION_RE.match(container)
    if m:
        return m.group("name")
    return container[:-len(".jar")] if container.endswith(".jar") else container


def _artifact_from_jdt_uri(uri: str, gav_map: dict[tuple[str, str, str], str]) -> str:
    """FROZEN label contract (SPIKE_NOTES): in-repo GAV -> artifactId; unmapped
    jar -> basename sans version; JDK/JRT -> module segment verbatim."""
    gav = _uri_gav(uri)
    if gav is not None:
        return gav_map.get(gav, gav[1])
    container, _, _ = _jdt_uri_parts(uri)
    if container and ".jar" not in container:
        return container                       # java.base and friends
    return _jar_label(container)


def _as_locations(result) -> list[dict]:
    if not result:
        return []
    locs = result if isinstance(result, list) else [result]
    out = []
    for l in locs:
        if not isinstance(l, dict):
            continue
        uri = l.get("uri") or l.get("targetUri") or ""
        rng = l.get("range") or l.get("targetSelectionRange") or l.get("targetRange") or {}
        if uri:
            out.append({"uri": uri, "range": rng})
    return out


# ---------------------------------------------------------------------
# hover signature parsing (cross-check ONLY — symbols never come from hover)
# ---------------------------------------------------------------------


def _hover_signature(hv) -> str:
    """jdtls answers legacy MarkedString[]: the FIRST {language:"java"} block
    is the signature; javadoc/source links follow as plain strings."""
    if not hv:
        return ""
    contents = hv.get("contents") if isinstance(hv, dict) else hv
    if isinstance(contents, list):
        for c in contents:
            if isinstance(c, dict) and c.get("language") == "java":
                return (c.get("value") or "").strip()
        for c in contents:
            if isinstance(c, str) and c.strip():
                return _hover_code_line(c)
        return ""
    if isinstance(contents, dict):                     # MarkupContent (defensive)
        return _hover_code_line(contents.get("value", "") or "")
    return _hover_code_line(contents or "")


def _hover_code_line(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            for j in range(i + 1, len(lines)):
                if lines[j].strip().startswith("```"):
                    break
                if lines[j].strip():
                    return lines[j].strip()
            return ""
    return lines[0].strip() if lines else ""


_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_FQN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")
_QUALIFIED_RE = re.compile(
    r"\b(?:[A-Za-z_$][A-Za-z0-9_$]*\s*\.\s*)+([A-Za-z_$][A-Za-z0-9_$]*)")


def _split_top_level(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _last_top_level_space(text: str) -> int:
    depth, found = 0, -1
    for i, ch in enumerate(text):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == " " and depth == 0:
            found = i
    return found


def _canon_type(t: str) -> str:
    """Whitespace-canonical spelling so declaration-parsed and hover-parsed
    types compare equal (`Map<String,Integer>` vs `Map<String, Integer>`)."""
    t = re.sub(r"\s+", " ", t.strip())
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"\s*<\s*", "<", t)
    t = re.sub(r"\s*>", ">", t)
    t = re.sub(r"\s*\[\s*\]", "[]", t)
    return t


def _parse_hover_signature(hover: str) -> tuple[str, str, list[str]] | None:
    """('Calculations', 'calculate', ['long a', 'long b']) from
    'long jp.iryo.spike.lib.Calculations.calculate(long a, long b)'.
    Constructor hovers are a bare type FQN (no parens) -> None."""
    code = _hover_code_line(hover)
    if not code or "(" not in code:
        return None
    head, _, rest = code.partition("(")
    body = rest.rsplit(")", 1)[0] if ")" in rest else rest
    name_part = head.strip()
    tokens = name_part.split()
    last = tokens[-1] if tokens else name_part
    segs = last.replace("$", ".").split(".")
    member = segs[-1]
    container = segs[-2] if len(segs) >= 2 else ""
    if "<" in container:
        container = container[:container.index("<")]
    if "<" in member:
        member = member[:member.index("<")]
    return container, member, _split_top_level(body)


def _hover_type_fqn(hover: str) -> str:
    """Constructor hovers carry only the type: 'jp.iryo.spike.lib.Rec'."""
    code = _hover_code_line(hover)
    if code and _FQN_RE.fullmatch(code):
        return code
    return ""


def _hover_member_and_params(hover: str) -> tuple[str, list[str]] | None:
    parsed = _parse_hover_signature(hover)
    return (parsed[1], parsed[2]) if parsed else None


def _norm_hover_param(p: str) -> str:
    """'java.lang.String value' -> 'String'; 'int... values' -> 'int...';
    'Map<String, Integer> m' -> 'Map<String, Integer>' — hover text normalized
    to the same spelling as declaration-parsed parameter types."""
    p = p.strip()
    if not p:
        return ""
    p = re.sub(r"^final\s+", "", p)
    cut = _last_top_level_space(p)
    if cut > 0:
        tail = p[cut + 1:].strip()
        if _IDENT_RE.fullmatch(tail):
            p = p[:cut].strip()
    p = _QUALIFIED_RE.sub(r"\1", p)
    return _canon_type(p)


def _simple_type_name(symbol: str) -> str:
    gi = symbol.find("<")
    head = symbol if gi == -1 else symbol[:gi]
    cut = max(head.rfind("."), head.rfind("$"))
    return symbol[cut + 1:] if cut != -1 else symbol


def _symbol_member_name(symbol: str) -> str:
    op = symbol.rfind("(")
    head = symbol[:op] if op != -1 else symbol
    if head.endswith("<init>"):
        return "<init>"
    tail = head.replace("$", ".").rsplit(".", 1)[-1]
    if tail.endswith(">") and "<" in tail:
        tail = tail[:tail.index("<")]
    return tail


def _symbol_container(symbol: str) -> str:
    head = symbol.split("(")[0]
    if head.endswith(".<init>"):
        return head[: -len(".<init>")]
    cut = head.rfind(".")
    return head[:cut] if cut != -1 else ""


def _symbol_param_count(symbol: str) -> int | None:
    op = symbol.rfind("(")
    if op == -1:
        return None
    cp = symbol.rfind(")")
    inner = symbol[op + 1:cp] if cp > op else ""
    return len(_split_top_level(inner))


def _hover_agrees(cand: "_Candidate", hover_name: str, hover_count: int) -> bool:
    if _symbol_member_name(cand.symbol) != hover_name:
        return False
    sym_count = _symbol_param_count(cand.symbol)
    return sym_count is None or hover_count == sym_count


# ---------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------


class _Workspace:
    """Cross-project registries shared by all sessions."""

    def __init__(self, target_dir: str, discovery: Discovery):
        self.target_dir = target_dir
        self.projects = discovery.projects
        self.gav_map = discovery.gav_map
        self.artifact_of_file = dict(discovery.artifact_of_file)
        self.indexes: dict[str, DeclIndex] = {}          # artifact -> index
        self.project_of_file: dict[str, Project] = {}
        self.artifacts: dict[str, Project] = {}          # artifact -> session
        sources: dict[str, dict[str, str]] = {}
        for p in self.projects:
            for fp in p.files:
                with open(fp, encoding="utf-8") as f:
                    text = f.read()
                artifact = self.artifact_of_file.get(fp) or p.artifact
                sources.setdefault(artifact, {})[fp] = text
                self.project_of_file[fp] = p
                self.artifacts.setdefault(artifact, p)
        for artifact, files in sources.items():
            self.indexes[artifact] = build_decl_index(files, artifact=artifact)

        self.registry: dict[tuple[str, str], Decl] = {}
        # identical member fq-symbols declared in DIFFERENT files under one
        # artifact label — their locations are untrustworthy
        self.dup_members: set[tuple[str, str]] = set()
        first_file: dict[tuple[str, str], str] = {}
        for artifact, idx in self.indexes.items():
            for d in idx.all_decls():
                key = (artifact, d.symbol)
                self.registry.setdefault(key, d)
                if d.kind != "type":
                    prev = first_file.get(key)
                    if prev is not None and prev != d.file:
                        self.dup_members.add(key)
                    first_file.setdefault(key, d.file)
        self._decompiled_cache: dict[tuple[str, str], DeclIndex] = {}

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.target_dir)

    def artifact_of(self, path: str) -> str | None:
        art = self.artifact_of_file.get(path)
        if art is not None:
            return art
        proj = self.project_of_file.get(path)
        return proj.artifact if proj is not None else None

    def is_in_repo(self, artifact: str) -> bool:
        return artifact in self.indexes

    def target_of(self, artifact: str, symbol: str) -> dict:
        d = self.registry.get((artifact, symbol))
        if d is None or (artifact, symbol) in self.dup_members:
            return {"symbol": symbol, "artifact": artifact,
                    "kind": d.kind if d else "method", "file": None, "line": None}
        return {
            "symbol": symbol, "artifact": artifact,
            "kind": d.kind,
            "file": self.rel(d.file),
            "line": d.line,
        }

    def decompiled_index(self, artifact: str, type_fqn: str, source: str) -> DeclIndex:
        key = (artifact, type_fqn)
        idx = self._decompiled_cache.get(key)
        if idx is None:
            idx = build_decl_index({_decompiled_path(artifact, type_fqn): source},
                                   artifact=artifact)
            self._decompiled_cache[key] = idx
        return idx


def _decompiled_path(artifact: str, type_fqn: str) -> str:
    return f"/jdt/{artifact}/{type_fqn}.java"


@dataclass
class _Candidate:
    symbol: str
    kind: str
    artifact: str
    in_repo: bool
    params: list[str] = field(default_factory=list)
    siblings: list["_Candidate"] = field(default_factory=list)


def _cand_from_decl(d: Decl, artifact: str, in_repo: bool, idx: DeclIndex | None) -> _Candidate:
    siblings: list[_Candidate] = []
    if idx is not None and d.container and d.kind != "type":
        name = _symbol_member_name(d.symbol)
        for s in idx.members_of(d.container):
            if s.symbol != d.symbol and _symbol_member_name(s.symbol) == name:
                siblings.append(_Candidate(s.symbol, s.kind, artifact, in_repo, list(s.params)))
    return _Candidate(d.symbol, d.kind, artifact, in_repo, list(d.params), siblings)


def _is_ctor_chain(site: CallSite) -> bool:
    """`this(...)` / `super(...)` bind to a constructor, and their hover is the
    bare type FQN — they skip the invocation hover crosscheck."""
    return site.site_kind == "invocation" and site.token in ("this", "super")


def _is_ctor_site(site: CallSite) -> bool:
    return (site.site_kind == "object_creation"
            or _is_ctor_chain(site)
            or (site.site_kind == "method_reference" and site.token == "new"))


async def _resolve_location(sess: ProjectSession, ws: _Workspace, site: CallSite,
                            loc: dict) -> tuple[list[_Candidate], dict | None]:
    """Returns (candidates, external_target)."""
    uri = loc["uri"]
    rng = loc.get("range") or {}
    start = rng.get("start") or {"line": 0, "character": 0}

    if uri.startswith("jdt:"):
        artifact = _artifact_from_jdt_uri(uri, ws.gav_map)
        in_repo = ws.is_in_repo(artifact)
        hover = await _site_hover(sess, site)
        source = await sess.class_file_contents(uri)   # URI passed back verbatim
        type_fqn = _uri_type_name(uri)
        cands: list[_Candidate] = []
        if source:
            idx = ws.decompiled_index(artifact, type_fqn, source)
            cands = _candidates_from_external_index(
                ws, idx, _decompiled_path(artifact, type_fqn), artifact, site, start, hover)
        if in_repo:
            # GAV maps to an in-repo artifact: re-anchor onto the real
            # declaration so file/line come from the source tree
            mapped = []
            for c in cands:
                d = ws.registry.get((artifact, c.symbol))
                if d is not None:
                    mapped.append(_cand_from_decl(d, artifact, True, ws.indexes.get(artifact)))
            return mapped, None
        if cands:
            c = cands[0]
            return [], {"symbol": c.symbol, "artifact": artifact, "kind": c.kind,
                        "file": None, "line": None}
        if type_fqn:
            return [], {"symbol": type_fqn, "artifact": artifact, "kind": "type",
                        "file": None, "line": None}
        return [], None

    path = _uri_to_path(uri)
    artifact = ws.artifact_of(path)
    if artifact is None or artifact not in ws.indexes:
        return [], None      # outside the analyzed source tree — never guess
    idx = ws.indexes[artifact]
    decl = idx.decl_at(path, start.get("line", 0), start.get("character", 0))
    if decl is None:
        return [], None
    return [_cand_from_decl(decl, artifact, True, idx)], None


def _candidates_from_external_index(ws: _Workspace, idx: DeclIndex, syn_path: str,
                                    artifact: str, site: CallSite, start: dict,
                                    hover: str) -> list[_Candidate]:
    in_repo = ws.is_in_repo(artifact)
    decl = idx.decl_at(syn_path, start.get("line", 0), start.get("character", 0))
    if decl is not None and decl.kind != "type":
        return [_cand_from_decl(decl, artifact, in_repo, idx)]

    types = [d for d in idx.all_decls() if d.kind == "type"]
    if not types:
        return []
    ttype = decl if (decl is not None and decl.kind == "type") else types[0]

    # degenerate range (spike ⑨: record ctor/accessor answer 0,0) or a range
    # that landed on the type — choose by site kind + hover
    if _is_ctor_site(site):
        fqn = _hover_type_fqn(hover)
        if fqn and _simple_type_name(fqn) != _simple_type_name(ttype.symbol):
            return []
        ctors = idx.ctors_of(ttype.symbol)
        if not ctors:
            return [_Candidate(ttype.symbol, "type", artifact, in_repo)]
        if site.arg_count is None:
            return []
        # written arg count can lie when a varargs or longer constructor could
        # also bind — refuse those instead of guessing
        risky = any(p.endswith("...") for c in ctors for p in c.params) or \
            any(len(c.params) > site.arg_count for c in ctors)
        by_argc = [c for c in ctors if len(c.params) == site.arg_count]
        if not risky and len(by_argc) == 1:
            return [_cand_from_decl(by_argc[0], artifact, in_repo, idx)]
        return []

    if site.site_kind == "class_literal":
        return [_Candidate(ttype.symbol, "type", artifact, in_repo)]

    hv = _hover_member_and_params(hover)
    if hv is None:
        if decl is not None:
            return [_Candidate(decl.symbol, "type", artifact, in_repo)]
        return []
    name, params = hv
    members = [d for d in idx.all_decls()
               if d.kind != "type" and _symbol_member_name(d.symbol) == name]
    want_types = [_norm_hover_param(p) for p in params]
    typed = [m for m in members
             if [_canon_type(p) for p in m.params] == [_canon_type(t) for t in want_types]]
    if len(typed) == 1:
        return [_cand_from_decl(typed[0], artifact, in_repo, idx)]
    exact = [m for m in members if len(m.params) == len(params)]
    if len(exact) == 1:
        return [_cand_from_decl(exact[0], artifact, in_repo, idx)]
    return []


async def _site_hover(sess: ProjectSession, site: CallSite) -> str:
    key = (site.file, site.line, site.col)
    if key not in sess._hover_cache:
        sess._hover_cache[key] = await sess.hover_text(site.file, site.line - 1, site.col)
    return sess._hover_cache[key]


def _typed_matches(pool: list[_Candidate], name: str, types: list[str]) -> list[_Candidate]:
    """Candidates whose declared param types match the hover types exactly."""
    want = [_canon_type(t) for t in types]
    out: list[_Candidate] = []
    seen: set[tuple[str, str]] = set()
    for c in pool:
        if c.kind == "type" or _symbol_member_name(c.symbol) != name:
            continue
        if [_canon_type(p) for p in c.params] != want:
            continue
        if (c.artifact, c.symbol) not in seen:
            seen.add((c.artifact, c.symbol))
            out.append(c)
    return out


def _select_by_hover(candidates: list[_Candidate], name: str,
                     types: list[str]) -> _Candidate | None:
    pool = list(candidates)
    for c in candidates:
        pool.extend(c.siblings)
    typed = _typed_matches(pool, name, types)
    return typed[0] if len(typed) == 1 else None


def _hover_rescue(ws: _Workspace, container: str, member: str,
                  types: list[str]) -> _Candidate | None:
    """Definition failed or lied, but hover names the bound member. Accept a
    GLOBALLY UNIQUE (container, member, param types) match from the in-repo
    registry — the uniqueness gate is what preserves zero-wrong."""
    if not member or not container:
        return None
    want = [_canon_type(t) for t in types]
    matches: list[tuple[str, Decl]] = []
    for (artifact, sym), d in ws.registry.items():
        if d.kind == "type" or _symbol_member_name(sym) != member:
            continue
        if (artifact, sym) in ws.dup_members:
            continue
        if [_canon_type(p) for p in d.params] != want:
            continue
        if d.container and _simple_type_name(d.container).split("<")[0] == container:
            matches.append((artifact, d))
    uniq = {(a, d.symbol): (a, d) for a, d in matches}
    if len(uniq) != 1:
        return None
    artifact, d = next(iter(uniq.values()))
    return _cand_from_decl(d, artifact, True, ws.indexes.get(artifact))


async def _resolve_site(sess: ProjectSession, ws: _Workspace, site: CallSite) -> dict:
    artifact = ws.artifact_of(site.file)
    entry: dict = {
        "file": ws.rel(site.file), "line": site.line, "col": site.col,
        "token": site.token, "site_kind": site.site_kind,
        "enclosing": None, "enclosing_artifact": artifact,
        "status": "unresolved", "target": None,
        "static_target": None, "resolved_via": None, "candidate_count": None,
    }
    if artifact in ws.indexes:
        enc = ws.indexes[artifact].enclosing_member(site.file, site.line - 1, site.col)
        if enc is not None:
            entry["enclosing"] = enc.symbol

    had_def_error = False
    try:
        locs = await sess.definition(site.file, site.line - 1, site.col)
    except (LspError, LspTimeout):
        # per-request server errors degrade the SITE, never the project
        had_def_error = True
        locs = []

    # the jdt:// class-name segment is independent evidence for a hover rescue
    uri_types = {_jdt_uri_parts(l["uri"])[2] for l in locs if l["uri"].startswith("jdt:")}
    uri_types.discard("")

    cands: list[_Candidate] = []
    external = None
    seen: set[tuple[str, str]] = set()
    for loc in locs:
        cs, e = await _resolve_location(sess, ws, site, loc)
        external = external or e
        for c in cs:
            key = (c.artifact, c.symbol)
            if key not in seen:
                seen.add(key)
                cands.append(c)

    hover_sig = None
    hover_types: list[str] = []
    crosscheck = site.site_kind == "invocation" and not _is_ctor_chain(site)
    if crosscheck:
        hover_sig = _parse_hover_signature(await _site_hover(sess, site))
        if hover_sig is not None:
            hover_types = [_norm_hover_param(p) for p in hover_sig[2]]

    def _corroborated(c: _Candidate) -> bool:
        """A rescued candidate needs independent evidence: either the server
        ERRORED (the binding exists, the request just blew up), or a jdt:// URI
        names the very type the candidate belongs to. A clean null definition
        gets no rescue — the true target may be external."""
        if uri_types:
            d = ws.registry.get((c.artifact, c.symbol))
            if d is None:
                return False
            return _simple_type_name(d.container).split("<")[0] in uri_types
        return had_def_error

    if not cands:
        if hover_sig is not None and (had_def_error or uri_types):
            rescued = _hover_rescue(ws, hover_sig[0], hover_sig[1], hover_types)
            if rescued is not None and _corroborated(rescued):
                cands = [rescued]
        if not cands:
            if external is not None:
                entry["status"] = "external"
                entry["target"] = external
                return entry
            entry["status"] = "unresolved"
            return entry

    cand: _Candidate | None = None
    if crosscheck:
        if hover_sig is None:
            entry["status"] = "unresolved"
            return entry
        name = hover_sig[1]
        cand = _select_by_hover(cands, name, hover_types)
        if cand is None:
            if len(cands) == 1 and _hover_agrees(cands[0], name, len(hover_types)):
                cand = cands[0]
            else:
                cand = _hover_rescue(ws, hover_sig[0], name, hover_types)
                if cand is not None and not _corroborated(cand):
                    cand = None
        if cand is None:
            if len(cands) > 1:
                entry["status"] = "ambiguous"
                entry["candidate_count"] = len(cands)
            else:
                entry["status"] = "unresolved"
            return entry
    else:
        if len(cands) > 1:
            entry["status"] = "ambiguous"
            entry["candidate_count"] = len(cands)
            return entry
        cand = cands[0]

    if not cand.in_repo:
        entry["status"] = "external"
        entry["target"] = {"symbol": cand.symbol, "artifact": cand.artifact,
                           "kind": cand.kind, "file": None, "line": None}
        return entry

    entry["status"] = "resolved"
    entry["resolved_via"] = "lsp"
    entry["target"] = ws.target_of(cand.artifact, cand.symbol)
    entry["target"]["kind"] = cand.kind if entry["target"]["file"] is None else entry["target"]["kind"]
    return entry


# ---------------------------------------------------------------------
# interface-member → unique implementation upgrade
# ---------------------------------------------------------------------


def _apply_interface_impl_upgrade(ws: _Workspace, sites: list[dict]):
    """Abstract interface-member targets with exactly ONE in-repo implementing
    class upgrade to that implementation; the interface member is preserved as
    static_target. `default`/`static`/`private` interface methods have bodies —
    they ARE the dispatch target and are never upgraded."""
    cache: dict[tuple[str, str], list[tuple[str, str]]] = {}

    # interfaces are matched by simple name (base lists carry source spellings)
    # — refuse the upgrade entirely when two interfaces share a simple name
    iface_simple_symbols: dict[str, set[tuple[str, str]]] = {}
    for (artifact, sym), d in ws.registry.items():
        if d.kind == "type" and d.type_kind == "interface":
            iface_simple_symbols.setdefault(
                _simple_type_name(sym).split("<")[0], set()).add((artifact, sym))

    def impls_of(iface_container: str, member_tail: str) -> list[tuple[str, str]]:
        key = (iface_container, member_tail)
        if key in cache:
            return cache[key]
        iface_simple = _simple_type_name(iface_container).split("<")[0]
        found: list[tuple[str, str]] = []
        for (artifact, sym), d in ws.registry.items():
            if d.kind == "type" or not d.container or not sym.startswith(d.container):
                continue
            tail = sym[len(d.container):].lstrip(".")
            if tail != member_tail or d.container == iface_container:
                continue
            td = ws.registry.get((artifact, d.container))
            if td is None or td.kind != "type" or td.type_kind == "interface":
                continue
            if any(bt.split("<")[0].rsplit(".", 1)[-1] == iface_simple
                   for bt in (td.super_interfaces or td.base_types)):
                found.append((artifact, sym))
        cache[key] = found
        return found

    for entry in sites:
        if entry["status"] != "resolved" or entry.get("resolved_via") != "lsp":
            continue
        if entry.get("static_target"):
            continue
        tgt = entry["target"]
        d = ws.registry.get((tgt["artifact"], tgt["symbol"]))
        if d is None or not d.container or d.has_body:
            continue
        td = ws.registry.get((tgt["artifact"], d.container))
        if td is None or td.type_kind != "interface":
            continue
        if len(iface_simple_symbols.get(
                _simple_type_name(d.container).split("<")[0], set())) != 1:
            continue  # same-named interfaces elsewhere — refuse, never guess
        member_tail = tgt["symbol"][len(d.container):].lstrip(".")
        found = impls_of(d.container, member_tail)
        if len(found) != 1:
            continue
        impl_artifact, impl_sym = found[0]
        entry["static_target"] = {"symbol": tgt["symbol"], "artifact": tgt["artifact"]}
        entry["target"] = ws.target_of(impl_artifact, impl_sym)
        entry["resolved_via"] = "iface-impl"


# ---------------------------------------------------------------------
# DI registry (last-wins) upgrade
# ---------------------------------------------------------------------


def _container_simple_of_target(ws: _Workspace, target: dict) -> str:
    d = ws.registry.get((target.get("artifact"), target.get("symbol")))
    container = d.container if (d is not None and d.container) else \
        _symbol_container(target.get("symbol") or "")
    return _simple_type_name(container).split("<")[0] if container else ""


def _apply_di_registry(ws: _Workspace, site_entries: list[tuple[CallSite, dict]]):
    """`services.addSingleton(Notifier.class, SmsNotifier.class)` rewires
    interface calls to the registered implementation (last registration wins).
    HARDENED: the add*() invocation itself must bind to the micro-DI container
    type, and the implementation member must be concrete."""
    invocations: dict[str, list[tuple[CallSite, dict]]] = {}
    for site, entry in site_entries:
        if site.site_kind == "invocation":
            invocations.setdefault(site.file, []).append((site, entry))

    def registration_trusted(lit: CallSite) -> bool:
        best: tuple[CallSite, dict] | None = None
        for s, e in invocations.get(lit.file, []):
            if s.token != lit.di_method:
                continue
            if (s.line, s.col) > (lit.line, lit.col):
                continue
            if best is None or (s.line, s.col) > (best[0].line, best[0].col):
                best = (s, e)
        if best is None:
            return False
        entry = best[1]
        if entry["status"] not in ("resolved", "external"):
            return False
        return _container_simple_of_target(ws, entry.get("target") or {}) == \
            _DI_CONTAINER_SIMPLE

    regs: dict[str, dict] = {}
    for site, entry in site_entries:
        if site.site_kind != "class_literal" or entry["status"] != "resolved":
            continue
        if site.ta_role not in _DI_ROLES:
            continue
        if not registration_trusted(site):
            continue
        g = regs.setdefault(site.ta_group or "", {"file": entry["file"], "line": entry["line"]})
        g[site.ta_role] = (entry["target"]["artifact"], entry["target"]["symbol"])

    service_to_impl: dict[tuple[str, str], tuple[str, str]] = {}
    for g in sorted(regs.values(), key=lambda g: (g.get("file", ""), g.get("line", 0))):
        if "service" in g and "impl" in g:
            service_to_impl[g["service"]] = g["impl"]

    if not service_to_impl:
        return

    for site, entry in site_entries:
        if site.site_kind != "invocation" or entry["status"] != "resolved":
            continue
        tgt = entry["target"]
        d = ws.registry.get((tgt["artifact"], tgt["symbol"]))
        if d is None or not d.container:
            continue
        impl = service_to_impl.get((tgt["artifact"], d.container))
        if impl is None:
            continue
        member_tail = tgt["symbol"][len(d.container):].lstrip(".")
        impl_artifact, impl_type = impl
        impl_symbol = f"{impl_type}.{member_tail}"
        impl_decl = ws.registry.get((impl_artifact, impl_symbol))
        if impl_decl is None or not impl_decl.has_body:
            continue     # missing or abstract override — never splice a guess
        entry["static_target"] = {"symbol": tgt["symbol"], "artifact": tgt["artifact"]}
        entry["target"] = ws.target_of(impl_artifact, impl_symbol)
        entry["resolved_via"] = "di"


# ---------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------


_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.$]+(?:\.\*)?)\s*;", re.M)
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.$]+)\s*;", re.M)
_JDK_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "com.sun.")


def _imported_packages(content: str) -> list[str]:
    out = []
    for fqn in _IMPORT_RE.findall(content):
        out.append(fqn[:-2] if fqn.endswith(".*") else fqn.rsplit(".", 1)[0])
    return out


def _select_warmup_site(proj: Project) -> tuple[str, CallSite] | None:
    """Prefer a call site in a file that imports a package NOT declared inside
    this project root: a successful definition there proves the Maven
    classpath is bound, not merely that the syntax tree exists."""
    contents: dict[str, str] = {}
    declared: set[str] = set()
    for fp in sorted(proj.files):
        try:
            with open(fp, encoding="utf-8") as f:
                contents[fp] = f.read()
        except OSError:
            continue
        m = _PACKAGE_RE.search(contents[fp])
        if m:
            declared.add(m.group(1))

    fallback: tuple[str, CallSite] | None = None
    for fp, content in contents.items():
        sites = [s for s in extract_call_sites(fp, content)
                 if s.site_kind in ("invocation", "object_creation")]
        if not sites:
            continue
        if fallback is None:
            fallback = (fp, sites[0])
        external = [pkg for pkg in _imported_packages(content)
                    if pkg not in declared and not pkg.startswith(_JDK_PREFIXES)]
        if not external:
            continue
        names = {fqn.rsplit(".", 1)[-1] for fqn in _IMPORT_RE.findall(content)
                 if not fqn.endswith(".*")}
        for s in sites:
            text = f"{s.token} {s.receiver_text or ''}"
            if any(n in text for n in names):
                return fp, s
        return fp, sites[0]
    return fallback


async def _semantic_warmup(sess: ProjectSession, proj: Project, deadline_s: float = 90.0):
    """documentSymbol readiness proves only the syntax tree; overload binding
    needs the resolved Maven classpath (ServiceReady precedes it — spike ③).
    Probe a cross-artifact site until definition answers."""
    target = _select_warmup_site(proj)
    if target is None:
        return
    fp, site = target
    await sess.open_file(fp)
    end = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < end:
        try:
            if await sess.definition(fp, site.line - 1, site.col):
                return
        except (LspError, LspTimeout):
            pass
        await asyncio.sleep(2.0)


async def _run_project(ws: _Workspace, proj: Project, client_factory,
                       sem: asyncio.Semaphore) -> tuple[dict, list[dict]]:
    entry = {"sln": ws.rel(proj.pom), "status": "resolved", "reason": None,
             "files": [ws.rel(f) for f in sorted(proj.files)]}
    sites_out: list[dict] = []
    async with sem:
        sess = ProjectSession(proj, client_factory)
        try:
            await sess.start_and_wait_ready()
            if client_factory is None:
                await _semantic_warmup(sess, proj)
            pairs: list[tuple[CallSite, dict]] = []
            for fp in sorted(proj.files):
                await sess.open_file(fp)
                with open(fp, encoding="utf-8") as f:
                    content = f.read()
                for site in extract_call_sites(fp, content):
                    pairs.append((site, await _resolve_site(sess, ws, site)))
            _apply_di_registry(ws, pairs)
            sites_out = [e for _, e in pairs]
        except (LspConnectionError, LspTimeout, LspError, OSError) as e:
            entry["status"] = "fallback"
            entry["reason"] = f"{type(e).__name__}: {e}"
            sites_out = []
        finally:
            try:
                await sess.stop()
            except Exception:
                pass
    return entry, sites_out


async def _run_all(ws: _Workspace, client_factory) -> tuple[list[dict], list[dict]]:
    sem = asyncio.Semaphore(max(1, config.JAVA_LSP_CONCURRENCY))
    # heaviest projects first: they dominate wall-clock and suffer most from
    # cold-start contention when started last
    order = sorted(ws.projects, key=lambda p: len(p.files), reverse=True)
    results = await asyncio.gather(
        *(_run_project(ws, p, client_factory, sem) for p in order))
    projects = [r[0] for r in results]
    site_lists = [r[1] for r in results]

    # serial second chance: a ready-timeout during the concurrent wave is
    # usually contention, not a broken project — retry once, alone
    for i, entry in enumerate(projects):
        if entry["status"] == "fallback" and "LspTimeout" in (entry.get("reason") or ""):
            retry_entry, retry_sites = await _run_project(
                ws, order[i], client_factory, asyncio.Semaphore(1))
            if retry_entry["status"] == "resolved":
                projects[i] = retry_entry
                site_lists[i] = retry_sites

    sites = [s for lst in site_lists for s in lst]
    projects.sort(key=lambda s: s["sln"])
    return projects, sites


def run_resolution(target_dir: str, java_files: list[str], output_dir: str,
                   client_factory=None) -> dict:
    """Synchronous Phase 1.6 entry point; writes <output_dir>/resolution.json."""
    target_dir = os.path.abspath(target_dir)
    result: dict = {
        "version": 2,
        "target_dir": target_dir,
        "lsp": {"enabled": bool(config.JAVA_LSP_ENABLED), "cmd": config.JAVA_LSP_CMD},
        "solutions": [],
        "fallback_files": [],
        "declarations": [],
        "sites": [],
        "ambiguous_symbols": [],
        "stats": {},
    }

    def rel_all(paths):
        return sorted(os.path.relpath(p, target_dir) for p in paths)

    if not config.JAVA_LSP_ENABLED:
        result["fallback_files"] = rel_all(java_files)
        _write_artifact(output_dir, result)
        return result

    discovery = discover_projects(target_dir, java_files)
    result["fallback_files"] = rel_all(discovery.fallback_files)

    ws = _Workspace(target_dir, discovery)
    projects, sites = asyncio.run(_run_all(ws, client_factory))
    _apply_interface_impl_upgrade(ws, sites)
    result["solutions"] = projects

    resolved_poms = {p["sln"] for p in projects if p["status"] == "resolved"}
    declarations = []
    for artifact in sorted(a for a, p in ws.artifacts.items()
                           if ws.rel(p.pom) in resolved_poms):
        for d in ws.indexes[artifact].all_decls():
            declarations.append({
                "artifact": artifact, "symbol": d.symbol, "kind": d.kind,
                "file": ws.rel(d.file), "line": d.line,
                "container": d.container, "namespace": d.namespace,
            })
    result["declarations"] = declarations
    result["sites"] = sites
    result["ambiguous_symbols"] = [
        {"artifact": a, "symbol": s} for a, s in sorted(ws.dup_members)]

    stats: dict[str, int] = {}
    for s in sites:
        stats[s["status"]] = stats.get(s["status"], 0) + 1
    stats["declarations"] = len(declarations)
    stats["solutions_resolved"] = sum(1 for p in projects if p["status"] == "resolved")
    stats["solutions_fallback"] = sum(1 for p in projects if p["status"] != "resolved")
    result["stats"] = stats

    _write_artifact(output_dir, result)
    return result


def _write_artifact(output_dir: str, result: dict):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "resolution.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
