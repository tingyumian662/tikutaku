"""把 BIP checkout_fail 题的 7 位短 buggy_commit 解析成 40 位（GitHub API）。

短 SHA 在 codeload 404 的原因是对象不可达/歧义，需完整 SHA。API 限流（60/h/IP）
时等待重试；Clash 开着时可走代理自动生效（HTTPS_PROXY 不设，直连，403 只影响速率）。

用法: D:/tikutaku/venv/Scripts/python.exe tools/resolve_bip_sha.py
解析完成后自动重跑 31 题筛查。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


def api_json(url: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={
        "User-Agent": "tikutaku", "Accept": "application/vnd.github+json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return -1, None


def main() -> int:
    import pandas as pd

    df = pd.read_parquet(ROOT / "bank" / "index.parquet")
    data = json.loads((ROOT / "logs" / "bip_screen.json").read_text(encoding="utf-8"))
    fails = [k for k, v in data.items() if v.get("verdict") == "checkout_fail"]
    print(f"{len(fails)} checkout_fail tasks to resolve", flush=True)
    resolved, gone = [], []
    for i, bid in enumerate(fails):
        r = df[df.bank_id == bid]
        if not len(r):
            continue
        env = json.loads(r.iloc[0].env_notes)
        sha = env.get("buggy_commit", "")
        ru = str(env.get("repo_url") or r.iloc[0].repo or "")
        full_repo = ru.rstrip("/").split("github.com/")[-1] if "github.com" in ru else (
            ru if "/" in ru else f"{ru}/{ru}")
        if len(sha) == 40:
            resolved.append((bid, sha))
            continue
        for attempt in range(30):
            code, data_j = api_json(f"https://api.github.com/repos/{full_repo}/commits/{sha}")
            if code == 200 and data_j:
                full = data_j["sha"]
                env["buggy_commit"] = full
                df.loc[df.bank_id == bid, "env_notes"] = json.dumps(env, ensure_ascii=False)
                resolved.append((bid, full))
                print(f"{bid} {sha[:7]} -> {full[:12]}...", flush=True)
                break
            if code == 404:
                gone.append((bid, sha))
                print(f"{bid} {sha[:7]} -> 404 GONE (skip)", flush=True)
                break
            if code == 403 or code == -1:
                wait = 300 if code == 403 else 30
                print(f"rate-limited/err ({code}), sleep {wait}s (attempt {attempt+1})", flush=True)
                time.sleep(wait)
        time.sleep(2)
    if resolved:
        df.to_parquet(ROOT / "bank" / "index.parquet")
        print(f"saved {len(resolved)} resolved commits into index.parquet", flush=True)
    if gone:
        gf = ROOT / "logs" / "bip_gone_commits.json"
        gf.write_text(json.dumps({b: s for b, s in gone}, indent=1), encoding="utf-8")
        print(f"{len(gone)} commits are gone upstream (logged)", flush=True)
    # 自动重跑已解析题的筛查
    if resolved:
        import subprocess
        ids = ",".join(b for b, _ in resolved)
        subprocess.run([sys.executable, "-u", str(ROOT / "tools" / "bip_screen_invalid.py"), ids])
    return 0


if __name__ == "__main__":
    sys.exit(main())
