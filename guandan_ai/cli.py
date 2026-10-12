import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from .analysis import HandAnalyzer, features
from .cards import validate_hand
from .evaluation import evaluate, write_report, write_online_summary
from .platform import ROOT, RUNTIME, REVISION, SERVER_FILES, blob_hash, download_platform, download_baseline, find_java
from .policies import PolicyConfig
from .rules import JavaRules


def read_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("策略配置必须是 JSON 对象")
    try:
        return PolicyConfig(**data)
    except TypeError as exc:
        raise ValueError(f"策略配置字段不正确: {exc}") from exc


def main(argv=None):
    # Redirected Windows consoles can default to cp1252; CLI output is UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    from .local_config import load_local_env
    load_local_env()
    parser = argparse.ArgumentParser(description="OpenGuanDan Python AI 实验台")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-platform", help="下载并校验官方运行时（不写入 Git）")
    fetch.add_argument("--server", action="store_true", help="同时下载当前系统的官方比赛服务器")
    sub.add_parser("doctor", help="检查 Java 和动作引擎")
    sub.add_parser("fetch-baseline", help="下载并校验 MIT 开源启发式 AI 对照")
    analyze = sub.add_parser("analyze", help="分析手牌与合法分解")
    analyze.add_argument("cards", nargs="+", help="如 S3 H3 C4；10 写 T，小王 SB，大王 HR")
    analyze.add_argument("--trump", default="2", choices=list("23456789TJQKA"))
    analyze.add_argument("--budget", type=float, default=300)
    for name in ("evaluate", "ablate"):
        p = sub.add_parser(name, help="固定发牌换队评测" if name == "evaluate" else "手牌/行为/搜索消融")
        p.add_argument("--a", default="hybrid")
        p.add_argument("--b", default="greedy")
        p.add_argument("--pairs", type=int, default=4)
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--mode", choices=["episode", "match"], default="episode")
        p.add_argument("--budget", type=float, default=150)
        p.add_argument("--output")
        p.add_argument("--samples", type=int, default=3)
        p.add_argument("--search-cards", type=int, default=20)
        p.add_argument("--config", help="PolicyConfig JSON 文件；指定时覆盖算法参数默认值")
        p.add_argument("--strict-budget", action="store_true", help="双方策略均使用可终止的独立进程")
    for name in ("client", "arena"):
        p = sub.add_parser(name, help="在线单客户端" if name == "client" else "创建四 AI 在线比赛")
        p.add_argument("--url", default="ws://127.0.0.1:8181")
        p.add_argument("--rounds", type=int, default=1)
        p.add_argument("--budget", type=float, default=200)
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--timeout", type=float, default=1800)
        p.add_argument("--output", default=f"data/runs/{name}")
        p.add_argument("--config", help="PolicyConfig JSON 文件")
        if name == "client":
            p.add_argument("--strategy", default="hybrid")
            p.add_argument("--seat", type=int, choices=range(4), default=0)
            p.add_argument("--room", type=int)
        else:
            p.add_argument("--strategies", nargs=4, default=["hybrid", "greedy", "hybrid", "greedy"])
    report = sub.add_parser("report", help="从 summary.json 重新生成报告")
    report.add_argument("summary")
    report.add_argument("--output", default="reports/report.md")
    sub.add_parser("ui", help="启动 Streamlit 分析界面")
    play = sub.add_parser("play", help="启动 HTML/CSS/JavaScript 真人牌桌")
    play.add_argument("--port", type=int, default=8502)
    play.add_argument("--host", default="127.0.0.1", help="局域网模式用 0.0.0.0")
    play.add_argument("--ws-port", type=int, default=8503)
    play.add_argument("--store", help="SQLite 牌局检查点路径")
    play.add_argument("--turn-timeout", type=float, default=90, help="真人回合秒数，超时后托管")
    server = sub.add_parser("server", help="在当前终端运行已下载的官方服务器")
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch-platform":
            print(json.dumps(download_platform(args.server), ensure_ascii=False, indent=2))
        elif args.command == "fetch-baseline":
            print(json.dumps(download_baseline(), ensure_ascii=False, indent=2))
        elif args.command == "doctor":
            print(f"Java: {find_java()}\n平台版本: {REVISION}")
            with JavaRules() as rules:
                moves = rules.legal(["S3", "S3", "H2", "C4", "D5", "S6", "H7"], "2")
                print(f"官方动作引擎正常：{len(moves)} 个合法动作")
        elif args.command == "analyze":
            hand = validate_hand(args.cards)
            with JavaRules() as rules:
                rules.legal(["S3"], "2")
                plans = HandAnalyzer(rules).decompose(hand, args.trump, args.budget)
                print(json.dumps({"features": features(hand, args.trump), "plans": [p.to_dict() for p in plans]}, ensure_ascii=False, indent=2))
        elif args.command in ("evaluate", "ablate"):
            config = read_config(args.config) if args.config else PolicyConfig(samples=args.samples, search_cards=args.search_cards)
            options = dict(strategy_b=args.b, pairs=args.pairs, seed=args.seed, mode=args.mode,
                           budget_ms=args.budget, config=config, strict_budget=args.strict_budget)
            if args.command == "evaluate":
                summary, path = evaluate(strategy_a=args.a, output=args.output, **options)
                print(f"报告: {path / 'report.md'}")
                if summary["failed"]:
                    raise RuntimeError(f"{summary['failed']} 场未完成，详见日志")
            else:
                from datetime import datetime
                output = Path(args.output or ROOT / "data/runs" / ("ablation-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
                data = []
                for variant in ("hybrid", "no-structure", "no-belief", "no-search"):
                    summary, _ = evaluate(strategy_a=variant, output=output / variant, **options)
                    data.append({k: v for k, v in summary.items() if k != "results"})
                (output / "ablation.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"消融结果: {output / 'ablation.json'}")
                if any(row["failed"] for row in data):
                    raise RuntimeError("消融中存在未完成比赛，详见各组日志")
        elif args.command in ("client", "arena"):
            from .client import arena, play_client
            if args.budget <= 0 or args.rounds <= 0:
                raise ValueError("预算与比赛数必须为正")
            config = read_config(args.config) if args.config else None
            options = dict(url=args.url, rounds=args.rounds, budget_ms=args.budget, seed=args.seed, timeout=args.timeout, config=config)
            if args.command == "arena":
                result = asyncio.run(arena(strategies=args.strategies, output=args.output, **options))
            else:
                result = asyncio.run(play_client(strategy=args.strategy, seat=args.seat, room_id=args.room,
                                                log_path=Path(args.output) / "client.jsonl", **options))
                strategies = ["external"] * 4
                strategies[args.seat] = args.strategy
                write_online_summary([Path(args.output) / "client.jsonl"], strategies, args.output, args.url,
                                     args.budget, args.seed, args.rounds, args.seat % 2)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "report":
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            write_report(json.loads(Path(args.summary).read_text(encoding="utf-8")), args.output)
            print(args.output)
        elif args.command == "play":
            from .web_server import serve
            serve(args.port, args.host, args.ws_port, args.store, args.turn_timeout)
        elif args.command == "ui":
            subprocess.run([sys.executable, "-m", "streamlit", "run", str(ROOT / "streamlit_app.py"),
                            "--server.address", "127.0.0.1", "--browser.gatherUsageStats", "false"], cwd=ROOT, check=True)
        elif args.command == "server":
            import os
            exe = RUNTIME / ("guandan.exe" if os.name == "nt" else "guandan-linux")
            if not exe.is_file():
                raise RuntimeError("先运行 python -m guandan_ai fetch-platform --server")
            if blob_hash(exe.read_bytes()) != SERVER_FILES[exe.name]:
                raise RuntimeError("官方服务器校验失败，请重新 fetch-platform --server")
            subprocess.run([str(exe)], cwd=RUNTIME, check=True)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"错误: {exc}\n")


if __name__ == "__main__":
    main()

