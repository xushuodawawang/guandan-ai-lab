import json

import pytest

from guandan_ai.llm import LLMPolicy, ModelProfile, model_statistics, profiles
from guandan_ai.models import Observation, PASS


def follow():
    return Observation(0, ["S6", "S7", "SB"], (3, 10, 10, 10), "2",
                       [PASS, ["Single", "6", ["S6"]], ["Single", "B", ["SB"]]],
                       greater_pos=1, greater_action=["Single", "5", ["S5"]])


def test_fast_mode_remains_available_without_conflicting_effort(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_THINKING", "disabled")
    monkeypatch.delenv("DEEPSEEK_MAX_TOKENS", raising=False)
    monkeypatch.setenv("DEEPSEEK_REASONING_EFFORT", "high")
    profile = profiles()["deepseek"]
    assert profile.thinking == "disabled" and profile.reasoning_effort is None and profile.max_tokens == 512


def test_reasoning_usage_is_recorded_without_exposing_reasoning_text():
    def transport(profile, payload, timeout):
        context = json.loads(payload["messages"][1]["content"])
        assert context["low_cost_follow_candidate_ids"]
        candidate = next(c["candidate_id"] for c in context["candidates"] if c["action"] == ["Single", "6", ["S6"]])
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"candidate_id": candidate, "intent": "improve_structure"}),
            "reasoning_content": "PRIVATE_REASONING_TEXT"}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 1200, "total_tokens": 1400,
                      "completion_tokens_details": {"reasoning_tokens": 1180}}}
    decision = LLMPolicy(0, transport=transport).decide(follow())
    assert decision.index == 1 and not decision.fallback
    assert "PRIVATE_REASONING_TEXT" not in json.dumps(decision.to_dict())
    assert model_statistics([decision])["reasoning_tokens"] == 1180


def test_pass_is_not_fixed_candidate_zero_and_valid_voluntary_pass_is_preserved():
    obs = follow()
    def transport(profile, payload, timeout):
        context = json.loads(payload["messages"][1]["content"])
        candidate = next(c["candidate_id"] for c in context["candidates"] if c["action"][0] == "PASS")
        assert candidate != 0
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"candidate_id": candidate, "intent": "conserve_control"})}}]}
    decision = LLMPolicy(0, transport=transport).decide(obs)
    assert decision.index == 0 and not decision.fallback
    assert "主动不出" in decision.reason
    assert model_statistics([decision])["model_voluntary_passes"] == 1


def test_only_legal_pass_is_classified_and_does_not_call_api():
    obs = follow()
    obs.actions = [PASS]
    def forbidden(*args):
        pytest.fail("forced PASS must not call the model")
    decision = LLMPolicy(0, transport=forbidden).decide(obs)
    assert model_statistics([decision])["model_forced_passes"] == 1


def test_direct_finish_cannot_be_lost_to_a_model_pass():
    obs = follow()
    obs.hand = ["S6"]
    obs.actions = [PASS, ["Single", "6", ["S6"]]]
    def forbidden(*args):
        pytest.fail("a legal direct finish must not need an API call")
    decision = LLMPolicy(0, transport=forbidden).decide(obs)
    assert decision.index == 1 and not decision.fallback and not decision.search["request_attempted"]


def test_truncated_reasoning_falls_back_legally_and_keeps_known_usage():
    response = {"choices": [{"finish_reason": "length", "message": {"content": ""}}],
                "usage": {"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 8192}}}
    decision = LLMPolicy(0, transport=lambda *args: response).decide(follow())
    assert decision.fallback and 0 <= decision.index < len(follow().actions)
    assert decision.search["reasoning_tokens"] == 8192
