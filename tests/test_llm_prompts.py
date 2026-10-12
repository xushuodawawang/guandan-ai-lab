import json

import pytest

from guandan_ai.belief import PublicHistory
from guandan_ai.llm import LLMPolicy, ModelProfile
from guandan_ai.llm_prompts import PROMPT_VERSION, build_context, candidate_detail, system_prompt
from guandan_ai.models import Observation, PASS


@pytest.mark.parametrize("seat", range(4))
def test_absolute_team_roles_are_correct_for_every_seat(seat):
    mate = (seat + 2) % 4
    obs = Observation(seat, ["S6", "S7"], (7, 7, 7, 7), "2",
                      [PASS, ["Single", "6", ["S6"]]], greater_pos=mate,
                      greater_action=["Single", "5", ["S5"]])
    context = build_context(obs, PublicHistory(seat), [])
    assert context["teammate"] == mate and context["leader_relation"] == "队友"
    assert context["opponents"] == [(seat+1) % 4, (seat+3) % 4]
    assert "优先评估PASS" in system_prompt(obs)


def test_fresh_lead_ignores_stale_team_leader_and_only_flags_live_opponents():
    obs = Observation(0, ["S6", "S7"], (2, 0, 8, 1), "2",
                      [["Single", "6", ["S6"]]], greater_pos=2,
                      greater_action=["Single", "5", ["S5"]], finished=(1,))
    context = build_context(obs, PublicHistory(0), [])
    assert context["turn_mode"] == "lead" and not context["can_pass"]
    assert context["leader"] == -1 and context["leading_action"] is None
    assert context["opponents_near_finish"] == [3]
    assert "当前由你领出" in system_prompt(obs) and "优先评估PASS" not in system_prompt(obs)


@pytest.mark.parametrize("stage", ["tribute", "back"])
def test_handover_uses_its_own_duties_even_when_old_leader_is_teammate(stage):
    obs = Observation(0, ["S6", "S7"], (2, 1, 8, 27), "2",
                      [[stage, stage, ["S6"]]], stage=stage, greater_pos=2,
                      greater_action=["Single", "5", ["S5"]])
    prompt = system_prompt(obs)
    context = build_context(obs, PublicHistory(0), [])
    assert "只比较合法交接后结构" in prompt and "优先评估PASS" not in prompt
    assert context["turn_mode"] == stage and context["leader"] == -1
    assert not candidate_detail(obs, obs.actions[0], 0, 0)["can_finish"]


def test_finished_teammate_is_not_a_feed_target():
    obs = Observation(0, ["S6", "S7"], (2, 1, 0, 27), "2",
                      [PASS, ["Single", "6", ["S6"]]], greater_pos=1,
                      greater_action=["Single", "5", ["S5"]], finished=(2,))
    assert "不再向队友送牌" in system_prompt(obs)
    assert build_context(obs, PublicHistory(0), [])["teammate_finished"]


def test_candidate_explains_actual_bomb_pair_and_wildcard_consumption():
    hand = ["S4", "H4", "C4", "D4", "S6", "H6", "H7"]
    obs = Observation(0, hand, (7, 8, 8, 8), "7", [])
    bomb = candidate_detail(obs, ["Single", "4", ["S4"]], 0, 0)
    pair = candidate_detail(obs, ["Single", "6", ["S6"]], 1, 0)
    wild = candidate_detail(obs, ["Single", "7", ["H7"]], 2, 0)
    assert bomb["breaks_natural_bomb"] == ["4"] and bomb["remaining_after"].count("S4") == 0
    assert pair["breaks_pair"] == ["6"]
    assert wild["wildcards_used"] == 1


def test_extra_private_fields_in_history_are_not_forwarded_to_model():
    obs = Observation(0, ["S6", "S7"], (2, 8, 8, 8), "2",
                      [PASS, ["Single", "6", ["S6"]]], greater_pos=2,
                      greater_action=["Single", "5", ["S5"]])
    policy = LLMPolicy(0)
    policy.history.events = [{"stage": "play", "curPos": 2,
        "curAction": ["Single", "5", ["S5"]], "handCards": ["PRIVATE_STARTING_HAND"],
        "secret": "PRIVATE_KEY", "wire": {"hidden_hands": "PRIVATE_GLOBAL"}}]
    def transport(profile, payload, timeout):
        serialized = json.dumps(payload)
        assert all(value not in serialized for value in ("PRIVATE_STARTING_HAND", "PRIVATE_KEY", "PRIVATE_GLOBAL"))
        context = json.loads(payload["messages"][1]["content"])
        assert context["public_history"][0]["curAction"] == ["Single", "5", ["S5"]]
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"candidate_id":1}'}}]}
    policy.transport = transport
    decision = policy.decide(obs)
    assert not decision.fallback and decision.search["prompt_version"] == PROMPT_VERSION


def test_low_cost_follow_remains_available_after_candidate_limit():
    obs = Observation(0, ["S6", "S7", "SA", "SB"], (4, 8, 8, 8), "2",
                      [PASS] + [["Single", r, [c]] for r, c in (("6", "S6"), ("7", "S7"), ("A", "SA"), ("B", "SB"))])
    policy = LLMPolicy(0, profile=ModelProfile(candidates=4))
    policy.score = lambda observed, action: (0 if action[0] == "PASS" else {"6": 6, "7": 7, "A": 14, "B": 16}[action[1]], [])
    candidates = policy.candidates_for(obs)
    assert 0 in candidates and 1 in candidates and len(candidates) == 4
