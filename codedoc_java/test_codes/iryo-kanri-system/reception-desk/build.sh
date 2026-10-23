#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"; ROOT="$(cd .. && pwd)"
export JAVA_HOME="${JAVA21_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
mvn -q -Dstyle.color=never -Dorg.slf4j.simpleLogger.log.org.apache.maven.plugin.compiler.CompilerMojo=warn -s "$ROOT/settings.xml" -Dmaven.repo.local="$ROOT/_m2" clean package dependency:copy-dependencies -DoutputDirectory=target/deps
"$JAVA_HOME/bin/java" -cp "target/classes:target/deps/*" iryo.reception.Program
