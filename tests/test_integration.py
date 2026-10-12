from collections import Counter
import json
import random
import time

import pytest

from guandan_ai.analysis import HandAnalyzer
from guandan_ai.cards import hand_key
from guandan_ai.evaluation import run_one
from guandan_ai.logging import read_jsonl
from guandan_ai.models import Observation, is_pass
from guandan_ai.platform import RUNTIME
from guandan_ai.replay import history_at
from guandan_ai.rules import JavaRules, load_environment
from guandan_ai.search import SearchState
from guandan_ai.worker import ProcessPolicy

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def rules():
    if not (RUNTIME / "guandan-java/guandan-java-action.jar").is_file():
        pytest.skip("先 fetch-platform，再运行官方引擎契约测试")
    with JavaRules() as r:
        r.legal(["S3"], "2")
        yield r


def test_official_duplicate_pairs_wild_straight_and_four_kings(rules):
    assert ["Pair", "3", ["S3", "S3"]] in rules.legal(["S3", "S3"], "2")
    assert any(a[0] == "Straight" for a in rules.legal(["S3", "S4", "S5", "S6", "H2"], "2"))
    assert any(a[0] == "FourKings" for a in rules.legal(["SB", "SB", "HR", "HR"], "2"))


def test_legal_decomposition_and_small_exact_search(rules):
    hand = ["S3", "H3", "C4", "D5", "S6", "H7"]
    plan = HandAnalyzer(rules).decompose(hand, "2", budget_ms=500)[0]
    assert plan.turns == 2 and plan.exact
    remaining = hand[:]
    from guandan_ai.cards import remove_cards
    for group in plan.groups:
        assert group in rules.legal(remaining, "2")
        remaining = remove_cards(remaining, group[2])
    assert not remaining


@pytest.mark.parametrize("seed", [2, 11])
def test_search_transition_matches_official_table_for_entire_episode(rules, seed):
    Environment = load_environment(rules)
    env = Environment(first_player=seed % 4)
    for seat in range(4):
        env.add_player(str(seat), seat)
    random.seed(seed)
    env.start()
    state = SearchState(tuple(hand_key(h) for h in env.get_hand_card()), env.state.current_pos, env.rank)
    rng = random.Random(seed)
    for _ in range(500):
        moves = env.legal_moves.action_list
        index = rng.randrange(len(moves))
        state = state.play(moves[index])
        pending = env.play({"actIndex": index})
        assert state.hands == tuple(hand_key(h) for h in env.get_hand_card())
        if state.terminal:
            assert any(p.body.get("stage") == "episodeOver" for p in pending)
            break
        assert state.turn == env.state.current_pos
        assert state.leader == env.state.greater_pos
        greater = env.state.greater_action.to_json()
        assert (json.loads(state.greater) if state.greater else None) == (None if is_pass(greater) else greater)
    else:
        pytest.fail("随机牌局未在合理步数内结束")


def test_actual_match_logs_conserve_public_information_and_complete_tribute(rules, tmp_path):
    result = run_one(rules, "hybrid", "greedy", seed=3, mode="match", budget_ms=70,
                     log_path=tmp_path / "match.jsonl")
    assert result["status"] == "finished", result["error"]
    assert result["episodes"] > 1 and result["fallbacks"] == 0
    events = read_jsonl(tmp_path / "match.jsonl")
    stages = {e["observation"]["stage"] for e in events if e["kind"] == "decision"}
    assert {"play", "tribute", "back"} <= stages
    decisions = [e for e in events if e["kind"] == "decision" and e["observation"]["stage"] == "play"]
    for e in decisions[::max(1, len(decisions) // 20)]:
        history, obs = history_at(events, e)
        assert sum(history.unseen(obs).values()) == sum(obs.rest) - len(obs.hand)
        for sample in history.sample(obs, random.Random(7), count=1):
            total = sum((Counter(h) for h in sample), Counter()) + history.played
            from guandan_ai.cards import deck
            assert total == Counter(deck())
    assert any(e["decision"]["search"].get("completed_depth", 0) > 0 for e in decisions)


def test_online_worker_terminates_overdue_policy(rules):
    worker = ProcessPolicy("plugin:tests.policy_fixtures:SlowPolicy", 0)
    try:
        worker.start()
        obs = Observation(0, ["S3"], (1, 1, 1, 1), "2", [["Single", "3", ["S3"]]])
        started = time.perf_counter()
        decision = worker.decide(obs, budget_ms=30)
        assert decision.fallback and decision.index == 0
        assert time.perf_counter() - started < 1.5
        assert worker.process is None
    finally:
        worker.close()


def test_existing_published_policy_runs_without_hidden_hands(rules):
    from guandan_ai.platform import WELKIN_ROOT
    from guandan_ai.policies import make_policy
    if not (WELKIN_ROOT / "LICENSE").is_file():
        pytest.skip("先 fetch-baseline")
    policy = make_policy("welkin", 0, 42, rules)
    obs = Observation(0, ["S3", "S3"], (2, 8, 9, 7), "2",
                      [["Single", "3", ["S3"]], ["Pair", "3", ["S3", "S3"]]])
    decision = policy.decide(obs, budget_ms=150)
    assert decision.index == 1 and not decision.fallback


def test_strict_offline_budget_runs_real_episode(rules, tmp_path):
    result = run_one(rules, "team", "greedy", seed=8, mode="episode", budget_ms=200,
                     log_path=tmp_path / "strict.jsonl", strict_budget=True)
    assert result["status"] == "finished", result["error"]
    assert result["steps"] > 10 and result["fallbacks"] == 0


def test_alpha_beta_values_match_exhaustive_small_tree(rules):
    from guandan_ai.search import EndgameSearch
    hands = [["S3", "H3"], ["S4", "H4"], ["S5", "H6"], ["S6", "S7"]]
    actions = rules.legal(hands[0], "2")
    obs = Observation(0, hands[0], (2, 2, 2, 2), "2", actions)
    start = SearchState(tuple(hand_key(h) for h in hands), 0, "2")
    def exhaustive(state, depth):
        if state.terminal or not depth:
            return state.value(0)
        greater = json.loads(state.greater) if state.greater else None
        values = [exhaustive(state.play(a), depth - 1) for a in rules.legal(state.hands[state.turn], "2", greater)]
        return (max if state.turn % 2 == 0 else min)(values)
    search = EndgameSearch(rules, max_depth=5, max_nodes=50000, branch_limit=100)
    _, meta = search.choose(obs, list(range(len(actions))), [hands], time.perf_counter() + 4)
    assert meta["completed_depth"] == 5
    for index, action in enumerate(actions):
        assert meta["values"][index] == exhaustive(start.play(action), 4)

