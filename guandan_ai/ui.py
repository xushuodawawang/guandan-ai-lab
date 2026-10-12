import html
import json
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from .analysis import HandAnalyzer, features
from .cards import format_card, validate_hand
from .logging import read_jsonl
from .platform import ROOT, RUNTIME
from .replay import history_at, replay_snapshot
from .rules import JavaRules

STRATEGIES = ["hybrid", "team", "greedy", "random", "welkin", "no-structure", "no-belief", "no-search"]
NAMES = {"hybrid": "综合 AI", "team": "团队启发式", "greedy": "贪心基线", "random": "随机基线",
         "welkin": "开源启发式 AI（适配）", "no-structure": "关闭手牌分解", "no-belief": "关闭行为推断", "no-search": "关闭末局搜索"}


@st.cache_resource
def resources():
    rules = JavaRules()
    rules.legal(["S3"], "2")
    return rules, HandAnalyzer(rules)


def cards_view(cards):
    pieces = []
    for card in cards:
        red = card.startswith(("H", "D"))
        pieces.append(f'<span class="playing-card {"red-card" if red else "black-card"}">{html.escape(format_card(card))}</span>')
    st.markdown('<div class="card-hand">' + "".join(pieces) + '</div>', unsafe_allow_html=True)


def log_picker(key):
    paths = sorted((ROOT / "data").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if (ROOT / "data").exists() else []
    upload = st.file_uploader("导入 JSONL 比赛日志", type=["jsonl"], key=key + "_upload")
    if upload:
        return read_jsonl(upload)
    if not paths:
        st.info("还没有比赛日志。先在 PK 统计页运行一次离线比赛。")
        return None
    path = st.selectbox("比赛日志", paths, format_func=lambda p: str(p.relative_to(ROOT)), key=key + "_path")
    return read_jsonl(path)


def analysis_page():
    st.title("把一手牌，拆成可执行的计划")
    st.write("比较剩余手牌结构与合法分解。小手牌在预算内可做精确搜索，大手牌采用有界近似。")
    with st.form("hand_form"):
        text = st.text_area("手牌编码", "S3 H3 C3 D4 S4 H5 C6 D7 S8 H9 CT DJ HQ SK HA", help="空格或逗号分隔；10 写 T，小王 SB，大王 HR。相同牌最多两张。")
        col1, col2 = st.columns(2)
        trump = col1.selectbox("当前级牌", list("23456789TJQKA"))
        budget = col2.slider("分析预算（毫秒）", 50, 1000, 300, step=50)
        submitted = st.form_submit_button("分析手牌", use_container_width=True)
    if submitted:
        try:
            hand = validate_hand(text.replace(",", " ").replace("，", " ").upper().split())
            if not hand or len(hand) > 27:
                raise ValueError("请输入 1–27 张有效手牌")
            rules, analyzer = resources()
            plans = analyzer.decompose(hand, trump, budget, alternatives=3)
            st.session_state["analysis_result"] = (hand, trump, [p.to_dict() for p in plans], len(rules.legal(hand, trump)))
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))
    if "analysis_result" not in st.session_state:
        cards_view("S3 H3 C3 D4 S4 H5 C6 D7 S8 H9 CT DJ HQ SK HA".split())
        st.caption("先选择级牌，再开始分析；这里的示例牌尚未执行分析。")
        return
    hand, trump, plans, legal_count = st.session_state["analysis_result"]
    cards_view(hand)
    f = features(hand, trump)
    cols = st.columns(4)
    cols[0].metric("手牌", len(hand))
    cols[1].metric("合法领出动作", legal_count)
    cols[2].metric("最佳分解手数", plans[0]["turns"])
    cols[3].metric("红桃级牌", f["wild_cards"])
    st.caption(f"当前级牌 {trump} · {'已证明最少手数' if plans[0]['exact'] else '预算内近似分解，未证明最优'} · {plans[0]['elapsed_ms']:.1f} ms")
    for i, plan in enumerate(plans):
        with st.expander(f"方案 {i + 1}：{plan['turns']} 手", expanded=i == 0):
            for group in plan["groups"]:
                st.write(f"{group[0]} · {group[1]}")
                cards_view(group[2])


def replay_page():
    st.title("逐步复盘，一次选择的来由")
    events = log_picker("replay")
    if not events:
        return
    seat = st.selectbox("观察座位", [0, 1, 2, 3], format_func=lambda s: f"座位 {s} · 队友 {(s + 2) % 4}")
    reveal = st.checkbox("显示所有已记录手牌（赛后裁判复盘）", value=False)
    cursor = st.slider("日志事件位置", 0, max(0, len(events) - 1), max(0, len(events) - 1), key="replay_cursor") if len(events) > 1 else 0
    snap = replay_snapshot(events, cursor, seat)
    st.caption(f"级牌 {snap['trump']} · 当前领先座位 {snap['leader']} · 已完成顺序 {snap['order']}")
    columns = st.columns(4)
    for player, col in enumerate(columns):
        with col:
            st.subheader(f"座位 {player}" + (" · 本人" if player == seat else " · 队友" if player == (seat + 2) % 4 else ""))
            st.metric("剩余张数", snap["rest"][player])
            if player == seat or reveal:
                cards_view(snap["hands"][player])
            else:
                st.caption("手牌隐藏")
    decision = snap["decision"]
    if decision:
        st.subheader("最近一次 AI 决策")
        st.write(decision["decision"]["reason"])
        st.caption(f"策略 {decision['strategy']} · 耗时 {decision['decision']['elapsed_ms']:.2f} ms")
        scores = decision["decision"].get("scores", [])
        if scores:
            st.dataframe(pd.DataFrame([{ "动作": " ".join(format_card(c) for c in r["action"][2]) if isinstance(r["action"][2], list) else "PASS",
                "牌型": r["action"][0], "评分": r["score"], "说明": "；".join(r["reasons"])} for r in scores]), use_container_width=True, hide_index=True)
        if decision["decision"].get("search"):
            with st.expander("末局搜索详情"):
                st.json(decision["decision"]["search"])
    with st.expander("当前原始事件"):
        current = dict(events[cursor])
        if not reveal and current.get("kind") == "begin" and current.get("recipient") != seat:
            st.caption("该事件包含其他座位的私有手牌，当前视角隐藏。")
        elif not reveal and current.get("kind") == "decision" and current.get("seat") != seat:
            st.caption("该事件属于其他座位的私有决策，当前视角隐藏。")
        elif not reveal and current.get("kind") == "received" and "handCards" in current.get("message", {}) and current["message"].get("myPos", seat) != seat:
            st.caption("该接收事件包含其他座位的私有信息，当前视角隐藏。")
        else:
            st.json(current)


def belief_page():
    st.title("已知牌记准确，未知牌给概率")
    st.write("预测仅利用本人手牌、公开出牌、公开交接及剩余张数。PASS 只提供弱线索。")
    events = log_picker("belief")
    if not events:
        return
    decisions = [e for e in events if e["kind"] == "decision" and e["observation"]["stage"] == "play"]
    if not decisions:
        st.info("日志中还没有出牌决策。")
        return
    selected = st.selectbox("预测时刻", decisions, format_func=lambda e: f"事件 {e['seq']} · 座位 {e['seat']} · 剩余 {len(e['observation']['hand'])} 张")
    behavior = st.checkbox("使用 PASS 行为软权重", True)
    history, obs = history_at(events, selected)
    unseen = history.unseen(obs)
    st.caption(f"未知牌共 {sum(unseen.values())} 张；抽样是近似预测，不是其他玩家的确定手牌。")
    st.dataframe(pd.DataFrame([{"牌": format_card(c), "未见数量": n} for c, n in sorted(unseen.items())]), hide_index=True, use_container_width=True)
    prediction = history.summary(obs, random.Random(42), count=80, behavior=behavior)
    st.dataframe(pd.DataFrame(prediction).rename(columns={"seat": "座位", "rank": "点数", "expected_count": "预计张数", "p_pair": "至少两张概率", "p_bomb": "至少四张概率"}), hide_index=True, use_container_width=True)


def stats_page():
    st.title("在相同发牌上，比较不同决策")
    st.write("每个配对交换双方队伍。完整比赛与单小局分别统计，开发样本不代表最终棋力。")
    with st.expander("运行离线 PK", expanded=True):
        with st.form("evaluation_form"):
            columns = st.columns(2)
            a = columns[0].selectbox("A 队算法", STRATEGIES, format_func=lambda n: NAMES[n])
            b = columns[1].selectbox("B 队算法", STRATEGIES, index=2, format_func=lambda n: NAMES[n])
            cols = st.columns(3)
            pairs = cols[0].number_input("配对组数（每组两场）", 1, 100, 2)
            budget = cols[1].number_input("单步软预算 ms", 20, 2000, 150, step=10)
            seed = cols[2].number_input("起始种子", 0, 999999, 1)
            mode = st.selectbox("评测层级", ["episode", "match"], format_func=lambda m: "单小局 · 快速开发检查" if m == "episode" else "完整比赛 · 含升级进贡还贡")
            strict = st.checkbox("进程隔离并终止超预算策略（正式 PK 建议启用）", value=False)
            launch = st.form_submit_button("开始 PK", use_container_width=True)
        current = st.session_state.get("evaluation_process")
        if launch:
            if current and current.poll() is None:
                st.warning("已有评测正在运行，请等待结束。")
            elif not (RUNTIME / "guandan-java/guandan-java-action.jar").is_file():
                st.error("先在终端运行 python -m guandan_ai fetch-platform")
            else:
                output = ROOT / "data/runs" / ("ui-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
                output.mkdir(parents=True)
                with (output / "console.log").open("w", encoding="utf-8") as stdout:
                    command = [sys.executable, "-X", "utf8", "-m", "guandan_ai", "evaluate", "--a", a, "--b", b,
                        "--pairs", str(pairs), "--seed", str(seed), "--budget", str(budget), "--mode", mode, "--output", str(output)]
                    if strict:
                        command.append("--strict-budget")
                    current = subprocess.Popen(command,
                        cwd=ROOT, stdout=stdout, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                st.session_state["evaluation_process"] = current
                st.session_state["evaluation_output"] = str(output)
        if current:
            status = current.poll()
            st.info("评测运行中，点击刷新查看进度。" if status is None else f"评测进程已结束，退出码 {status}。")
            st.button("刷新进度")
            console = Path(st.session_state["evaluation_output"]) / "console.log"
            if console.exists():
                st.code(console.read_text(encoding="utf-8", errors="replace")[-3000:], language="text")
    paths = sorted((ROOT / "data/runs").rglob("summary.json"), key=lambda p: p.stat().st_mtime, reverse=True) if (ROOT / "data/runs").exists() else []
    if not paths:
        st.info("还没有完成的评测结果。")
        return
    path = st.selectbox("评测结果", paths, format_func=lambda p: str(p.parent.relative_to(ROOT)))
    summary = json.loads(path.read_text(encoding="utf-8"))
    level = {"match": "完整比赛", "episode": "单小局，非完整比赛胜率", "session": "线上多场会话，最终平台结果"}.get(summary["mode"], summary["mode"])
    st.caption(f"{summary['strategy_a']} 对 {summary['strategy_b']} · {level}")
    if not summary.get("paired_deals", True):
        st.caption("线上服务器发牌，未控制同牌配对。耗时仅涵盖已记录客户端。")
    cols = st.columns(4)
    cols[0].metric("已完成", f"{summary['completed']} / {summary['matches']}")
    cols[1].metric("A 得分率", f"{summary['score_rate_a']:.1%}" if summary["score_rate_a"] is not None else "无数据")
    cols[2].metric("异常回退", summary["fallbacks"])
    cols[3].metric("超软预算", summary["over_budget"])
    st.caption(f"完整配对 {summary['complete_pairs']} 组 · 95% 配对区间 {summary['paired_bootstrap_95ci'] or '样本不足'}")
    df = pd.DataFrame(summary["results"])
    if not df.empty:
        st.dataframe(df[["seed", "swap", "status", "a_score", "episodes", "mean_ms", "p95_ms", "fallbacks"]], hide_index=True, use_container_width=True)
        st.bar_chart(df.groupby("swap")[["a_score"]].mean())
    st.download_button("下载完整结果 JSON", path.read_bytes(), "summary.json", "application/json")
    report = path.parent / "report.md"
    if report.exists():
        st.download_button("下载 Markdown 报告", report.read_bytes(), "report.md", "text/markdown")
    figure = path.parent / "report.svg"
    if figure.exists():
        st.download_button("下载 SVG 图表", figure.read_bytes(), "report.svg", "image/svg+xml")


def main():
    st.set_page_config(page_title="欢乐掼蛋 · AI 实验台", page_icon="♠", layout="wide")
    st.markdown("""<style>
    .block-container {padding-top: 2rem; max-width: 1300px;}
    h1 {font-size: 2rem !important; letter-spacing: -.03em;}
    .card-hand {display:flex;flex-wrap:wrap;gap:7px;margin:14px 0 20px;}
    .playing-card {background:white;border:1px solid #cbd8ce;border-radius:7px;
      min-width:52px;height:76px;display:flex;align-items:center;justify-content:center;
      font-weight:650;font-size:20px;box-shadow:0 2px 3px #173c2010;}
    .red-card {color:#b72c3b;} .black-card {color:#172d23;}
    </style>""", unsafe_allow_html=True)
    st.sidebar.title("♠ 欢乐掼蛋")
    st.sidebar.caption("真人对局 · 手牌 · 对照实验")
    page = st.sidebar.radio("工作台", ["欢乐掼蛋", "手牌分析", "对局回放", "记牌预测", "PK 统计"])
    st.sidebar.divider()
    st.sidebar.caption("0 / 2 同队，1 / 3 同队。在线 AI 仅使用本人及公开信息。")
    from .game_ui import play_page
    pages = {"欢乐掼蛋": lambda: play_page(resources), "手牌分析": analysis_page, "对局回放": replay_page, "记牌预测": belief_page, "PK 统计": stats_page}
    try:
        pages[page]()
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        st.error(f"无法读取或处理数据：{exc}")

