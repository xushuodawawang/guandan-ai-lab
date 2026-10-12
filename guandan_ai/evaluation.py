import csv
import hashlib
import json
import math
import random
import statistics
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .cards import deck
from .logging import JsonlLogger
from .models import Observation
from .platform import REVISION, ROOT, WELKIN_REVISION
from .belief import PublicHistory
from .policies import PolicyConfig, decide_safely, make_policy
from .rules import JavaRules, load_environment


def normalize_result(msg):
    if "victory" in msg:
        winner = int(msg["victory"])
        if winner not in (0, 1):
            raise ValueError("未知 victory 团队编码")
        return {"winner": winner, "basis": "official victory", "raw": msg}
    victories = msg.get("victoryNum")
    if isinstance(victories, list) and len(victories) == 4 and isinstance(msg.get("draws"), list) and len(msg["draws"]) == 4:
        a, b = victories[0] + victories[2], victories[1] + victories[3]
        return {"winner": None if a == b else int(b > a), "basis": "official partnership tally", "raw": msg}
    raise ValueError("未知结算结构，不能推断完整比赛胜负")


def episode_result(msg):
    order = msg["order"]
    if len(order) != 4 or sorted(order) != [0, 1, 2, 3]:
        raise ValueError("小局结束名次不完整")
    partner = order.index((order[0] + 2) % 4)
    return {"winner": order[0] % 2, "upgrade": 3 if partner == 1 else 2 if partner == 2 else 1,
            "order": order, "trump": msg["curRank"]}


def run_one(rules, strategy_a="hybrid", strategy_b="greedy", seed=1, swap=False,
            mode="episode", budget_ms=150, log_path=None, config=None, max_steps=15000, strict_budget=False):
    Environment = load_environment(rules)
    random_state = random.getstate()
    random.seed(seed)
    shoe = deck()
    random.Random(seed).shuffle(shoe)
    # Construct with a complete internal deck. Upstream deck_data constructor leaves
    # __cards empty, which breaks the next episode's reshuffle; do not modify upstream.
    env = Environment(first_player=seed % 4)
    types = __import__(Environment.__module__.rsplit(".", 1)[0] + ".types", fromlist=["parse"])
    env.deck_data, env.deck = shoe[:], types.parse(shoe)
    team_a = int(swap)
    names = [strategy_a if i % 2 == team_a else strategy_b for i in range(4)]
    if strict_budget:
        from .worker import ProcessPolicy
        policies = [ProcessPolicy(names[i], i, seed * 101 + i, config) for i in range(4)]
        try:
            for policy in policies:
                policy.start()
        except Exception:
            for policy in policies:
                policy.close()
            raise
    else:
        policies = [make_policy(names[i], i, seed * 101 + i, rules, config) for i in range(4)]
    histories = [PublicHistory(i) for i in range(4)]
    for i in range(4):
        env.add_player(f"{names[i]}-{i}", i)
    started = time.perf_counter()
    decisions, episodes = [], []
    outcome, error, pending = None, None, None
    path = Path(log_path or ROOT / "data/runs/single.jsonl")
    with JsonlLogger(path) as logger:
        logger.write("match_start", mode=mode, seed=seed, swap=swap, team_a=team_a,
                     strategies=names, platform_revision=REVISION, budget_ms=budget_ms,
                     strict_budget=strict_budget, baseline_revision=WELKIN_REVISION if "welkin" in names else None,
                     config=asdict(config or PolicyConfig()),
                     initial_deck_sha256=hashlib.sha256(json.dumps(shoe).encode()).hexdigest())
        try:
            pending = env.start()
            for step in range(max_steps):
                request = None
                for packet in pending:
                    msg, seat = packet.body, packet.seat
                    histories[seat].observe(msg)
                    if callable(getattr(policies[seat], "observe", None)):
                        policies[seat].observe(msg)
                    if msg["type"] == "notify":
                        if msg["stage"] == "beginning":
                            logger.write("begin", recipient=seat, message=msg, episode=len(episodes))
                        elif seat == 0:
                            logger.write("public_event", message=msg, episode=len(episodes))
                            if msg["stage"] == "episodeOver":
                                episodes.append(episode_result(msg))
                            elif msg["stage"] == "gameResult":
                                outcome = normalize_result(msg)
                    elif msg["type"] == "act":
                        request = (seat, msg)
                if mode == "episode" and episodes:
                    outcome = {"winner": episodes[-1]["winner"], "basis": "episode head partnership"}
                    break
                if outcome is not None:
                    break
                if request:
                    seat, msg = request
                    obs = Observation.from_message(msg, seat, histories[seat].finished)
                    if strict_budget and policies[seat].process is None:
                        policies[seat].start()
                    decision = decide_safely(policies[seat], obs, budget_ms)
                    decisions.append(decision)
                    logger.write("decision", seat=seat, strategy=names[seat], observation=asdict(obs),
                                 decision=decision.to_dict(), action=obs.actions[decision.index], episode=len(episodes))
                    pending = env.loop({"actIndex": decision.index})
                else:
                    pending = env.loop()
            if outcome is None:
                error = f"超过最大 {max_steps} 步，比赛未完成"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.write("error", message=error)
        finally:
            random.setstate(random_state)
            if strict_budget:
                for policy in policies:
                    policy.close()
        times = [d.elapsed_ms for d in decisions]
        from .llm import model_statistics
        result = {"seed": seed, "swap": swap, "team_a": team_a, "strategy_a": strategy_a,
                  "strategy_b": strategy_b, "mode": mode, "status": "finished" if outcome is not None else "error",
                  "winner": outcome["winner"] if outcome else None,
                  "a_score": (0.5 if outcome["winner"] is None else float(outcome["winner"] == team_a)) if outcome else None,
                  "result_basis": outcome["basis"] if outcome else None,
                  "episodes": len(episodes), "steps": len(decisions), "fallbacks": sum(d.fallback for d in decisions),
                  "over_budget": sum(t > budget_ms for t in times),
                  "mean_ms": statistics.mean(times) if times else 0,
                  "p95_ms": sorted(times)[min(len(times) - 1, math.ceil(len(times) * .95) - 1)] if times else 0,
                  "elapsed_sec": time.perf_counter() - started, "error": error,
                  "log": str(path), "episode_results": episodes, "model_stats": model_statistics(decisions)}
        logger.write("match_end", result=result)
    return result


def confidence_interval(pair_scores, seed=0):
    if len(pair_scores) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(pair_scores, k=len(pair_scores))) / len(pair_scores) for _ in range(2000))
    return [means[49], means[1949]]


def summarize(results):
    finished = [r for r in results if r["status"] == "finished"]
    grouped = {}
    for result in finished:
        grouped.setdefault(result["seed"], []).append(result)
    complete_pairs = [statistics.mean(r["a_score"] for r in group) for group in grouped.values()
                      if len(group) == 2 and {r["swap"] for r in group} == {False, True}]
    return {"matches": len(results), "completed": len(finished), "failed": len(results) - len(finished),
            "wins_a": sum(r["a_score"] == 1 for r in finished), "draws": sum(r["a_score"] == .5 for r in finished),
            "score_rate_a": statistics.mean(r["a_score"] for r in finished) if finished else None,
            "complete_pairs": len(complete_pairs), "paired_score_rate_a": statistics.mean(complete_pairs) if complete_pairs else None,
            "paired_bootstrap_95ci": confidence_interval(complete_pairs),
            "fallbacks": sum(r["fallbacks"] for r in results), "over_budget": sum(r["over_budget"] for r in results),
            "decisions": sum(r["steps"] for r in results)}


def evaluate(strategy_a="hybrid", strategy_b="greedy", pairs=4, seed=1, mode="episode", budget_ms=150, output=None, config=None, strict_budget=False):
    if pairs < 1 or budget_ms <= 0 or mode not in ("episode", "match"):
        raise ValueError("pairs/budget 必须为正，mode 必须为 episode 或 match")
    output = Path(output or ROOT / "data/runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with JavaRules() as rules:
        rules.legal(["S3"], "2")  # warm the JVM outside decision timing
        for number in range(seed, seed + pairs):
            for swap in (False, True):
                result = run_one(rules, strategy_a, strategy_b, number, swap, mode, budget_ms,
                                 output / f"seed-{number}-swap-{int(swap)}.jsonl", config, strict_budget=strict_budget)
                results.append(result)
                print(f"seed={number} swap={int(swap)} {result['status']} A={result['a_score']} episodes={result['episodes']}", flush=True)
    summary = {"strategy_a": strategy_a, "strategy_b": strategy_b, "mode": mode,
               "platform_revision": REVISION, "budget_ms": budget_ms, "seed": seed,
               "strict_budget": strict_budget, "baseline_revision": WELKIN_REVISION if "welkin" in (strategy_a, strategy_b) else None,
               "config": asdict(config or PolicyConfig()), **summarize(results), "results": results}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "matches.csv").open("w", encoding="utf-8-sig", newline="") as f:
        columns = [k for k in results[0] if k != "episode_results"]
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows({k: r[k] for k in columns} for r in results)
    write_report(summary, output / "report.md")
    return summary, output


def write_report(summary, path):
    rate = summary.get("score_rate_a")
    ci = summary.get("paired_bootstrap_95ci")
    mode_name = {"episode": "单小局（不代表完整比赛胜率）", "match": "完整比赛",
                 "session": "线上多场会话（仅报告最终平台结果，不推断中间比赛胜负）"}.get(summary["mode"], summary["mode"])
    lines = ["# 掼蛋 AI 评测报告", "", f"- 比较：`{summary['strategy_a']}` 对 `{summary['strategy_b']}`",
        f"- 评测层级：{mode_name}", f"- 官方环境版本：`{summary['platform_revision']}`",
        f"- 决策预算：{summary['budget_ms']} ms（{'进程隔离，预算 + 80ms IPC 等待后终止，清理时间额外记录' if summary.get('strict_budget') else '离线软预算，实际超预算另计'}）",
        f"- 已完成 / 总数：{summary['completed']} / {summary['matches']}，失败 {summary['failed']}",
        f"- A 胜场：{summary['wins_a']}，平局：{summary['draws']}",
        f"- A 得分率（平局 0.5）：{rate:.1%}" if rate is not None else "- 无有效结果",
        f"- 完整配对组：{summary['complete_pairs']}；95% 配对 bootstrap 区间：{ci if ci else '样本不足'}",
        f"- 异常回退：{summary['fallbacks']}；超软预算决策：{summary['over_budget']} / {summary['decisions']}",
        "", "## 逐场数据", "", "| seed | 换队 | 状态 | A 得分 | 小局数 | 平均 ms | P95 ms |", "|---|---|---|---|---|---|---|"]
    for r in summary["results"]:
        lines.append(f"| {r['seed']} | {r['swap']} | {r['status']} | {r['a_score']} | {r['episodes']} | {r['mean_ms']:.2f} | {r['p95_ms']:.2f} |")
    if any(r.get("model_stats", {}).get("model_decisions") for r in summary["results"]):
        lines += ["", "## 大模型调用", "", "| seed | 换队 | 模型回合 | 已记录请求 | 模型回退 | tokens | 平均 ms | P95 ms | 估算金额 |", "|---|---|---|---|---|---|---|---|---|"]
        for r in summary["results"]:
            m = r.get("model_stats", {})
            lines.append(f"| {r['seed']} | {r['swap']} | {m.get('model_decisions', 0)} | {m.get('model_requests', 0)} | {m.get('model_fallbacks', 0)} | {m.get('usage', {}).get('total_tokens', 0)} | {m.get('model_mean_ms')} | {m.get('model_p95_ms')} | {m.get('estimated_cost')} |")
        lines += ["", "模型、提示词版本和用量详见 JSON 及逐场日志。超时而未收到供应商用量的调用无法准确计价；估算金额按用户配置的价格计算，不替代供应商账单。", ""]
    pairing_note = "每组使用相同起始牌堆与首家并交换双方队伍。区间按完整配对组抽样，不把同一比赛的小局当成独立样本。" if summary.get("paired_deals", True) else "本结果来自线上服务器，未控制发牌或同牌配对。seed 是策略种子，不能当作服务器发牌种子。耗时只涵盖已记录客户端。"
    lines += ["", "## 解释与限制", "", pairing_note,
              "该结果只代表此版本、对手、样本与预算；小样本开发评测不构成棋力结论。官方离线环境与线上服务器仍须分别做规则契约验证。",
              "末局搜索基于未知牌抽样、候选裁剪和有限深度，不能保证不完全信息下最优。同学策略未提供时，不能把内置弱基线当作已完成同学 PK。", ""]
    if summary.get("baseline_revision"):
        lines += ["## 已有 AI 来源与适配", "", f"对照使用 [welkin03/guandan-ai](https://github.com/welkin03/guandan-ai) 的 MIT 启发式 `heuristic_policy`，固定版本 `{summary['baseline_revision']}`。",
                  "根动作改由比赛平台提供，大小王和序列主点数进行了编码映射；其他手牌只提供长度，禁止读取牌值。未启用其额外全信息 guard；进贡/还贡采用本项目适配。缓存不改变原评分。",
                  "该对照是已发布启发式算法的适配版，不代表原作者完整研究栈或神经模型。软预算测试应结合超预算与耗时分析，正式公平 PK 建议开启 --strict-budget。", ""]
    if plot_report(summary, Path(path).with_suffix(".svg")):
        lines += ["## 图表", "", f"![比赛结果与决策耗时]({Path(path).with_suffix('.svg').name})", ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def write_online_summary(log_paths, strategies, output, url, budget_ms=200, seed=1, rounds=1, team_a=0):
    from .logging import read_jsonl
    events = [e for path in log_paths for e in read_jsonl(path)]
    outcomes = [e["result"] for e in events if e["kind"] == "result"]
    if not outcomes or len({o["winner"] for o in outcomes}) != 1:
        raise ValueError("在线结果缺失或客户端结算不一致")
    decisions = [e for e in events if e["kind"] == "decision"]
    # Public notifications are duplicated across recipients; use one perspective.
    first_events = read_jsonl(log_paths[0])
    episodes = [episode_result(e["message"]) for e in first_events
                if e["kind"] == "received" and e["message"].get("stage") == "episodeOver"]
    times = [e["decision"]["elapsed_ms"] for e in decisions]
    winner = outcomes[0]["winner"]
    label = lambda parity: "+".join(dict.fromkeys(strategies[i] for i in range(4) if i % 2 == parity))
    result = {"seed": seed, "swap": False, "team_a": team_a, "strategy_a": label(team_a),
        "strategy_b": label(1 - team_a), "mode": "match" if rounds == 1 else "session", "status": "finished",
        "winner": winner, "a_score": .5 if winner is None else float(winner == team_a),
        "result_basis": outcomes[0]["basis"], "episodes": len(episodes), "steps": len(decisions),
        "fallbacks": sum(e["decision"].get("fallback", False) for e in decisions),
        "over_budget": sum(t > budget_ms for t in times), "mean_ms": statistics.mean(times) if times else 0,
        "p95_ms": sorted(times)[max(0, math.ceil(len(times) * .95) - 1)] if times else 0,
        "elapsed_sec": max(e["time"] for e in events) - min(e["time"] for e in events),
        "error": None, "log": str(log_paths[0]), "episode_results": episodes}
    summary = {"strategy_a": result["strategy_a"], "strategy_b": result["strategy_b"], "mode": result["mode"],
        "platform_revision": "online server; version not advertised", "url": url, "rounds": rounds,
        "budget_ms": budget_ms, "seed": seed, "strict_budget": True, "paired_deals": False,
        "baseline_revision": WELKIN_REVISION if "welkin" in strategies else None,
        **summarize([result]), "results": [result]}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = [k for k in result if k != "episode_results"]
    with (output / "matches.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerow({k: result[k] for k in columns})
    write_report(summary, output / "report.md")
    return summary


def plot_report(summary, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    results = summary["results"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.7), layout="constrained")
    rate = summary.get("score_rate_a")
    axes[0].bar(["Team A", "Team B"], [rate or 0, 1 - rate if rate is not None else 0], color=["#17634a", "#a6b6ae"])
    axes[0].set_ylim(0, 1)
    axes[0].set_ylabel("Score rate (draw = 0.5)")
    axes[0].set_title(f"{summary['mode']} | completed {summary['completed']}/{summary['matches']}")
    axes[1].plot(range(1, len(results) + 1), [r["mean_ms"] for r in results], marker="o", label="Mean, all seats")
    axes[1].plot(range(1, len(results) + 1), [r["p95_ms"] for r in results], marker="o", label="P95, all seats")
    axes[1].axhline(summary["budget_ms"], color="#b72c3b", linestyle="--", label="Decision target")
    axes[1].set_xlabel("Run")
    axes[1].set_ylabel("Decision latency (ms)")
    axes[1].legend(fontsize=8)
    fig.suptitle(f"{summary['strategy_a']} vs {summary['strategy_b']} (development sample)", fontsize=12)
    fig.savefig(path, format="svg")
    plt.close(fig)
    return True

