#!/usr/bin/env bash
# 前提: 9 個の jp.iryo ライブラリが ../../_feed へ deploy 済みであること。
set -euo pipefail
cd "$(dirname "$0")"; ROOT="$(cd ../.. && pwd)"
export JAVA_HOME="${JAVA21_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
mvn -q -s "$ROOT/settings.xml" -Dmaven.repo.local="$ROOT/_m2" clean package dependency:copy-dependencies -DoutputDirectory=target/deps
"$JAVA_HOME/bin/java" -cp "target/classes:target/deps/*" iryo.probe.Program
