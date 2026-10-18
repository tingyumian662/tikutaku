#!/usr/bin/env bash
# 经 codeload.github.com 拉 tarball（git clone 断连时的替代通道），解包到 sources/repos/<name>/
# 幂等：目录已存在且非空则跳过
set -u
cd "$(dirname "$0")/.."
REPOS="sources/repos"
mkdir -p "$REPOS"

fetch() { # fetch <目录名> <owner/repo> <ref>
  local name="$1" repo="$2" ref="$3"
  if [ -n "$(ls -A "$REPOS/$name" 2>/dev/null)" ]; then
    echo "[skip] $name"; return 0
  fi
  echo "==== $name <- $repo@$ref (tarball) ===="
  local url="https://codeload.github.com/$repo/tar.gz/$ref"
  for i in 1 2 3; do
    if curl -sfL --connect-timeout 15 -m 600 "$url" -o "/tmp/$name.tgz"; then
      mkdir -p "$REPOS/$name"
      # tar 包内首层目录是 <repo>-<ref>，剥掉
      tar -xzf "/tmp/$name.tgz" -C "$REPOS/$name" --strip-components=1 && rm -f "/tmp/$name.tgz" \
        && echo "[ok] $name" && return 0
    fi
    echo "[retry $i] $name"; sleep 8
  done
  echo "[FAIL] $name"; rm -rf "$REPOS/$name"
}

fetch swe-perf        SWE-Perf/SWE-Perf             main
fetch cve-bench       uiuc-kang-lab/cve-bench       main
fetch defects4j       rjust/defects4j               master
fetch bugsinpy        soarsmu/BugsInPy              master
fetch swelancer       openai/SWELancer-Benchmark    999bc21e
fetch terminal-bench-1 harbor-framework/terminal-bench-1 main
fetch terminal-bench  harbor-framework/terminal-bench main
fetch livecodebench   LiveCodeBench/LiveCodeBench   main

echo "==== 盘点 ===="
ls "$REPOS"
