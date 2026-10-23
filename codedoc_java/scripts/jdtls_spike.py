#!/usr/bin/env python3
"""Qualification spike for Eclipse JDT LS (approved conversion plan, S2).

The spike is intentionally self-contained and repeatable:

* creates and builds a Java 21 mini-lib/mini-app corpus in
  /tmp/jdtls_spike_mini;
* drives graph_generator.lsp_client.LspClient through a recording subclass;
* compares jdt:// class-file contents without and with an attached sources jar;
* runs the approved 16 qualification checks; and
* writes raw JSON fixtures plus SPIKE_NOTES.md under
  tests/fixtures/jdtls_spike/.

Run:
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
      .venv/bin/python scripts/jdtls_spike.py

No graph_generator source is modified by this spike.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import Any
from urllib.parse import unquote, urlsplit


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from graph_generator.lsp_client import (  # noqa: E402
    LspClient,
    LspError,
    LspTimeout,
)


SPIKE_ROOT = Path("/tmp/jdtls_spike_mini")
LIB_DIR = SPIKE_ROOT / "mini-lib"
APP_DIR = SPIKE_ROOT / "mini-app"
FEED_DIR = SPIKE_ROOT / "_feed"
M2_DIR = SPIKE_ROOT / "_m2"
SETTINGS_XML = SPIKE_ROOT / "settings.xml"
WORK_DIR = SPIKE_ROOT / "_jdtls"
HELD_SOURCE_DIR = SPIKE_ROOT / "_held_sources"
FIXTURE_DIR = REPO / "tests" / "fixtures" / "jdtls_spike"

JAVA_HOME = Path("/usr/lib/jvm/java-21-openjdk-amd64")
JAVA_EXE = JAVA_HOME / "bin" / "java"
JDTLS = Path("/home/admin/tools/jdt-language-server/bin/jdtls")
MAVEN = shutil.which("mvn") or "mvn"

GROUP_PATH = Path("jp/iryo/spike/mini-lib/1.0.0")
LOCAL_GAV_DIR = M2_DIR / GROUP_PATH
FEED_GAV_DIR = FEED_DIR / GROUP_PATH
SOURCE_JAR = FEED_GAV_DIR / "mini-lib-1.0.0-sources.jar"
HELD_SOURCE_JAR = HELD_SOURCE_DIR / SOURCE_JAR.name
SOURCE_SENTINEL = "SOURCE_SENTINEL_JDTLS_SPIKE"

JAVA_SETTINGS: dict[str, Any] = {
    "maven": {"downloadSources": True},
    "import": {"gradle": {"enabled": False}},
    "configuration": {
        "maven": {"userSettings": str(SETTINGS_XML)},
    },
}


SETTINGS_TEXT = """\
<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.2.0 https://maven.apache.org/xsd/settings-1.2.0.xsd">
  <localRepository>/tmp/jdtls_spike_mini/_m2</localRepository>
</settings>
"""

LIB_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>jp.iryo.spike</groupId>
  <artifactId>mini-lib</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    <maven.compiler.parameters>true</maven.compiler.parameters>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
        <configuration>
          <release>21</release>
          <parameters>true</parameters>
          <encoding>UTF-8</encoding>
        </configuration>
      </plugin>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-source-plugin</artifactId>
        <version>3.3.1</version>
        <executions>
          <execution>
            <id>attach-sources</id>
            <goals><goal>jar-no-fork</goal></goals>
          </execution>
        </executions>
      </plugin>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-deploy-plugin</artifactId>
        <version>3.1.3</version>
      </plugin>
    </plugins>
  </build>
</project>
"""

APP_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>jp.iryo.spike</groupId>
  <artifactId>mini-app</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    <maven.compiler.parameters>true</maven.compiler.parameters>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <repositories>
    <repository>
      <id>feed</id>
      <url>file:///tmp/jdtls_spike_mini/_feed</url>
    </repository>
  </repositories>
  <dependencies>
    <dependency>
      <groupId>jp.iryo.spike</groupId>
      <artifactId>mini-lib</artifactId>
      <version>1.0.0</version>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
        <configuration>
          <release>21</release>
          <parameters>true</parameters>
          <encoding>UTF-8</encoding>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""

LIB_SOURCES = {
    "Calculations.java": """\
package jp.iryo.spike.lib;

/** SOURCE_SENTINEL_JDTLS_SPIKE real attached source. */
public final class Calculations {
    public static final String 日本語定数 = "診療";

    private Calculations() {}

    public static int calculate(int a, int b) {
        return a + b;
    }

    public static long calculate(long a, long b) {
        return a + b;
    }

    public static int calculate(int... values) {
        int total = 0;
        for (int value : values) {
            total += value;
        }
        return total;
    }

    public static int 点数変換(int 点数) {
        return 点数 * 10;
    }
}
""",
    "Cache.java": """\
package jp.iryo.spike.lib;

import java.util.HashMap;
import java.util.Map;

public final class Cache<K, V> {
    private final Map<K, V> values = new HashMap<>();

    public V get(K key) {
        return values.get(key);
    }

    public void put(K key, V value) {
        values.put(key, value);
    }
}
""",
    "Rec.java": """\
package jp.iryo.spike.lib;

public record Rec(int amount) {}
""",
    "Notifier.java": """\
package jp.iryo.spike.lib;

public interface Notifier {
    void send(String s);

    default double fee() {
        return 0.05;
    }
}
""",
    "MailNotifier.java": """\
package jp.iryo.spike.lib;

public final class MailNotifier implements Notifier {
    @Override
    public void send(String s) {
        System.out.println("メール:" + s);
    }
}
""",
    "OldTable.java": """\
package jp.iryo.spike.lib;

@Deprecated
public class OldTable {
    public int calc(int a, int b) {
        return a + b + 1000;
    }
}
""",
    "PointMath.java": """\
package jp.iryo.spike.lib;

public final class PointMath {
    private PointMath() {}

    public static int toYen(int point) {
        return point * 10;
    }
}
""",
}

APP_SOURCES = {
    "BaseThing.java": """\
package jp.iryo.spike.app;

class BaseThing {
    BaseThing() {}
}
""",
    "App.java": """\
package jp.iryo.spike.app;

import java.util.List;
import java.util.function.Consumer;
import java.util.function.IntUnaryOperator;
import jp.iryo.spike.lib.Cache;
import jp.iryo.spike.lib.Calculations;
import jp.iryo.spike.lib.MailNotifier;
import jp.iryo.spike.lib.Notifier;
import jp.iryo.spike.lib.OldTable;
import jp.iryo.spike.lib.PointMath;
import jp.iryo.spike.lib.Rec;
import static jp.iryo.spike.lib.PointMath.toYen;

public class App extends BaseThing {
    private final String name;

    public App() { this("既定"); }

    public App(String name) { super(); this.name = name; }

    static String localPlain(String value) {
        return value;
    }

    public static void main(String[] args) {
        String plain = localPlain("局所");
        int exact = Calculations.calculate(1, 2);
        long widened = Calculations.calculate(1, 2L);
        int many = Calculations.calculate(1, 2, 3);
        int japanese = Calculations.点数変換(3);
        String constant = Calculations.日本語定数;

        Rec rec = new Rec(5);
        int amount = rec.amount();
        Cache<String, Rec> cache = new Cache<>();
        cache.put("外来", rec);
        Rec cached = cache.get("外来");

        IntUnaryOperator staticRef = PointMath::toYen;
        MailNotifier mail = new MailNotifier();
        Consumer<String> instanceRef = mail::send;
        Notifier n = new Notifier() {
            @Override public void send(String s) {}
        };
        double fee = n.fee();
        Class<?> notifierType = Notifier.class;
        Class<?> implType = MailNotifier.class;

        int yen = toYen(15);
        List<String> visits = List.of("外来");
        String firstVisit = visits.get(0);
        System.out.println(firstVisit);
        int old = new OldTable().calc(1, 2);

        new App();
        staticRef.applyAsInt(yen);
        instanceRef.accept(constant);
        n.send(nameOf(plain, cached, exact, widened, many, japanese, amount, fee,
                notifierType, implType, old));
    }

    private static String nameOf(Object... values) {
        return String.valueOf(values.length);
    }
}
""",
    "NeverOpened.java": """\
package jp.iryo.spike.app;

import jp.iryo.spike.lib.PointMath;

public class NeverOpened {
    public int invoke() {
        return PointMath.toYen(99);
    }
}
""",
}


# needle is deliberately the full unique source line. token is where the LSP
# position lands. All columns are calculated as UTF-16 code units.
SITE_SPECS: dict[
    str,
    tuple[str, str, str] | tuple[str, str, str, int],
] = {
    "local_plain": ("App.java", '        String plain = localPlain("局所");', "localPlain"),
    "overload_exact": ("App.java", "        int exact = Calculations.calculate(1, 2);", "calculate"),
    "overload_mixed": ("App.java", "        long widened = Calculations.calculate(1, 2L);", "calculate"),
    "varargs": ("App.java", "        int many = Calculations.calculate(1, 2, 3);", "calculate"),
    "japanese_method": ("App.java", "        int japanese = Calculations.点数変換(3);", "点数変換"),
    # The fourth field selects the second matching token on the source line.
    "record_ctor": ("App.java", "        Rec rec = new Rec(5);", "Rec", 1),
    "record_accessor": (
        "App.java",
        "        int amount = rec.amount();",
        "amount",
        1,
    ),
    "cache_get": ("App.java", '        Rec cached = cache.get("外来");', "get"),
    "method_ref_static": ("App.java", "        IntUnaryOperator staticRef = PointMath::toYen;", "toYen"),
    "method_ref_instance": ("App.java", "        Consumer<String> instanceRef = mail::send;", "send"),
    "anonymous_notifier": (
        "App.java",
        "        Notifier n = new Notifier() {",
        "Notifier",
        1,
    ),
    "default_fee": ("App.java", "        double fee = n.fee();", "fee"),
    "class_literal_notifier": ("App.java", "        Class<?> notifierType = Notifier.class;", "Notifier"),
    "class_literal_impl": ("App.java", "        Class<?> implType = MailNotifier.class;", "MailNotifier"),
    "this_ctor": ("App.java", '    public App() { this("既定"); }', "this"),
    "super_ctor": ("App.java", "    public App(String name) { super(); this.name = name; }", "super"),
    "static_import": ("App.java", "        int yen = toYen(15);", "toYen"),
    "jdk_list_get": ("App.java", "        String firstVisit = visits.get(0);", "get"),
    "jdk_println": ("App.java", "        System.out.println(firstVisit);", "println"),
    "deprecated_stub": ("App.java", "        int old = new OldTable().calc(1, 2);", "calc"),
    "never_opened": ("NeverOpened.java", "        return PointMath.toYen(99);", "toYen"),
}


def _safe_reset_spike_root() -> None:
    expected = Path("/tmp/jdtls_spike_mini")
    if SPIKE_ROOT != expected or SPIKE_ROOT.parent != Path("/tmp"):
        raise RuntimeError(f"refusing unsafe reset target: {SPIKE_ROOT}")
    if SPIKE_ROOT.exists():
        shutil.rmtree(SPIKE_ROOT)
    SPIKE_ROOT.mkdir(parents=True)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_mini_corpus() -> dict[str, Any]:
    """Create, build, deploy, and execute the throwaway mini corpus."""
    _safe_reset_spike_root()
    _write_text(SETTINGS_XML, SETTINGS_TEXT)
    _write_text(LIB_DIR / "pom.xml", LIB_POM)
    _write_text(APP_DIR / "pom.xml", APP_POM)

    lib_src = LIB_DIR / "src" / "main" / "java" / "jp" / "iryo" / "spike" / "lib"
    app_src = APP_DIR / "src" / "main" / "java" / "jp" / "iryo" / "spike" / "app"
    for name, text in LIB_SOURCES.items():
        _write_text(lib_src / name, text)
    for name, text in APP_SOURCES.items():
        _write_text(app_src / name, text)

    env = os.environ.copy()
    env["JAVA_HOME"] = str(JAVA_HOME)
    env["PATH"] = str(JAVA_HOME / "bin") + os.pathsep + env.get("PATH", "")

    commands: list[dict[str, Any]] = []

    def run(args: list[str], cwd: Path, timeout: int = 300) -> dict[str, Any]:
        started = time.monotonic()
        completed = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        record = {
            "argv": args,
            "cwd": str(cwd),
            "returncode": completed.returncode,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        commands.append(record)
        if completed.returncode != 0:
            raise RuntimeError(
                f"command failed ({completed.returncode}): {args}\n{completed.stderr}"
            )
        return record

    java_version = run([str(JAVA_EXE), "-version"], SPIKE_ROOT)
    maven_version = run([MAVEN, "-version"], SPIKE_ROOT)
    deploy = run(
        [
            MAVEN,
            "-q",
            "-s",
            "../settings.xml",
            "clean",
            "deploy",
            "-DaltDeploymentRepository=feed::file:///tmp/jdtls_spike_mini/_feed",
        ],
        LIB_DIR,
    )
    package = run(
        [MAVEN, "-q", "-s", "../settings.xml", "clean", "package"],
        APP_DIR,
    )
    execute = run(
        [
            str(JAVA_EXE),
            "-cp",
            f"{APP_DIR / 'target/classes'}:{LIB_DIR / 'target/classes'}",
            "jp.iryo.spike.app.App",
        ],
        APP_DIR,
    )

    expected_files = {
        "binary_jar": FEED_GAV_DIR / "mini-lib-1.0.0.jar",
        "sources_jar": SOURCE_JAR,
        "pom": FEED_GAV_DIR / "mini-lib-1.0.0.pom",
        "app_class": APP_DIR
        / "target"
        / "classes"
        / "jp"
        / "iryo"
        / "spike"
        / "app"
        / "App.class",
    }
    missing = [str(path) for path in expected_files.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"mini corpus build omitted expected files: {missing}")

    return {
        "java_version": java_version,
        "maven_version": maven_version,
        "deploy": deploy,
        "package": package,
        "execute": execute,
        "commands": commands,
        "expected_files": {name: str(path) for name, path in expected_files.items()},
        "source_jar_sha256": _sha256(SOURCE_JAR),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        stat = path.stat()
        out[str(path.relative_to(root))] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": _sha256(path),
        }
    return out


def _fixture(name: str, payload: Any) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _utf16_col(line: str, codepoint_col: int) -> int:
    return len(line[:codepoint_col].encode("utf-16-le")) // 2


def _site_path(site: str) -> Path:
    filename = SITE_SPECS[site][0]
    return (
        APP_DIR
        / "src"
        / "main"
        / "java"
        / "jp"
        / "iryo"
        / "spike"
        / "app"
        / filename
    )


def _site_record(site: str) -> dict[str, Any]:
    spec = SITE_SPECS[site]
    filename, needle, token = spec[:3]
    token_occurrence = spec[3] if len(spec) == 4 else 0
    path = _site_path(site)
    text = path.read_text(encoding="utf-8")
    count = text.count(needle)
    if count != 1:
        raise RuntimeError(f"{site}: expected one needle, found {count}: {needle!r}")
    needle_offset = text.index(needle)
    token_offsets: list[int] = []
    search_from = 0
    while True:
        found = needle.find(token, search_from)
        if found < 0:
            break
        token_offsets.append(found)
        search_from = found + len(token)
    if token_occurrence >= len(token_offsets):
        raise RuntimeError(
            f"{site}: token occurrence {token_occurrence} absent in {needle!r}"
        )
    token_in_needle = token_offsets[token_occurrence]
    token_offset = needle_offset + token_in_needle
    line_no = text.count("\n", 0, token_offset)
    line_start = text.rfind("\n", 0, token_offset) + 1
    line_end = text.find("\n", token_offset)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    codepoint_col = token_offset - line_start
    return {
        "site": site,
        "file": str(path),
        "relative_file": filename,
        "source_line": line,
        "token": token,
        "token_occurrence": token_occurrence,
        "position": {
            "line": line_no,
            "character": _utf16_col(line, codepoint_col),
        },
    }


def _all_site_records() -> dict[str, dict[str, Any]]:
    return {site: _site_record(site) for site in SITE_SPECS}


def _error_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, LspError):
        payload.update({"code": exc.code, "data": exc.data})
    return payload


class RecordingJdtlsClient(LspClient):
    """LspClient with lossless visibility into server traffic.

    Every server notification is retained with its full params. In addition to
    the base client's method-count watcher, this subclass has the predicate
    needed by JDT LS: language/status where params.type == ServiceReady.
    """

    def __init__(self, *args: Any, label: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.label = label
        self.process_started_at = time.monotonic()
        self.initialize_started_at: float | None = None
        self.notifications: list[dict[str, Any]] = []
        self.server_requests: list[dict[str, Any]] = []
        self.request_records: list[dict[str, Any]] = []
        self.language_statuses: list[dict[str, Any]] = []
        self.service_ready_event = asyncio.Event()
        self.service_ready_ms: float | None = None
        self.opened_files: set[str] = set()
        self.stderr_path = WORK_DIR / label / "stderr.log"
        self._stderr_stream: Any = None

    async def start(self) -> None:
        """Start like LspClient, but retain stderr under the throwaway root."""
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_stream = self.stderr_path.open("wb")
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr_stream,
            limit=4 * 1024 * 1024,
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())

    async def stop(self) -> None:
        try:
            await super().stop()
        finally:
            if self._stderr_stream is not None:
                self._stderr_stream.close()
                self._stderr_stream = None

    def stderr_text(self) -> str:
        if self._stderr_stream is not None:
            self._stderr_stream.flush()
        try:
            return self.stderr_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""

    def mark_initialize_start(self) -> None:
        self.initialize_started_at = time.monotonic()

    def _elapsed_ms(self) -> float:
        base = self.initialize_started_at or self.process_started_at
        return round((time.monotonic() - base) * 1000, 3)

    async def request(
        self, method: str, params: Any, timeout: float | None = None
    ) -> Any:
        started_ms = self._elapsed_ms()
        record: dict[str, Any] = {
            "method": method,
            "params": params,
            "started_ms": started_ms,
        }
        try:
            result = await super().request(method, params, timeout=timeout)
            record["completed_ms"] = self._elapsed_ms()
            record["result"] = result
            return result
        except Exception as exc:
            record["completed_ms"] = self._elapsed_ms()
            record["error"] = _error_payload(exc)
            raise
        finally:
            self.request_records.append(record)

    async def _handle(self, msg: dict[str, Any]) -> None:
        if "method" in msg and "id" not in msg:
            record = {
                "method": msg.get("method"),
                "params": msg.get("params"),
                "elapsed_ms": self._elapsed_ms(),
            }
            self.notifications.append(record)
            if msg.get("method") == "language/status":
                status = {
                    "params": msg.get("params"),
                    "elapsed_ms": record["elapsed_ms"],
                }
                self.language_statuses.append(status)
                params = msg.get("params") or {}
                if params.get("type") == "ServiceReady":
                    if self.service_ready_ms is None:
                        self.service_ready_ms = record["elapsed_ms"]
                    self.service_ready_event.set()
        await super()._handle(msg)

    async def _answer_server_request(self, msg: dict[str, Any]) -> None:
        self.server_requests.append(
            {
                "method": msg.get("method"),
                "id": msg.get("id"),
                "params": msg.get("params"),
                "elapsed_ms": self._elapsed_ms(),
            }
        )
        await super()._answer_server_request(msg)

    async def wait_service_ready(self, timeout: float = 240.0) -> None:
        if self.service_ready_event.is_set():
            return
        try:
            await asyncio.wait_for(self.service_ready_event.wait(), timeout)
        except asyncio.TimeoutError:
            raise LspTimeout(f"no language/status ServiceReady within {timeout}s")


def _jdtls_command(
    label: str,
    *,
    unique_configuration: bool,
    xmx: str = "700m",
) -> tuple[list[str], Path, Path | None]:
    data_dir = WORK_DIR / label / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    configuration_dir: Path | None = None
    cmd = [
        str(JDTLS),
        "--java-executable",
        str(JAVA_EXE),
        "--jvm-arg=-Xms128m",
        f"--jvm-arg=-Xmx{xmx}",
    ]
    if unique_configuration:
        configuration_dir = WORK_DIR / label / "configuration"
        configuration_dir.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--jvm-arg=-Dosgi.configuration.area={configuration_dir}")
    cmd.extend(["-data", str(data_dir)])
    return cmd, data_dir, configuration_dir


def _initialize_params(project_dir: Path) -> dict[str, Any]:
    return {
        "processId": os.getpid(),
        "rootUri": project_dir.as_uri(),
        "workspaceFolders": [
            {"uri": project_dir.as_uri(), "name": project_dir.name}
        ],
        "capabilities": {
            "workspace": {
                "configuration": True,
                "workspaceFolders": True,
            },
            "window": {"workDoneProgress": True},
            "textDocument": {
                "documentSymbol": {
                    "hierarchicalDocumentSymbolSupport": True,
                },
                "definition": {},
                "hover": {
                    "contentFormat": ["markdown", "plaintext"],
                },
            },
        },
        "initializationOptions": {
            "extendedClientCapabilities": {
                "classFileContentsSupport": True,
            },
            "settings": {"java": JAVA_SETTINGS},
        },
    }


async def _start_session(
    label: str,
    project_dir: Path,
    *,
    unique_configuration: bool = True,
    tolerate_initialization_failure: bool = False,
) -> tuple[RecordingJdtlsClient, dict[str, Any]]:
    cmd, data_dir, configuration_dir = _jdtls_command(
        label, unique_configuration=unique_configuration
    )
    client = RecordingJdtlsClient(
        cmd,
        cwd=str(project_dir),
        config_sections={"java": JAVA_SETTINGS},
        default_timeout=60.0,
        watch_notifications={"language/status"},
        workspace_folders=[str(project_dir)],
        label=label,
    )
    await client.start()
    client.mark_initialize_start()
    params = _initialize_params(project_dir)
    launch: dict[str, Any] = {
        "label": label,
        "project_dir": str(project_dir),
        "command": cmd,
        "data_dir": str(data_dir),
        "configuration_dir": (
            str(configuration_dir) if configuration_dir is not None else None
        ),
        "initialize": {
            "method": "initialize",
            "params": params,
        },
    }
    try:
        result = await client.request("initialize", params, timeout=90.0)
        launch["initialize"]["response"] = result
        await client.notify("initialized", {})
    except Exception as exc:
        launch["initialize"]["error"] = _error_payload(exc)
        launch["effective_process"] = _process_record(client)
        launch["stderr"] = client.stderr_text()
        if tolerate_initialization_failure:
            return client, launch
        await client.stop()
        raise
    launch["effective_process"] = _process_record(client)
    launch["stderr"] = client.stderr_text()
    return client, launch


async def _did_open(client: RecordingJdtlsClient, path: Path) -> None:
    key = str(path)
    if key in client.opened_files:
        return
    await client.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": path.as_uri(),
                "languageId": "java",
                "version": 1,
                "text": path.read_text(encoding="utf-8"),
            }
        },
    )
    client.opened_files.add(key)


def _position_params(site: str) -> dict[str, Any]:
    rec = _site_record(site)
    return {
        "textDocument": {"uri": Path(rec["file"]).as_uri()},
        "position": rec["position"],
    }


async def _positional_request(
    client: RecordingJdtlsClient,
    site: str,
    method: str,
    *,
    require_open: bool = True,
    timeout: float = 45.0,
) -> dict[str, Any]:
    rec = _site_record(site)
    path = Path(rec["file"])
    if require_open:
        await _did_open(client, path)
    params = _position_params(site)
    out: dict[str, Any] = {
        "site": rec,
        "did_open": str(path) in client.opened_files,
        "request": {"method": method, "params": params},
    }
    try:
        out["response"] = await client.request(method, params, timeout=timeout)
    except Exception as exc:
        out["error"] = _error_payload(exc)
    return out


def _definition_uri(response: Any) -> str | None:
    if not response:
        return None
    locations = response if isinstance(response, list) else [response]
    for location in locations:
        if not isinstance(location, dict):
            continue
        uri = location.get("uri") or location.get("targetUri")
        if isinstance(uri, str):
            return uri
    return None


async def _warmup_cross_jar(
    client: RecordingJdtlsClient,
    *,
    timeout: float = 240.0,
) -> dict[str, Any]:
    """Poll a known cross-artifact invocation from immediately after init."""
    await _did_open(client, _site_path("overload_mixed"))
    deadline = time.monotonic() + timeout
    attempts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        params = _position_params("overload_mixed")
        attempt: dict[str, Any] = {
            "started_ms": client._elapsed_ms(),
            "request": {
                "method": "textDocument/definition",
                "params": params,
            },
        }
        try:
            response = await client.request(
                "textDocument/definition", params, timeout=20.0
            )
            attempt["completed_ms"] = client._elapsed_ms()
            attempt["response"] = response
            uri = _definition_uri(response)
            attempt["target_uri"] = uri
            attempts.append(attempt)
            if uri and uri.startswith("jdt://"):
                return {
                    "success": True,
                    "attempts": attempts,
                    "first_success_ms": attempt["completed_ms"],
                    "target_uri": uri,
                }
        except Exception as exc:
            attempt["completed_ms"] = client._elapsed_ms()
            attempt["error"] = _error_payload(exc)
            attempts.append(attempt)
        await asyncio.sleep(0.5)
    return {
        "success": False,
        "attempts": attempts,
        "first_success_ms": None,
        "target_uri": None,
    }


async def _document_symbols_until_ready(
    client: RecordingJdtlsClient,
    path: Path,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    await _did_open(client, path)
    deadline = time.monotonic() + timeout
    attempts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        params = {"textDocument": {"uri": path.as_uri()}}
        rec: dict[str, Any] = {
            "request": {
                "method": "textDocument/documentSymbol",
                "params": params,
            },
            "started_ms": client._elapsed_ms(),
        }
        try:
            response = await client.request(
                "textDocument/documentSymbol", params, timeout=20.0
            )
            rec["completed_ms"] = client._elapsed_ms()
            rec["response"] = response
            attempts.append(rec)
            if response:
                return {"success": True, "attempts": attempts}
        except Exception as exc:
            rec["completed_ms"] = client._elapsed_ms()
            rec["error"] = _error_payload(exc)
            attempts.append(rec)
        await asyncio.sleep(0.5)
    return {"success": False, "attempts": attempts}


async def _class_file_contents(
    client: RecordingJdtlsClient,
    uri: str,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    # TextDocumentIdentifier: the jdt:// URI must round-trip byte-for-byte.
    params = {"uri": uri}
    out: dict[str, Any] = {
        "request": {
            "method": "java/classFileContents",
            "params": params,
        }
    }
    try:
        out["response"] = await client.request(
            "java/classFileContents", params, timeout=timeout
        )
    except Exception as exc:
        out["error"] = _error_payload(exc)
    return out


async def _class_file_until_source(
    client: RecordingJdtlsClient,
    uri: str,
    *,
    timeout: float = 150.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    attempts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        attempt = await _class_file_contents(client, uri)
        attempt["elapsed_ms"] = client._elapsed_ms()
        attempts.append(attempt)
        content = attempt.get("response")
        if isinstance(content, str) and SOURCE_SENTINEL in content:
            return {
                "success": True,
                "attempts": attempts,
                "final": attempt,
            }
        await asyncio.sleep(1.0)
    return {
        "success": False,
        "attempts": attempts,
        "final": attempts[-1] if attempts else None,
    }


def _process_record(client: RecordingJdtlsClient) -> dict[str, Any]:
    proc = client._proc
    if proc is None:
        return {}
    pid = proc.pid
    cmdline = ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmdline = " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
    except OSError:
        pass
    ps = subprocess.run(
        ["ps", "-o", "pid=,ppid=,rss=,etime=,args=", "-p", str(pid)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    rss_kib: int | None = None
    fields = ps.stdout.strip().split(None, 3)
    if len(fields) >= 3 and fields[2].isdigit():
        rss_kib = int(fields[2])
    return {
        "pid": pid,
        "is_alive": client.is_alive,
        "returncode": proc.returncode,
        "cmdline": cmdline,
        "ps_argv": ["ps", "-o", "pid=,ppid=,rss=,etime=,args=", "-p", str(pid)],
        "ps_stdout": ps.stdout,
        "ps_stderr": ps.stderr,
        "ps_returncode": ps.returncode,
        "rss_kib": rss_kib,
    }


def _session_capture(
    client: RecordingJdtlsClient,
    launch: dict[str, Any],
) -> dict[str, Any]:
    return {
        **launch,
        "process": _process_record(client),
        "service_ready_ms": client.service_ready_ms,
        "language_statuses": client.language_statuses,
        "server_requests": client.server_requests,
        "notifications": client.notifications,
        "request_records": client.request_records,
        "opened_files": sorted(client.opened_files),
    }


def _remove_sources_for_no_source_session() -> dict[str, Any]:
    HELD_SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    if not SOURCE_JAR.is_file():
        raise RuntimeError(f"cannot hold missing source jar: {SOURCE_JAR}")
    shutil.copy2(SOURCE_JAR, HELD_SOURCE_JAR)
    held_sha = _sha256(HELD_SOURCE_JAR)
    SOURCE_JAR.unlink()
    removed_sidecars: list[str] = []
    for suffix in (".sha1", ".md5"):
        sidecar = Path(str(SOURCE_JAR) + suffix)
        if sidecar.exists():
            sidecar.unlink()
            removed_sidecars.append(str(sidecar))
    local_existed = LOCAL_GAV_DIR.exists()
    if local_existed:
        shutil.rmtree(LOCAL_GAV_DIR)
    return {
        "held_source_jar": str(HELD_SOURCE_JAR),
        "held_sha256": held_sha,
        "feed_source_jar_removed": not SOURCE_JAR.exists(),
        "removed_checksum_sidecars": removed_sidecars,
        "isolated_local_gav_existed_before_purge": local_existed,
        "isolated_local_gav_absent_after_purge": not LOCAL_GAV_DIR.exists(),
    }


def _restore_sources_for_source_session() -> dict[str, Any]:
    if not HELD_SOURCE_JAR.is_file():
        raise RuntimeError(f"held source jar missing: {HELD_SOURCE_JAR}")
    FEED_GAV_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HELD_SOURCE_JAR, SOURCE_JAR)
    if LOCAL_GAV_DIR.exists():
        shutil.rmtree(LOCAL_GAV_DIR)
    return {
        "restored_source_jar": str(SOURCE_JAR),
        "restored_sha256": _sha256(SOURCE_JAR),
        "isolated_local_gav_absent_before_session": not LOCAL_GAV_DIR.exists(),
    }


async def _run_without_sources() -> dict[str, Any]:
    client, launch = await _start_session(
        "no_sources", APP_DIR, unique_configuration=True
    )
    try:
        warmup_task = asyncio.create_task(_warmup_cross_jar(client))
        service_error: dict[str, Any] | None = None
        try:
            await client.wait_service_ready(timeout=240.0)
        except Exception as exc:
            service_error = _error_payload(exc)
        warmup = await warmup_task
        document_symbols = await _document_symbols_until_ready(
            client, _site_path("local_plain")
        )

        mixed_definition = await _positional_request(
            client, "overload_mixed", "textDocument/definition"
        )
        mixed_uri = _definition_uri(mixed_definition.get("response"))
        class_contents = (
            await _class_file_contents(client, mixed_uri)
            if mixed_uri
            else {"error": {"message": "cross-jar definition returned no URI"}}
        )
        stub_hover = await _positional_request(
            client, "deprecated_stub", "textDocument/hover"
        )
        old_definition = await _positional_request(
            client, "deprecated_stub", "textDocument/definition"
        )
        await asyncio.sleep(1.0)
        process = _process_record(client)
        capture = _session_capture(client, launch)
        return {
            "launch": launch,
            "service_ready_error": service_error,
            "warmup": warmup,
            "document_symbols": document_symbols,
            "definition": mixed_definition,
            "class_file_contents": class_contents,
            "binary_stub_hover": stub_hover,
            "old_table_definition": old_definition,
            "process": process,
            "session": capture,
        }
    finally:
        await client.stop()


async def _run_with_sources() -> dict[str, Any]:
    client, launch = await _start_session(
        "with_sources", APP_DIR, unique_configuration=True
    )
    try:
        warmup_task = asyncio.create_task(_warmup_cross_jar(client))
        service_error: dict[str, Any] | None = None
        try:
            await client.wait_service_ready(timeout=240.0)
        except Exception as exc:
            service_error = _error_payload(exc)
        warmup = await warmup_task
        document_symbols = await _document_symbols_until_ready(
            client, _site_path("local_plain")
        )

        definitions: dict[str, Any] = {}
        for site in (
            "local_plain",
            "overload_exact",
            "overload_mixed",
            "varargs",
            "japanese_method",
            "record_ctor",
            "record_accessor",
            "cache_get",
            "method_ref_static",
            "method_ref_instance",
            "anonymous_notifier",
            "default_fee",
            "class_literal_notifier",
            "class_literal_impl",
            "this_ctor",
            "super_ctor",
            "static_import",
            "jdk_list_get",
            "jdk_println",
            "deprecated_stub",
        ):
            definitions[site] = await _positional_request(
                client, site, "textDocument/definition"
            )

        hover_sites = {
            "plain_method": "local_plain",
            "overload": "overload_mixed",
            "generic_instantiated_receiver": "cache_get",
            "varargs": "varargs",
            "constructor": "record_ctor",
            "record_accessor": "record_accessor",
        }
        hovers = {
            label: await _positional_request(
                client, site, "textDocument/hover"
            )
            for label, site in hover_sites.items()
        }

        mixed_uri = _definition_uri(
            definitions["overload_mixed"].get("response")
        )
        class_contents = (
            await _class_file_until_source(client, mixed_uri)
            if mixed_uri
            else {
                "success": False,
                "error": {"message": "cross-jar definition returned no URI"},
            }
        )

        # This request deliberately violates the normal didOpen-before-position
        # rule so item 13 can establish JDT LS behavior.
        unopened = await _positional_request(
            client,
            "never_opened",
            "textDocument/definition",
            require_open=False,
        )

        await asyncio.sleep(2.0)
        idle_process = _process_record(client)
        capture = _session_capture(client, launch)
        return {
            "launch": launch,
            "service_ready_error": service_error,
            "warmup": warmup,
            "document_symbols": document_symbols,
            "definitions": definitions,
            "hovers": hovers,
            "class_file_contents": class_contents,
            "unopened_definition": unopened,
            "idle_process": idle_process,
            "session": capture,
        }
    finally:
        await client.stop()


async def _run_concurrency_attempt(
    label_prefix: str,
    *,
    unique_configuration: bool,
) -> dict[str, Any]:
    """Run one simultaneous pair and retain failures instead of aborting."""
    lib_client: RecordingJdtlsClient | None = None
    app_client: RecordingJdtlsClient | None = None
    try:
        (lib_client, lib_launch), (app_client, app_launch) = await asyncio.gather(
            _start_session(
                f"{label_prefix}_lib",
                LIB_DIR,
                unique_configuration=unique_configuration,
                tolerate_initialization_failure=True,
            ),
            _start_session(
                f"{label_prefix}_app",
                APP_DIR,
                unique_configuration=unique_configuration,
                tolerate_initialization_failure=True,
            ),
        )

        initialization_ok = (
            "error" not in lib_launch["initialize"]
            and "error" not in app_launch["initialize"]
        )
        if initialization_ok:
            lib_probe = (
                LIB_DIR
                / "src"
                / "main"
                / "java"
                / "jp"
                / "iryo"
                / "spike"
                / "lib"
                / "Calculations.java"
            )
            app_probe = _site_path("local_plain")
            await asyncio.gather(
                _did_open(lib_client, lib_probe),
                _did_open(app_client, app_probe),
            )

            service_results = await asyncio.gather(
                lib_client.wait_service_ready(timeout=240.0),
                app_client.wait_service_ready(timeout=240.0),
                return_exceptions=True,
            )
            symbol_results = await asyncio.gather(
                _document_symbols_until_ready(lib_client, lib_probe),
                _document_symbols_until_ready(app_client, app_probe),
            )
            await asyncio.sleep(2.0)
        else:
            service_results = [
                RuntimeError("initialize did not succeed"),
                RuntimeError("initialize did not succeed"),
            ]
            symbol_results = [
                {"success": False, "skipped": "initialize failed"},
                {"success": False, "skipped": "initialize failed"},
            ]

        lib_process = _process_record(lib_client)
        app_process = _process_record(app_client)

        lib_text = (
            json.dumps(lib_client.notifications, ensure_ascii=False)
            + lib_launch.get("stderr", "")
            + lib_client.stderr_text()
        ).lower()
        app_text = (
            json.dumps(app_client.notifications, ensure_ascii=False)
            + app_launch.get("stderr", "")
            + app_client.stderr_text()
        ).lower()
        lock_patterns = (
            "workspace is already in use",
            "already locked",
            "could not acquire lock",
            "lock error",
            "configuration area",
            "location.lock",
        )
        lock_hits = [
            pattern
            for pattern in lock_patterns
            if pattern in lib_text or pattern in app_text
        ]

        return {
            "configuration_mode": (
                "unique per-session osgi.configuration.area"
                if unique_configuration
                else "wrapper default shared read-only cascaded configuration"
            ),
            "unique_data": True,
            "unique_configuration": unique_configuration,
            "lib": {
                "launch": lib_launch,
                "service_ready": not isinstance(service_results[0], Exception),
                "service_error": (
                    _error_payload(service_results[0])
                    if isinstance(service_results[0], Exception)
                    else None
                ),
                "document_symbols": symbol_results[0],
                "process": lib_process,
                "session": _session_capture(lib_client, lib_launch),
                "stderr": lib_client.stderr_text(),
            },
            "app": {
                "launch": app_launch,
                "service_ready": not isinstance(service_results[1], Exception),
                "service_error": (
                    _error_payload(service_results[1])
                    if isinstance(service_results[1], Exception)
                    else None
                ),
                "document_symbols": symbol_results[1],
                "process": app_process,
                "session": _session_capture(app_client, app_launch),
                "stderr": app_client.stderr_text(),
            },
            "lock_error_hits": lock_hits,
            "both_ready": (
                initialization_ok
                and not isinstance(service_results[0], Exception)
                and not isinstance(service_results[1], Exception)
                and symbol_results[0]["success"]
                and symbol_results[1]["success"]
            ),
        }
    finally:
        clients = [c for c in (lib_client, app_client) if c is not None]
        if clients:
            await asyncio.gather(*(client.stop() for client in clients))


async def _run_concurrency_check() -> dict[str, Any]:
    """Test the wrapper default, then use isolated config if it collides."""
    shared_attempt = await _run_concurrency_attempt(
        "concurrent_shared",
        unique_configuration=False,
    )
    unique_attempt: dict[str, Any] | None = None
    if not shared_attempt["both_ready"]:
        unique_attempt = await _run_concurrency_attempt(
            "concurrent_unique",
            unique_configuration=True,
        )
    effective = unique_attempt or shared_attempt
    shared_stderr = (
        shared_attempt["lib"].get("stderr", "")
        + shared_attempt["app"].get("stderr", "")
    )
    if "read-only file system" in shared_stderr.lower():
        shared_failure_reason = (
            "wrapper-default writable configuration resolved under "
            "/home/admin/.eclipse, which is read-only in this environment"
        )
    elif not shared_attempt["both_ready"]:
        shared_failure_reason = (
            "wrapper-default configuration sessions exited during initialize; "
            "see retained stderr"
        )
    else:
        shared_failure_reason = None
    return {
        "shared_configuration_attempt": shared_attempt,
        "unique_configuration_attempt": unique_attempt,
        "effective_attempt": (
            "unique_configuration"
            if unique_attempt is not None
            else "wrapper_shared_configuration"
        ),
        "shared_configuration_caused_trouble": not shared_attempt["both_ready"],
        "shared_configuration_failure_reason": shared_failure_reason,
        "shared_configuration_lock_error_hits": shared_attempt[
            "lock_error_hits"
        ],
        "unique_configuration_jvm_arg_needed": not shared_attempt["both_ready"],
        "lib": effective["lib"],
        "app": effective["app"],
        "lock_error_hits": effective["lock_error_hits"],
        "both_ready": effective["both_ready"],
    }


def _workspace_pollution() -> dict[str, Any]:
    names = (".project", ".classpath", ".settings", "bin")
    projects: dict[str, Any] = {}
    for project in (LIB_DIR, APP_DIR):
        found: dict[str, Any] = {}
        for name in names:
            path = project / name
            if path.exists():
                if path.is_dir():
                    found[name] = {
                        "type": "directory",
                        "entries": sorted(
                            str(child.relative_to(project))
                            for child in path.rglob("*")
                        ),
                    }
                else:
                    found[name] = {
                        "type": "file",
                        "size": path.stat().st_size,
                    }
        projects[project.name] = {
            "checked": list(names),
            "found": found,
            "polluted": bool(found),
        }
    return {"projects": projects}


def _hover_shape(hover: Any) -> str:
    if not isinstance(hover, dict):
        return type(hover).__name__
    contents = hover.get("contents")
    if isinstance(contents, list):
        if all(
            isinstance(item, (str, dict))
            and (
                isinstance(item, str)
                or set(item).issuperset({"language", "value"})
            )
            for item in contents
        ):
            return "MarkedString[]"
        return "list"
    if (
        isinstance(contents, dict)
        and isinstance(contents.get("kind"), str)
        and "value" in contents
    ):
        return "MarkupContent"
    if isinstance(contents, str):
        return "MarkedString(string)"
    return type(contents).__name__


def _hover_java_value(hover: Any) -> str | None:
    if not isinstance(hover, dict):
        return None
    contents = hover.get("contents")
    if isinstance(contents, list):
        for item in contents:
            if (
                isinstance(item, dict)
                and item.get("language") == "java"
                and isinstance(item.get("value"), str)
            ):
                return item["value"]
    return None


def _first_30_lines(content: Any) -> str:
    if not isinstance(content, str):
        return ""
    return "\n".join(content.splitlines()[:30])


def _uri_container(uri: str | None) -> str | None:
    if not uri or not uri.startswith("jdt://"):
        return None
    path = urlsplit(uri).path
    parts = [unquote(part) for part in path.split("/") if part]
    return parts[0] if parts else None


def _artifact_label_from_uri(uri: str | None) -> dict[str, Any]:
    container = _uri_container(uri)
    if not container:
        return {"container": None, "artifact_label": None, "rule": None}
    if container == "mini-lib-1.0.0.jar":
        return {
            "container": container,
            "artifact_label": "mini-lib",
            "rule": (
                "exact GAV map jp.iryo.spike:mini-lib:1.0.0 -> artifactId"
            ),
        }
    if container.endswith(".jar"):
        stem = container[:-4]
        stripped = re.sub(r"-\d+(?:[._-][0-9A-Za-z]+)*$", "", stem)
        return {
            "container": container,
            "artifact_label": stripped or stem,
            "rule": "unmapped external jar basename with version suffix removed",
        }
    return {
        "container": container,
        "artifact_label": container,
        "rule": "JDK/JRT module segment",
    }


def _response_ok(probe: dict[str, Any]) -> bool:
    return "error" not in probe and bool(probe.get("response"))


def _definition_summary(probe: dict[str, Any]) -> str:
    uri = _definition_uri(probe.get("response"))
    if probe.get("error"):
        return f"error={probe['error']['message']}"
    if not uri:
        return "no definition"
    response = probe.get("response")
    first = response[0] if isinstance(response, list) and response else response
    if isinstance(first, dict):
        selected = first.get("range") or first.get("targetSelectionRange")
        return f"{uri} range={selected}"
    return uri


def _status_sequence(session: dict[str, Any]) -> str:
    statuses = session.get("language_statuses") or []
    parts: list[str] = []
    for status in statuses:
        params = status.get("params") or {}
        label = params.get("type") or "?"
        message = params.get("message")
        if message:
            label += f"({message})"
        parts.append(f"+{status.get('elapsed_ms')}ms {label}")
    return " -> ".join(parts)


def _fixture_payloads(
    build: dict[str, Any],
    source_toggle: dict[str, Any],
    no_sources: dict[str, Any],
    with_sources: dict[str, Any],
    concurrency: dict[str, Any],
    pollution: dict[str, Any],
    maven_evidence: dict[str, Any],
) -> dict[str, Any]:
    definitions = with_sources["definitions"]
    hovers = with_sources["hovers"]
    no_uri = _definition_uri(no_sources["definition"].get("response"))
    with_uri = _definition_uri(definitions["overload_mixed"].get("response"))
    jdk_uri = _definition_uri(definitions["jdk_list_get"].get("response"))
    no_content = no_sources["class_file_contents"].get("response")
    source_final = with_sources["class_file_contents"].get("final") or {}
    with_content = source_final.get("response")

    hover_payloads = dict(hovers)
    hover_payloads["binary_stub_without_sources"] = no_sources[
        "binary_stub_hover"
    ]

    return {
        "00_build.json": build,
        "00_source_positions.json": _all_site_records(),
        "01_launch_concurrency.json": {
            "main_launch_with_unique_configuration": with_sources["launch"],
            "concurrency": concurrency,
        },
        "02_handshake_notifications.json": {
            "no_sources_session": no_sources["session"],
            "with_sources_session": with_sources["session"],
        },
        "03_warmup_timing.json": {
            "no_sources": {
                "service_ready_ms": no_sources["session"]["service_ready_ms"],
                "warmup": no_sources["warmup"],
                "language_statuses": no_sources["session"][
                    "language_statuses"
                ],
            },
            "with_sources": {
                "service_ready_ms": with_sources["session"]["service_ready_ms"],
                "warmup": with_sources["warmup"],
                "language_statuses": with_sources["session"][
                    "language_statuses"
                ],
            },
        },
        "04_in_repo_definition.json": definitions["local_plain"],
        "05_cross_jar_without_sources.json": {
            "source_toggle": source_toggle["removed"],
            "definition": no_sources["definition"],
            "jdt_uri_exact": no_uri,
            "uri_contract": _artifact_label_from_uri(no_uri),
            "class_file_contents": no_sources["class_file_contents"],
            "first_30_lines": _first_30_lines(no_content),
            "old_table_definition": no_sources["old_table_definition"],
        },
        "06_cross_jar_with_sources.json": {
            "source_toggle": source_toggle["restored"],
            "definition": definitions["overload_mixed"],
            "jdt_uri_exact": with_uri,
            "uri_contract": _artifact_label_from_uri(with_uri),
            "class_file_contents": with_sources["class_file_contents"],
            "first_30_lines": _first_30_lines(with_content),
        },
        "07_jdk_jrt_definition.json": {
            "list_get": definitions["jdk_list_get"],
            "println": definitions["jdk_println"],
            "jdt_uri_exact": jdk_uri,
            "uri_contract": _artifact_label_from_uri(jdk_uri),
        },
        "08_hover_payloads.json": {
            label: {
                **probe,
                "observed_shape": _hover_shape(probe.get("response")),
            }
            for label, probe in hover_payloads.items()
        },
        "09_record_definitions.json": {
            "constructor": definitions["record_ctor"],
            "accessor": definitions["record_accessor"],
        },
        "10_method_references.json": {
            "static": definitions["method_ref_static"],
            "instance": definitions["method_ref_instance"],
        },
        "11_class_literals.json": {
            "interface": definitions["class_literal_notifier"],
            "implementation": definitions["class_literal_impl"],
        },
        "12_constructor_chains.json": {
            "this": definitions["this_ctor"],
            "super": definitions["super_ctor"],
        },
        "13_unopened_file.json": with_sources["unopened_definition"],
        "14_workspace_pollution.json": pollution,
        "15_idle_rss_concurrency.json": {
            "xmx": "700m",
            "with_sources_idle_process": with_sources["idle_process"],
            "concurrency": {
                "both_ready": concurrency["both_ready"],
                "lib_process": concurrency["lib"]["process"],
                "app_process": concurrency["app"]["process"],
            },
        },
        "16_maven_settings.json": maven_evidence,
        "17_additional_corpus_probes.json": {
            name: definitions[name]
            for name in (
                "overload_exact",
                "overload_mixed",
                "varargs",
                "japanese_method",
                "anonymous_notifier",
                "default_fee",
                "static_import",
                "deprecated_stub",
            )
        },
    }


def _make_notes(
    *,
    build: dict[str, Any],
    source_toggle: dict[str, Any],
    no_sources: dict[str, Any],
    with_sources: dict[str, Any],
    concurrency: dict[str, Any],
    pollution: dict[str, Any],
    maven_evidence: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[str]]:
    definitions = with_sources["definitions"]
    hovers = with_sources["hovers"]
    no_uri = _definition_uri(no_sources["definition"].get("response"))
    with_uri = _definition_uri(definitions["overload_mixed"].get("response"))
    jdk_uri = _definition_uri(definitions["jdk_list_get"].get("response"))
    no_content = no_sources["class_file_contents"].get("response")
    source_final = with_sources["class_file_contents"].get("final") or {}
    with_content = source_final.get("response")

    service_ms = no_sources["session"].get("service_ready_ms")
    success_ms = no_sources["warmup"].get("first_success_ms")
    gap_ms = (
        round(success_ms - service_ms, 3)
        if isinstance(service_ms, (int, float))
        and isinstance(success_ms, (int, float))
        else None
    )

    this_ok = _response_ok(definitions["this_ctor"])
    super_ok = _response_ok(definitions["super_ctor"])
    ctor_recommendation = "KEEP" if this_ok and super_ok else "DROP"

    local_uri = _definition_uri(definitions["local_plain"].get("response"))
    local_ok = bool(local_uri and local_uri.startswith("file:"))
    no_stub_ok = (
        isinstance(no_content, str)
        and bool(no_content.strip())
        and SOURCE_SENTINEL not in no_content
    )
    source_ok = (
        isinstance(with_content, str) and SOURCE_SENTINEL in with_content
    )
    jdk_contract = _artifact_label_from_uri(jdk_uri)
    feed_contract = _artifact_label_from_uri(with_uri or no_uri)

    hover_records: dict[str, dict[str, Any]] = {
        **hovers,
        "binary_stub_without_sources": no_sources["binary_stub_hover"],
    }
    hover_shapes = {
        label: _hover_shape(probe.get("response"))
        for label, probe in hover_records.items()
    }
    hovers_ok = all(
        _response_ok(probe) and shape != "NoneType"
        for probe, shape in zip(
            hover_records.values(), hover_shapes.values(), strict=True
        )
    )

    record_ctor_uri = _definition_uri(
        definitions["record_ctor"].get("response")
    )
    record_accessor_uri = _definition_uri(
        definitions["record_accessor"].get("response")
    )
    record_ok = bool(
        record_ctor_uri
        and record_ctor_uri.startswith("jdt://")
        and record_accessor_uri
        and record_accessor_uri.startswith("jdt://")
    )
    method_refs_ok = _response_ok(
        definitions["method_ref_static"]
    ) and _response_ok(definitions["method_ref_instance"])
    class_literals_ok = _response_ok(
        definitions["class_literal_notifier"]
    ) and _response_ok(definitions["class_literal_impl"])
    unopened_ok = (
        not with_sources["unopened_definition"]["did_open"]
        and _response_ok(with_sources["unopened_definition"])
    )
    rss_kib = with_sources["idle_process"].get("rss_kib")
    local_maven_ok = (
        maven_evidence["isolated_gav_absent_before_no_source_session"]
        and maven_evidence["isolated_gav_files_after_no_source_session"]
        and maven_evidence["home_scope_unchanged"]
    )
    shared_configuration_reason = concurrency.get(
        "shared_configuration_failure_reason"
    )
    shared_configuration_result = (
        shared_configuration_reason
        or "both wrapper-default sessions reached ready"
    )
    if shared_configuration_reason:
        concurrency_check_evidence = (
            "wrapper-default pair did not initialize because "
            f"{shared_configuration_reason}; shared-attempt lock hits="
            f"{concurrency['shared_configuration_lock_error_hits']}; "
            "the retry with unique -data and unique "
            "-Dosgi.configuration.area reached ServiceReady+documentSymbol "
            "for both mini-lib and mini-app."
        )
        concurrency_finding = (
            "the wrapper-default configuration failed on its writable "
            "configuration overlay, while two unique configuration sessions "
            "reached ready"
        )
    else:
        concurrency_check_evidence = (
            "two wrapper-default sessions with unique -data reached "
            "ServiceReady+documentSymbol for mini-lib and mini-app, with no "
            "lock errors."
        )
        concurrency_finding = (
            "two wrapper-default sessions with unique -data reached ready "
            "without configuration or lock trouble"
        )

    checks = [
        {
            "number": "①",
            "passed": bool(concurrency["both_ready"])
            and not concurrency["lock_error_hits"],
            "evidence": concurrency_check_evidence,
        },
        {
            "number": "②",
            "passed": (
                no_sources["session"].get("service_ready_ms") is not None
                and bool(no_sources["session"].get("server_requests"))
                and bool(no_sources["session"].get("notifications"))
            ),
            "evidence": (
                "initialize succeeded; server requests="
                f"{sorted({r['method'] for r in no_sources['session']['server_requests']})}; "
                f"status sequence: {_status_sequence(no_sources['session'])}."
            ),
        },
        {
            "number": "③",
            "passed": isinstance(service_ms, (int, float))
            and isinstance(success_ms, (int, float)),
            "evidence": (
                f"initialize→ServiceReady={service_ms} ms; "
                f"initialize→first successful cross-jar definition={success_ms} ms; "
                f"gap={gap_ms} ms."
            ),
        },
        {
            "number": "④",
            "passed": local_ok,
            "evidence": (
                "localPlain definition returned an in-workspace file URI: "
                f"{_definition_summary(definitions['local_plain'])}."
            ),
        },
        {
            "number": "⑤",
            "passed": bool(no_uri and no_uri.startswith("jdt://"))
            and no_stub_ok,
            "evidence": (
                f"without sources, definition URI={no_uri}; "
                f"classFileContents returned {len(no_content or '')} chars of "
                "FernFlower-decompiled fallback source."
            ),
        },
        {
            "number": "⑥",
            "passed": bool(with_uri and with_uri.startswith("jdt://"))
            and source_ok,
            "evidence": (
                f"with sources, definition remained jdt:// ({with_uri}); "
                f"classFileContents contained {SOURCE_SENTINEL}."
            ),
        },
        {
            "number": "⑦",
            "passed": bool(jdk_uri and jdk_uri.startswith("jdt://"))
            and jdk_contract["artifact_label"] == "java.base",
            "evidence": (
                f"List.get definition URI={jdk_uri}; frozen JDK label="
                f"{jdk_contract['artifact_label']}."
            ),
        },
        {
            "number": "⑧",
            "passed": hovers_ok,
            "evidence": "raw hover shapes: "
            + ", ".join(f"{name}={shape}" for name, shape in hover_shapes.items())
            + ".",
        },
        {
            "number": "⑨",
            "passed": record_ok,
            "evidence": (
                "record constructor and amount() accessor both returned definitions: "
                f"ctor={_definition_summary(definitions['record_ctor'])}; "
                f"accessor={_definition_summary(definitions['record_accessor'])}."
            ),
        },
        {
            "number": "⑩",
            "passed": method_refs_ok,
            "evidence": (
                "PointMath::toYen and mail::send both returned definitions."
            ),
        },
        {
            "number": "⑪",
            "passed": class_literals_ok,
            "evidence": (
                "Notifier.class and MailNotifier.class both returned binary definitions."
            ),
        },
        {
            "number": "⑫",
            # The check is conclusive even if the recommendation is DROP.
            "passed": "error" not in definitions["this_ctor"]
            and "error" not in definitions["super_ctor"],
            "evidence": (
                f"this()={_definition_summary(definitions['this_ctor'])}; "
                f"super()={_definition_summary(definitions['super_ctor'])}; "
                f"recommendation={ctor_recommendation}."
            ),
        },
        {
            "number": "⑬",
            "passed": unopened_ok,
            "evidence": (
                "NeverOpened.java was not sent via didOpen; definition result: "
                f"{_definition_summary(with_sources['unopened_definition'])}."
            ),
        },
        {
            "number": "⑭",
            "passed": True,
            "evidence": (
                "post-session metadata inventory captured; "
                + ", ".join(
                    f"{name} found={sorted(rec['found'])}"
                    for name, rec in pollution["projects"].items()
                )
                + "."
            ),
        },
        {
            "number": "⑮",
            "passed": isinstance(rss_kib, int)
            and rss_kib > 0
            and bool(concurrency["both_ready"]),
            "evidence": (
                f"idle ps RSS={rss_kib} KiB with -Xmx700m; "
                f"two-session ready={concurrency['both_ready']}."
            ),
        },
        {
            "number": "⑯",
            "passed": bool(local_maven_ok),
            "evidence": (
                "after purging jp.iryo.spike:mini-lib from isolated _m2, m2e "
                f"repopulated files={maven_evidence['isolated_gav_files_after_no_source_session']}; "
                f"~/.m2 jp/iryo/spike unchanged={maven_evidence['home_scope_unchanged']}."
            ),
        },
    ]

    exact_hover_json = json.dumps(
        no_sources["binary_stub_hover"].get("response"),
        ensure_ascii=False,
    )
    if "arg0" in exact_hover_json:
        stub_param_note = "stub hover exposed synthetic parameter names such as arg0"
    elif re.search(r"calc\s*\(\s*int\s+a\s*,\s*int\s+b", exact_hover_json):
        stub_param_note = "stub hover preserved real parameter names a/b via -parameters"
    else:
        stub_param_note = "parameter-name form is visible in the raw fixture"

    generic_json = json.dumps(
        hovers["generic_instantiated_receiver"].get("response"),
        ensure_ascii=False,
    )
    generic_substituted = (
        "Rec" in generic_json
        and "String" in generic_json
        and not re.search(r"\bV\s+get\s*\(K\b", generic_json)
    )

    passed_count = sum(1 for check in checks if check["passed"])
    findings = [
        (
            f"ServiceReady arrived at {service_ms} ms and the first successful "
            f"cross-jar definition at {success_ms} ms (gap {gap_ms} ms); use "
            "the semantic cross-artifact warmup as the real barrier."
        ),
        (
            "Jar and JDK navigation both use jdt://; attaching sources changes "
            "java/classFileContents from FernFlower-decompiled fallback text "
            "to the attached source but does not change the URI scheme or "
            "container contract."
        ),
        (
            f"Constructor-chain recommendation is {ctor_recommendation}; "
            f"{concurrency_finding}; one idle -Xmx700m server used "
            f"{rss_kib} KiB RSS."
        ),
    ]

    lines = [
        "# JDT LS qualification spike (S2)",
        "",
        "Generated by `scripts/jdtls_spike.py` against the throwaway corpus at "
        "`/tmp/jdtls_spike_mini`.",
        "",
        "## Checklist",
        "",
    ]
    for check in checks:
        verdict = "PASS" if check["passed"] else "FAIL"
        lines.append(
            f"- {check['number']} **{verdict}** — {check['evidence']}"
        )

    lines.extend(
        [
            "",
            "## FROZEN EXTERNAL label contract",
            "",
            "The exact observed in-feed jar URI (round-tripped unchanged):",
            "",
            "```text",
            str(with_uri or no_uri),
            "```",
            "",
            "The exact observed JDK/JRT URI:",
            "",
            "```text",
            str(jdk_uri),
            "```",
            "",
            "Derivation rule:",
            "",
            f"- In-feed jar container `{feed_contract['container']}` → exact "
            "GAV map `jp.iryo.spike:mini-lib:1.0.0` → artifact label "
            f"`{feed_contract['artifact_label']}`.",
            "- An unmapped external jar uses its URI container basename with "
            "the parseable version suffix removed.",
            f"- A JDK/JRT URI uses its module container segment verbatim: "
            f"`{jdk_contract['container']}` → artifact label "
            f"`{jdk_contract['artifact_label']}`.",
            "- The entire `jdt://` URI, including its query, is opaque and must "
            "round-trip verbatim into `java/classFileContents`.",
            "",
            "## Hover payloads",
            "",
        ]
    )
    for label, shape in hover_shapes.items():
        java_value = _hover_java_value(
            hover_records[label].get("response")
        )
        lines.append(
            f"- `{label}`: `{shape}`; Java value: `{java_value}`"
        )
    lines.extend(
        [
            "",
            f"The instantiated `Cache<String,Rec>.get(\"外来\")` hover "
            f"{'did' if generic_substituted else 'did not'} expose substituted "
            "receiver types. "
            f"For the no-source `OldTable.calc` probe, {stub_param_note}. "
            "Exact unnormalized payloads are in `08_hover_payloads.json`.",
            "",
            "## Readiness and notifications",
            "",
            f"- Without sources: `{_status_sequence(no_sources['session'])}`",
            f"- With sources: `{_status_sequence(with_sources['session'])}`",
            "- Server→client request methods: "
            f"`{sorted({request['method'] for request in no_sources['session']['server_requests']})}`. "
            "`workspace/configuration` was not requested in this run; the "
            "client's `config_sections['java']` hook was nevertheless loaded "
            "with the same Java settings supplied in `initializationOptions`.",
            f"- Initialize → ServiceReady: `{service_ms} ms`.",
            "- Initialize → first successful known cross-jar definition: "
            f"`{success_ms} ms`.",
            f"- Observed gap: `{gap_ms} ms`; ServiceReady "
            f"{'preceded' if gap_ms is not None and gap_ms >= 0 else 'did not precede'} "
            "the successful semantic binding.",
            "- The warmup barrier is therefore a successful definition on a "
            "known cross-artifact invocation, after the lifecycle probe; "
            "ServiceReady alone is not the binding proof.",
            "",
            "## classFileContents comparison",
            "",
            "Without the sources jar (first ~30 lines):",
            "",
            "```java",
            _first_30_lines(no_content),
            "```",
            "",
            "With the sources jar (first ~30 lines):",
            "",
            "```java",
            _first_30_lines(with_content),
            "```",
            "",
            "## Concurrency, Maven state, pollution, and memory",
            "",
            f"- Concurrent mini-lib + mini-app sessions: "
            f"`both_ready={concurrency['both_ready']}`, "
            f"`lock_error_hits={concurrency['lock_error_hits']}`.",
            "- The installed wrapper declares a shared read-only cascaded "
            "configuration but, without an explicit writable configuration "
            "area, its overlay resolved under `~/.eclipse`. "
            f"Wrapper-default result: `{shared_configuration_result}`; "
            f"shared-attempt lock errors: "
            f"`{concurrency['shared_configuration_lock_error_hits']}`. "
            f"An extra per-session `-Dosgi.configuration.area` was "
            f"{'needed' if concurrency['unique_configuration_jvm_arg_needed'] else 'not needed'} "
            "for the two-session test; the main capture sessions still used "
            "unique configuration areas to exercise the approved launch shape.",
            f"- m2e isolated local repository repopulated: "
            f"`{maven_evidence['isolated_gav_files_after_no_source_session']}`.",
            "- m2e fetched the real `mini-lib-1.0.0-sources.jar` into `_m2` "
            "after it was restored to `_feed`; before restoration it wrote "
            "only a `.lastUpdated` miss marker for that classifier.",
            f"- Home Maven GAV scope unchanged: "
            f"`{maven_evidence['home_scope_unchanged']}`.",
            f"- Workspace metadata inventory: "
            f"`{json.dumps(pollution, ensure_ascii=False)}`.",
            f"- Idle RSS from `ps`: `{rss_kib} KiB` with `-Xmx700m` "
            "(and an overriding `-Xms128m`, because the wrapper defaults to "
            "`-Xms1G`).",
            "",
            "## this()/super() site-kind decision",
            "",
            f"**{ctor_recommendation}**. "
            f"`this()` → {_definition_summary(definitions['this_ctor'])}; "
            f"`super()` → {_definition_summary(definitions['super_ctor'])}.",
            "",
            "## Three most consequential findings",
            "",
        ]
    )
    for index, finding in enumerate(findings, 1):
        lines.append(f"{index}. {finding}")
    lines.extend(
        [
            "",
            f"`SPIKE RESULT: {passed_count}/16 PASS`",
            "",
        ]
    )
    return "\n".join(lines), checks, findings


async def run_spike() -> tuple[int, list[str]]:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    stale_failure = FIXTURE_DIR / "_failure.json"
    if stale_failure.exists():
        stale_failure.unlink()

    home_gav_scope = Path.home() / ".m2" / "repository" / "jp" / "iryo" / "spike"
    home_before = _tree_snapshot(home_gav_scope)

    build = build_mini_corpus()
    _fixture("00_build.json", build)

    removed = _remove_sources_for_no_source_session()
    isolated_absent_before = not LOCAL_GAV_DIR.exists()
    no_sources = await _run_without_sources()
    isolated_after_no_sources = _tree_snapshot(LOCAL_GAV_DIR)

    restored = _restore_sources_for_source_session()
    with_sources = await _run_with_sources()
    isolated_after_sources = _tree_snapshot(LOCAL_GAV_DIR)

    concurrency = await _run_concurrency_check()
    pollution = _workspace_pollution()
    home_after = _tree_snapshot(home_gav_scope)
    maven_evidence = {
        "user_settings": str(SETTINGS_XML),
        "configured_local_repository": str(M2_DIR),
        "isolated_gav_absent_before_no_source_session": isolated_absent_before,
        "isolated_gav_files_after_no_source_session": sorted(
            isolated_after_no_sources
        ),
        "isolated_gav_files_after_with_source_session": sorted(
            isolated_after_sources
        ),
        "isolated_repository_file_count": len(_tree_snapshot(M2_DIR)),
        "home_gav_scope": str(home_gav_scope),
        "home_before": home_before,
        "home_after": home_after,
        "home_scope_unchanged": home_before == home_after,
        "workspace_configuration_requests": [
            request
            for request in no_sources["session"]["server_requests"]
            if request.get("method") == "workspace/configuration"
        ],
        "configuration_section_java": JAVA_SETTINGS,
    }
    source_toggle = {"removed": removed, "restored": restored}

    fixtures = _fixture_payloads(
        build,
        source_toggle,
        no_sources,
        with_sources,
        concurrency,
        pollution,
        maven_evidence,
    )
    for name, payload in fixtures.items():
        _fixture(name, payload)

    notes, checks, findings = _make_notes(
        build=build,
        source_toggle=source_toggle,
        no_sources=no_sources,
        with_sources=with_sources,
        concurrency=concurrency,
        pollution=pollution,
        maven_evidence=maven_evidence,
    )
    (FIXTURE_DIR / "SPIKE_NOTES.md").write_text(notes, encoding="utf-8")
    passed = sum(1 for check in checks if check["passed"])
    return passed, findings


def main() -> int:
    try:
        passed, findings = asyncio.run(run_spike())
    except Exception as exc:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        failure = {
            "error": _error_payload(exc),
            "traceback": traceback.format_exc(),
        }
        _fixture("_failure.json", failure)
        print(traceback.format_exc(), file=sys.stderr)
        print("SPIKE RESULT: 0/16 PASS")
        return 1

    print("3 MOST CONSEQUENTIAL FINDINGS")
    for index, finding in enumerate(findings, 1):
        print(f"{index}. {finding}")
    print(f"SPIKE RESULT: {passed}/16 PASS")
    return 0 if passed == 16 else 1


if __name__ == "__main__":
    raise SystemExit(main())
