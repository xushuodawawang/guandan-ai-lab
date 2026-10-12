"""Load server settings from a private local file without shell evaluation."""

import os
from pathlib import Path


def load_local_env(path=None):
    if path is None:
        from .platform import ROOT
        path = ROOT / ".env"
    path = Path(path)
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key.isidentifier():
            raise ValueError(f"本地 .env 第 {number} 行格式无效")
        if not key.startswith(("DEEPSEEK_", "GUANDAN_")):
            continue
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"本地 .env 第 {number} 行引号未闭合")
            value = value[1:-1]
        os.environ.setdefault(key, value)
