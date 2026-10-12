import json
import time
from pathlib import Path


class JsonlLogger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("w", encoding="utf-8")
        self.seq = 0

    def write(self, kind, **data):
        self.seq += 1
        event = {"seq": self.seq, "time": time.time(), "kind": kind, **data}
        self.file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def read_jsonl(source):
    if hasattr(source, "read"):
        text = source.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8")
    else:
        text = Path(source).read_text(encoding="utf-8")
    events = []
    for line in text.splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict) or "kind" not in value:
                raise ValueError("日志每行必须为含 kind 的 JSON 对象")
            events.append(value)
    return events

