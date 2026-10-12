from collections import Counter
import random

import pytest

from guandan_ai.analysis import features
from guandan_ai.belief import BeliefError, PublicHistory
from guandan_ai.cards import deck, remove_cards, validate_hand, wire_matrix
from guandan_ai.client import build_response
from guandan_ai.evaluation import confidence_interval, normalize_result, summarize
from guandan_ai.models import Decision, Observation, PASS
from guandan_ai.policies import HybridPolicy, TeamPolicy, decide_safely
from guandan_ai.search import SearchState


def observation(hand=None, actions=None, **kwargs):
    hand = hand or ["S3", "H3", "C4"]
    return Observation(0, hand, (len(hand), 4, 3, 4), "2",
                       actions or [PASS, ["Pair", "3", ["H3", "S3"]]], **kwargs)


def test_two_decks_keep_duplicate_suit_cards():
    assert len(deck()) == 108
    assert set(Counter(deck()).values()) == {2}
    assert remove_cards(["S3", "S3", "H3"], ["S3"]) == ["S3", "H3"]
    assert wire_matrix(["S3", "S3"])[1] == (1 << 1) | (1 << 5)


@pytest.mark.parametrize("cards", [["S3"] * 3, ["S10"], ["HB"], ["X3"]])
def test_reject_invalid_cards(cards):
    with pytest.raises(ValueError):
        validate_hand(cards)


def test_remove_rejects_nonexistent_card():
    with pytest.raises(ValueError):
        remove_cards(["S3"], ["S3", "S3"])


def test_observation_does_not_share_mutable_platform_actions():
    msg = {"handCards": ["S3"], "actionList": [["Single", "3", ["S3"]]], "curRank": "2"}
    obs = Observation.from_message(msg, 0)
    obs.actions[0][2].append("H3")
    assert msg["actionList"][0][2] == ["S3"]


def test_fresh_lead_list_overrides_stale_post_tribute_greater_action():
    stale = ["Single", "Q", ["HQ"]]
    msg = {"stage": "play", "handCards": ["S3"], "actionList": [["Single", "3", ["S3"]]],
           "curRank": "2", "greaterPos": 1, "greaterAction": stale}
    obs = Observation.from_message(msg, 0)
    assert obs.greater_action is None and obs.greater_pos == -1
    assert msg["greaterAction"] == stale


def test_external_baseline_cannot_inspect_other_hands():
    from guandan_ai.existing_ai import CountOnlyHand
    hand = CountOnlyHand(17)
    assert len(hand) == 17
    with pytest.raises(RuntimeError):
        list(hand)


def test_policy_configuration_rejects_invalid_search_limits():
    from guandan_ai.policies import PolicyConfig
    with pytest.raises(ValueError):
        PolicyConfig(samples=0)


def test_direct_finish_can_override_let_teammate_lead():
    obs = observation(["S3", "S3"], [PASS, ["Pair", "3", ["S3", "S3"]]], greater_pos=2)
    assert TeamPolicy(0).decide(obs).index == 1


def test_preserve_teammate_in_safe_position():
    obs = observation(greater_pos=2)
    assert TeamPolicy(0).decide(obs).index == 0


def test_illegal_plugin_index_returns_legal_fallback():
    class Bad:
        def decide(self, obs, budget_ms):
            return Decision(999, "bad")
    decision = decide_safely(Bad(), observation())
    assert decision.fallback and decision.index == 1


@pytest.mark.parametrize("stage,kind", [("play", "PLAY"), ("tribute", "TRIBUTE"), ("back", "PAYTRIBUTE")])
def test_response_preserves_exact_action_order_and_tribute_context(stage, kind):
    action = ["Pair", "3", ["H3", "S3"]]
    obs = observation(actions=[action], stage=stage, wire={"tributePos": 3, "tribute": "HR"})
    response = build_response(obs, Decision(0, "test"), 42)
    assert response["type"] == kind
    assert response["data"]["act"] is action
    if stage == "back":
        assert response["data"]["tributePos"] == 3
        assert response["data"]["tribute"] == "HR"


def make_belief():
    hands = [deck()[i * 27:(i + 1) * 27] for i in range(4)]
    history = PublicHistory(0)
    history.observe({"type": "notify", "stage": "beginning", "handCards": hands[0], "myPos": 0})
    return history, hands


def test_belief_conserves_all_unplayed_cards():
    history, hands = make_belief()
    obs = Observation(0, hands[0], (27, 27, 27, 27), "2", [PASS])
    for sample in history.sample(obs, random.Random(2), 12):
        assert [len(h) for h in sample] == [27] * 4
        assert sample[0] == hands[0]
        assert sum((Counter(h) for h in sample), Counter()) == Counter(deck())


def test_public_transfer_is_not_a_play_and_known_owner_is_preserved():
    history, hands = make_belief()
    transferred = hands[1].pop()
    hands[2].append(transferred)
    history.observe({"type": "notify", "stage": "tribute", "result": [[1, 2, transferred]]})
    obs = Observation(0, hands[0], (27, 26, 28, 27), "2", [PASS])
    assert sum(history.unseen(obs).values()) == 81
    for sample in history.sample(obs, random.Random(4), 10):
        assert transferred in sample[2]
    history.observe({"type": "notify", "stage": "play", "curPos": 2,
                     "curAction": ["Single", transferred[1], [transferred]]})
    obs.rest = (27, 26, 27, 27)
    assert history.known[2][transferred] == 0
    assert sum(history.unseen(obs).values()) == 80


def test_pass_is_soft_evidence_and_never_removes_cards():
    history, hands = make_belief()
    history.observe({"type": "notify", "stage": "play", "curPos": 1, "curAction": PASS,
        "greaterPos": 0, "greaterAction": ["Single", "3", ["S3"]]})
    obs = Observation(0, hands[0], (27, 27, 27, 27), "2", [PASS])
    assert sum(history.unseen(obs).values()) == 81
    assert len(history.sample(obs, random.Random(0), 4, behavior=True)) == 4
    assert history._weight(hands, "2") > 0


def test_missing_history_fails_closed_for_prediction():
    history, hands = make_belief()
    obs = Observation(0, hands[0], (27, 26, 27, 27), "2", [PASS])
    with pytest.raises(BeliefError):
        history.sample(obs, random.Random(0))


def test_episode_begin_resets_history():
    history, hands = make_belief()
    history.played.update(["S3"])
    history.observe({"type": "notify", "stage": "beginning", "handCards": hands[0], "myPos": 0})
    assert not history.played and not history.finished and history.episode == 2


def test_trick_return_to_active_leader():
    state = SearchState((("S3",), ("S4",), ("S5",), ("S6",)), 1, "2", 0, '["Single","3",["S3"]]')
    for _ in range(3):
        state = state.play(PASS)
    assert state.turn == 0 and state.greater == ""


def test_wind_goes_to_finished_leaders_teammate():
    state = SearchState(((), ("S4",), ("S5",), ("S6",)), 1, "2", 0, '["Single","3",["S3"]]', (0,))
    for _ in range(3):
        state = state.play(PASS)
    assert state.turn == 2 and state.greater == ""


def test_leading_pass_is_rejected():
    with pytest.raises(ValueError):
        SearchState((("S3",), ("S4",), ("S5",), ("S6",)), 0, "2").play(PASS)


def test_terminal_team_reward_not_individual_reward():
    state = SearchState(((), (), (), ("S6",)), 2, "2", order=(0, 1, 2))
    assert state.terminal and state.value(0) > 0 and state.value(1) < 0


def test_normalize_different_official_result_payloads():
    assert normalize_result({"victory": 1})["winner"] == 1
    assert normalize_result({"victoryNum": [1, 0, 1, 0], "draws": [0] * 4})["winner"] == 0
    with pytest.raises(ValueError):
        normalize_result({"curRank": "A"})
    # Native victoryNum counts small-game heads, not complete-match wins.
    with pytest.raises(ValueError):
        normalize_result({"victoryNum": [4, 3, 2, 1]})


def test_failed_match_is_not_counted_as_a_win_or_complete_pair():
    records = [{"seed": 1, "swap": False, "status": "finished", "a_score": 1., "steps": 2, "fallbacks": 0, "over_budget": 0},
               {"seed": 1, "swap": True, "status": "error", "a_score": None, "steps": 1, "fallbacks": 1, "over_budget": 0}]
    summary = summarize(records)
    assert summary["completed"] == 1 and summary["failed"] == 1 and summary["complete_pairs"] == 0
    assert summary["fallbacks"] == 1
    assert confidence_interval([1]) is None

