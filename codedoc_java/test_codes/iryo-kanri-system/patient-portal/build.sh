#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"; ROOT="$(cd .. && pwd)"
export JAVA_HOME="${JAVA21_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
mvn -q -s "$ROOT/settings.xml" -Dmaven.repo.local="$ROOT/_m2" clean package
