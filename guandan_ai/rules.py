import importlib.util
import itertools
import json
import queue
import subprocess
import sys
import threading
from functools import lru_cache

from .cards import hand_key, wire_matrix
from .models import Action, is_pass
from .platform import FILES, RUNTIME, blob_hash, find_java


class RulesUnavailable(RuntimeError):
    pass


class JavaRules:
    """Persistent official Java worker, serialized requests and a hard IPC timeout."""

    def __init__(self, timeout: float = 5):
        self.timeout = timeout
        self.proc = None
        self.lock = threading.RLock()
        self.responses = queue.Queue()
        self.ids = itertools.count(1)

    def _start(self):
        jar = RUNTIME / "guandan-java/guandan-java-action.jar"
        if not jar.is_file():
            raise RulesUnavailable("请先运行 python -m guandan_ai fetch-platform")
        if blob_hash(jar.read_bytes()) != FILES["guandan-java/guandan-java-action.jar"]:
            raise RulesUnavailable("规则引擎文件校验失败，请重新下载")
        self.responses = queue.Queue()
        self.proc = subprocess.Popen([find_java(), "-jar", str(jar), "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        proc, responses = self.proc, self.responses

        def read_lines():
            try:
                for line in proc.stdout:
                    responses.put(line)
            finally:
                responses.put(None)
        threading.Thread(target=read_lines, daemon=True).start()

    def invoke(self, payload: dict) -> list[Action]:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            rid = next(self.ids)
            try:
                self.proc.stdin.write(json.dumps({**payload, "id": rid}, separators=(",", ":")) + "\n")
                self.proc.stdin.flush()
                line = self.responses.get(timeout=self.timeout)
                if line is None:
                    raise RulesUnavailable("规则引擎进程意外结束")
                result = json.loads(line)
                if result.get("id") != rid or not result.get("ok") or not isinstance(result.get("actions"), list):
                    raise RulesUnavailable(f"规则引擎响应错误: {result}")
                return result["actions"]
            except (queue.Empty, BrokenPipeError, OSError, json.JSONDecodeError) as exc:
                self.close()
                raise RulesUnavailable(f"规则引擎调用失败或超时: {exc}") from exc

    @lru_cache(maxsize=12000)
    def _legal_cached(self, hand: tuple[str, ...], trump: str, greater: str):
        payload = {"mode": "first" if not greater else "second", "cards": wire_matrix(hand),
                   "heartsNum": hand.count("H" + trump), "currentRank": trump}
        if greater:
            payload["greaterAction"] = json.loads(greater)
        return self.invoke(payload)

    def legal(self, hand, trump: str, greater: Action | None = None) -> list[Action]:
        if not hand:
            return []
        key = "" if is_pass(greater) else json.dumps(greater, separators=(",", ":"))
        return self._legal_cached(hand_key(hand), trump, key)

    def close(self):
        with self.lock:
            if self.proc:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=2)
                for stream in (self.proc.stdin, self.proc.stdout):
                    if stream:
                        stream.close()
            self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def load_environment(rules: JavaRules):
    package = RUNTIME / "guandan-java/engine"
    for path, digest in FILES.items():
        if path.endswith(".py"):
            p = RUNTIME / path
            if not p.is_file() or blob_hash(p.read_bytes()) != digest:
                raise RulesUnavailable("官方 Python 环境缺失或校验失败，请重新 fetch-platform")
    name = "_official_openguandan_engine"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, package / "__init__.py", submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    sys.modules[name + ".moves"]._J = rules
    return sys.modules[name + ".environment"].Environment

