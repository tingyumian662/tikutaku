#!/usr/bin/env bash
# Defects4J 金标重封（bash 驱动版）：绕开 python→MSYS 的命名空间问题。
# 流程: checkout → git diff BUGGY..FIXED 标签 → 存 tmp/patches/<bank_id>.patch
# 之后由 seal_patches.py（纯原生 Python）封存并回写索引。
set -u
cd "$(dirname "$0")/.."
source tools/d4j_env.sh
D4J_BIN="sources/repos/defects4j/framework/bin/defects4j"
WORK="${WORK:-/e/AI/d4j_w2}"   # 原 batch 目录被锁死文件占用，弃用换新
LIST="${LIST:-tmp/d4j_tasks.tsv}"
mkdir -p tmp/patches "$WORK"

total=$(wc -l < "$LIST")
i=0; ok=0; fail=0
t0=$(date +%s)
while IFS=$'\t' read -r bank_id pid bug_id; do
  i=$((i+1))
  bank_id="${bank_id%%$'\r'}"; pid="${pid%%$'\r'}"; bug_id="${bug_id%%$'\r'}"   # 防 CRLF
  [ -z "$bug_id" ] && continue
  [ -f "tmp/patches/$bank_id.patch" ] && { ok=$((ok+1)); continue; }   # 断点续跑
  rm -rf "$WORK/cur"
  if perl "$D4J_BIN" checkout -p "$pid" -v "${bug_id}b" -w "$WORK/cur" > /dev/null 2>&1; then
    if git -C "$WORK/cur" diff "D4J_${pid}_${bug_id}_BUGGY_VERSION" "D4J_${pid}_${bug_id}_FIXED_VERSION" \
        > "tmp/patches/$bank_id.patch" 2>/dev/null && [ -s "tmp/patches/$bank_id.patch" ]; then
      ok=$((ok+1))
    else
      rm -f "tmp/patches/$bank_id.patch"; fail=$((fail+1)); echo "[diff空] $bank_id"
    fi
  else
    fail=$((fail+1)); echo "[checkout失败] $bank_id ($pid-$bug_id)"
  fi
  if (( i % 25 == 0 )); then
    el=$(( $(date +%s) - t0 )); eta=$(( (total - i) * el / i / 60 ))
    echo "[$i/$total] ok=$ok fail=$fail ${el}s elapsed, 预计剩余 ${eta} 分钟"
  fi
done < "$LIST"
rm -rf "$WORK/cur"
echo "=== bash 阶段完成: ok=$ok fail=$fail, 总用时 $(( ($(date +%s) - t0) / 60 )) 分钟 ==="
