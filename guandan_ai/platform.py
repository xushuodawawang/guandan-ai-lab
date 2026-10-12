"""Download the course-provided platform into a separate ignored runtime directory."""
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "openguandan"
REVISION = "52e473a5b7f5e90df47b1564416a48ec4ac5ef5b"
REPOSITORY = "GameAI-NJUPT/OpenGuanDan"
FILES = {
    "guandan-java/engine/__init__.py": "afa39499173f2786d3aa7f569354a7e3515f50fb",
    "guandan-java/engine/environment.py": "b77f37e1053dc70b01465a7a5d067f4d1df1860f",
    "guandan-java/engine/moves.py": "a94a893798b020cd1afaa91c055b918f20b482f5",
    "guandan-java/engine/table.py": "eeb288664a5006d3111d9e1bb378b34d5af1bb65",
    "guandan-java/engine/types.py": "99483111b272c19a409720136eb7d610dd0cb3e9",
    "guandan-java/guandan-java-action.jar": "4b552f7e76c364abc0aa055c31dbc9346f67b4c1",
}
SERVER_FILES = {"guandan.exe": "7d9c9de18f0c0ea235913294ed16186f8e8dc5bc",
                "guandan-linux": "6334fc8db762ad13a61c82ced0e3ad5b207e1548"}
WELKIN_REVISION = "3ab3523abbca2c703d5c0e421b85b23214292e91"
WELKIN_ROOT = ROOT / ".runtime" / "welkin-guandan-ai"
WELKIN_FILES = {
    "LICENSE": "1f4a3585df123c57a4fb00a690d5ae11a59d0c0f",
    "src/guandan/__init__.py": "c3af794cf69b67c1a097ae47982dc83fa09628bb",
    "src/guandan/cards.py": "8d418d60257e914913c798379a88f1ea2a4559de",
    "src/guandan/hand_eval.py": "dbeafc3c5488b16b39f0d230d51c2ee4e7970aeb",
    "src/guandan/policy.py": "dc0002b255a0a9a6c58ff91eaa09966c881980a9",
    "src/guandan/rules.py": "711c34764926c427e2c1401d8a299ab4c1feb5ed",
    "src/guandan/state.py": "bd18ea9c899d23cd88faa92950cb026c1b887f8b",
}


def blob_hash(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def download_platform(server: bool = False) -> dict:
    files = dict(FILES)
    if server:
        name = "guandan.exe" if os.name == "nt" else "guandan-linux"
        files[name] = SERVER_FILES[name]
    entries = []
    for path, digest in files.items():
        target = RUNTIME / path
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{REVISION}/{path}"
        if not target.is_file() or blob_hash(target.read_bytes()) != digest:
            print(f"下载官方平台: {path}", flush=True)
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Guandan-AI-Lab"}), timeout=90) as response:
                data = response.read()
            if blob_hash(data) != digest:
                raise RuntimeError(f"官方文件校验失败: {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(target.suffix + ".download")
            temp.write_bytes(data)
            temp.replace(target)
            if path == "guandan-linux":
                target.chmod(0o755)
        entries.append({"path": path, "git_blob_sha1": digest, "url": url,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    manifest = {"repository": REPOSITORY, "revision": REVISION,
                "license": "No explicit LICENSE found; course-provided runtime, not redistributed.",
                "files": entries}
    RUNTIME.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def find_java() -> str:
    override = os.environ.get("GUANDAN_JAVA")
    if override:
        return override
    java_home = os.environ.get("JAVA_HOME")
    candidates = []
    if java_home:
        candidates.append(Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java"))
    if os.name == "nt":
        candidates.extend(sorted(Path("C:/Program Files/Java").glob("jdk*/bin/java.exe"), reverse=True))
    candidates += [Path(shutil.which("java"))] if shutil.which("java") else []
    for p in candidates:
        try:
            subprocess.run([str(p), "-version"], capture_output=True, timeout=5, check=True)
            return str(p)
        except (OSError, subprocess.SubprocessError):
            continue
    raise RuntimeError("没有可运行的 JDK 17+；设置 GUANDAN_JAVA 为 java 的完整路径")


def download_baseline():
    entries = []
    for path, digest in WELKIN_FILES.items():
        target = WELKIN_ROOT / path
        url = f"https://raw.githubusercontent.com/welkin03/guandan-ai/{WELKIN_REVISION}/{path}"
        if not target.is_file() or blob_hash(target.read_bytes()) != digest:
            print(f"下载 MIT 开源对照算法: {path}", flush=True)
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Guandan-AI-Lab"}), timeout=90) as response:
                data = response.read()
            if blob_hash(data) != digest:
                raise RuntimeError(f"开源对照文件校验失败: {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        entries.append({"path": path, "git_blob_sha1": digest, "url": url})
    manifest = {"repository": "welkin03/guandan-ai", "revision": WELKIN_REVISION, "license": "MIT", "files": entries}
    (WELKIN_ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

