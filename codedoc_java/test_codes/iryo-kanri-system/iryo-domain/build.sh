#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"; ROOT="$(cd .. && pwd)"
export JAVA_HOME="${JAVA21_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
rm -rf "$ROOT/_m2/jp/iryo/iryo-domain" "$HOME/.m2/repository/jp/iryo/iryo-domain"  # 依存側が古い 1.0.0 を掴まないよう両キャッシュを退避
mvn -q -s "$ROOT/settings.xml" -Dmaven.repo.local="$ROOT/_m2" clean deploy -DaltDeploymentRepository=feed::file://"$ROOT"/_feed
