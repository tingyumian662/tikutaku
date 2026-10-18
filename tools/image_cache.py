"""镜像缓存管理：记录本地已有镜像、LRU 淘汰、磁盘上限、锚题白名单。

清单存 bank/image_cache.json：
  { "images": {ref: {"size_mb": int, "last_used": iso, "bank_ids": [...], "anchor": bool}} }

上限默认 200GB（E 盘实余 ~249GB，取保守值），锚题（anchor=true）永不淘汰。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from env import BANK  # noqa: E402

CACHE_FILE = BANK / "image_cache.json"
DEFAULT_LIMIT_MB = 200 * 1024


def _load() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {"images": {}}


def _save(data: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _fmt_err(cp: subprocess.CompletedProcess) -> str:
    return (cp.stderr or cp.stdout or "").strip()[-300:]


def docker_available() -> bool:
    try:
        cp = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=20)
        return cp.returncode == 0
    except Exception:
        return False


def local_size_mb(ref: str) -> int:
    cp = subprocess.run(["docker", "image", "inspect", ref, "--format", "{{.Size}}"],
                        capture_output=True, text=True)
    if cp.returncode == 0:
        try:
            return int(cp.stdout.strip()) // (1024 * 1024)
        except ValueError:
            pass
    return 0


def pull(ref: str, bank_id: str, anchor: bool = False, registry_mirror: str | None = None) -> bool:
    """拉一个镜像并登记。registry_mirror 形如 docker.1ms.run（Docker Hub 被墙时的中转）。"""
    target = f"{registry_mirror}/{ref}" if registry_mirror else ref
    print(f"  docker pull {target}")
    cp = subprocess.run(["docker", "pull", target], capture_output=True, text=True)
    if cp.returncode != 0:
        print(f"  [pull 失败] {_fmt_err(cp)}")
        return False
    if registry_mirror:
        # 中转拉的镜像 tag 带前缀，重打回原名便于统一引用
        subprocess.run(["docker", "tag", target, ref], capture_output=True)
    data = _load()
    data["images"][ref] = {
        "size_mb": local_size_mb(ref),
        "last_used": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bank_ids": sorted(set(data["images"].get(ref, {}).get("bank_ids", []) + [bank_id])),
        "anchor": anchor,
    }
    _save(data)
    evict_if_needed()
    return True


def touch(ref: str) -> None:
    data = _load()
    if ref in data["images"]:
        data["images"][ref]["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save(data)


def has(ref: str) -> bool:
    return ref in _load()["images"]


def total_mb() -> int:
    return sum(v["size_mb"] for v in _load()["images"].values())


def evict_if_needed(limit_mb: int = DEFAULT_LIMIT_MB) -> list[str]:
    """超限则按 LRU 删除非锚题镜像，返回被删 ref 列表。"""
    data = _load()
    used = sum(v["size_mb"] for v in data["images"].values())
    if used <= limit_mb:
        return []
    evictable = sorted(
        ((v["last_used"], ref) for ref, v in data["images"].items() if not v["anchor"]),
    )
    removed = []
    for _, ref in evictable:
        if used <= limit_mb:
            break
        cp = subprocess.run(["docker", "rmi", ref], capture_output=True, text=True)
        if cp.returncode == 0:
            used -= data["images"][ref]["size_mb"]
            removed.append(ref)
            del data["images"][ref]
    _save(data)
    if removed:
        print(f"[缓存] LRU 淘汰 {len(removed)} 个镜像，当前 {used//1024}GB")
    return removed


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "status":
        d = _load()
        print(f"登记镜像 {len(d['images'])} 个，共 {total_mb()//1024}GB / 上限 {DEFAULT_LIMIT_MB//1024}GB")
        for ref, v in sorted(d["images"].items(), key=lambda x: -x[1]['size_mb'])[:10]:
            print(f"  {v['size_mb']:>6}MB  {'锚' if v['anchor'] else ' '}  {ref}")
    else:
        print("用法: image_cache.py status")
