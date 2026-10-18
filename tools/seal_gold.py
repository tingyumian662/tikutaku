"""金标封存：gold patch 加密存 bank/seals/，密钥单独放 bank/seals/.key。

- seal(text) -> seal_id；同一 bank_id 重复 seal 为覆盖
- unseal(bank_id) -> text（判分与可解性验证时用）
- 评测时绝不能把明文 patch 放进 agent 可读的任务目录
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent))
from env import SEALS  # noqa: E402


def _key() -> bytes:
    key_file = SEALS / ".key"
    if key_file.exists():
        return key_file.read_bytes().strip()
    SEALS.mkdir(parents=True, exist_ok=True)
    k = Fernet.generate_key()
    # Windows 上尽量限制权限（尽力而为）
    key_file.write_bytes(k)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return k


def seal(bank_id: str, text: str) -> str:
    SEALS.mkdir(parents=True, exist_ok=True)
    f = Fernet(_key())
    blob = f.encrypt(text.encode("utf-8"))
    (SEALS / f"{bank_id}.bin").write_bytes(blob)
    return f"{bank_id}.bin"


def unseal(bank_id: str) -> str:
    f = Fernet(_key())
    blob = (SEALS / f"{bank_id}.bin").read_bytes()
    return f.decrypt(blob).decode("utf-8")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "unseal":
        print(unseal(sys.argv[2]))
    else:
        print("用法: seal_gold.py unseal <bank_id>")
