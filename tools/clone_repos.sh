#!/usr/bin/env bash
# Phase 1: 克隆全部 harness 仓库到 sources/repos/
# 用法: bash tools/clone_repos.sh   （幂等：已存在且非空则跳过）
set -u
cd "$(dirname "$0")/.."
REPOS="sources/repos"
mkdir -p "$REPOS"

clone() { # clone <目录名> <url> [额外参数]
  local name="$1" url="$2"; shift 2
  if [ -d "$REPOS/$name/.git" ] || [ -n "$(ls -A "$REPOS/$name" 2>/dev/null)" ]; then
    echo "[skip] $name"
    return 0
  fi
  echo "==== clone $name <- $url ===="
  git clone --depth 1 "$@" "$url" "$REPOS/$name" || echo "[FAIL] $name"
}

# --- SWE 系 harness ---
clone swebench            https://github.com/SWE-bench/SWE-bench
clone swe-bench-live      https://github.com/microsoft/SWE-bench-Live
clone swe-rebench-v2      https://github.com/SWE-rebench/SWE-rebench-V2
clone swe-rebench-harness https://github.com/SWE-rebench/SWE-bench-fork
clone multi-swe-bench     https://github.com/multi-swe-bench/multi-swe-bench
clone swe-smith           https://github.com/SWE-bench/SWE-smith
clone r2e-gym             https://github.com/R2E-Gym/R2E-Gym

# --- Terminal-Bench（任务集 + harness）---
clone terminal-bench-1    https://github.com/harbor-framework/terminal-bench-1
clone terminal-bench      https://github.com/harbor-framework/terminal-bench

# --- 其余 ---
clone livecodebench       https://github.com/LiveCodeBench/LiveCodeBench
clone swe-perf            https://github.com/SWE-Perf/SWE-Perf
clone cve-bench           https://github.com/uiuc-kang-lab/cve-bench
clone defects4j           https://github.com/rjust/defects4j
clone bugsinpy            https://github.com/soarsmu/BugsInPy

# --- SWE-Lancer：现 main 已清空，锁定旧提交 999bc21e ---
if [ ! -d "$REPOS/swelancer/.git" ]; then
  echo "==== clone swelancer @ 999bc21e ===="
  git clone https://github.com/openai/SWELancer-Benchmark "$REPOS/swelancer" \
    && git -C "$REPOS/swelancer" checkout 999bc21e \
    || echo "[FAIL] swelancer"
else
  echo "[skip] swelancer"
fi

echo "==== 完成，盘点 ===="
du -sh "$REPOS"/* 2>/dev/null | sort -rh | head -20
