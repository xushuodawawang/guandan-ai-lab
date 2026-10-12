from collections import Counter
from io import BytesIO
import random

import pytest

from guandan_ai.game import HumanGame
from guandan_ai.logging import read_jsonl
from guandan_ai.models import action_cards, is_pass
from guandan_ai.platform import RUNTIME
from guandan_ai.policies import GreedyPolicy
from guandan_ai.replay import replay_snapshot
from guandan_ai.rules import JavaRules

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def rules():
    if not (RUNTIME / "guandan-java/guandan-java-action.jar").is_file():
        pytest.skip("官方运行时未下载")
    with JavaRules() as rules:
        yield rules


def select_action(game, action):
    needed = Counter(action_cards(action))
    game.selected.clear()
    for i, card in enumerate(game.hand):
        if needed[card]:
            game.selected.add(i)
            needed[card] -= 1
    assert not +needed


def human_greedy_move(game):
    obs = game.observation()
    index = GreedyPolicy(0).decide(obs).index
    action = obs.actions[index]
    if is_pass(action):
        game.pass_turn()
    else:
        select_action(game, action)
        game.play_selected(index)


def test_human_controls_reject_illegal_selection_and_preserve_physical_duplicates(rules):
    game = HumanGame(rules, "greedy", seed=3)
    assert game.human_turn and game.rest == [27] * 4
    assert not game.step_ai()
    with pytest.raises(ValueError):
        game.pass_turn()
    with pytest.raises(ValueError):
        game.export_log()
    game.selected = {27}
    with pytest.raises(ValueError):
        game.play_selected()
    pair_index = next(i for i, a in enumerate(game.observation().actions)
                      if a[0] == "Pair" and len(set(a[2])) == 1)
    action = game.observation().actions[pair_index]
    select_action(game, action)
    assert len(game.selected) == 2
    assert pair_index in game.matching_actions()
    game.play_selected(pair_index)
    assert len(game.hand) == 25 and not game.selected and not game.human_turn
    assert any(e["kind"] == "decision" and e["strategy"] == "human" and e["action"] == action for e in game.events)


def test_hint_does_not_submit_or_redeal(rules):
    game = HumanGame(rules, "greedy", seed=9, budget_ms=60)
    before = game.hand[:]
    decision = game.hint()
    assert game.human_turn and game.steps == 0 and game.hand == before
    assert decision.index in game.matching_actions()
    assert game.hint().index == decision.index
    game.play_selected(decision.index)
    for _ in range(20):
        if game.human_turn or game.round_over:
            break
        assert game.step_ai()
    assert game.human_turn or game.round_over


@pytest.mark.parametrize("seed", [3, 11])
def test_playable_complete_match_round_boundaries_tribute_and_replay(rules, seed):
    previous_random = random.getstate()
    game = HumanGame(rules, "greedy", seed=seed)
    for _ in range(5000):
        if game.match_over:
            break
        if game.round_over:
            round_count = len(game.rounds)
            before = game.hand[:]
            assert not game.step_ai()
            assert before == game.hand
            events = read_jsonl(BytesIO(game.export_log()))
            assert replay_snapshot(events, len(events) - 1)["order"] == game.rounds[-1]["order"]
            game.next_round()
            assert len(game.rounds) == round_count and not game.round_over
        elif game.human_turn:
            human_greedy_move(game)
        else:
            assert game.step_ai()
    else:
        pytest.fail("真人模式完整比赛未结算")
    assert random.getstate() == previous_random
    assert game.outcome is not None and len(game.rounds) > 1
    stages = {e["observation"]["stage"] for e in game.events if e["kind"] == "decision"}
    assert {"play", "tribute", "back"} <= stages
    for event in game.events:
        if event["kind"] == "decision":
            assert event["action"] in event["observation"]["actions"]
            assert event["observation"]["seat"] == event["seat"]
    with pytest.raises(ValueError):
        game.next_round()
