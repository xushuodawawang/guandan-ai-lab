import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from guandan_ai.llm import LLMPolicy, ModelProfile, ModelError, profiles, public_profiles
from guandan_ai.models import Observation
from guandan_ai.worker import ProcessPolicy


def observation():
    return Observation(1, ["S3", "H3", "S4"], (9, 3, 12, 2), "2",
        [["PASS", "PASS", "PASS"], ["Single", "3", ["S3"]], ["Single", "4", ["S4"]]],
        greater_pos=0, greater_action=["Single", "2", ["S2"]], wire={"hidden_hands": "NEVER_SEND_THIS"})


def test_model_only_receives_private_self_hand_and_public_candidates(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "fixture-secret-key")
    profile = ModelProfile(key_env="TEST_MODEL_KEY", input_price_per_million=1, output_price_per_million=2)
    def transport(p, payload, timeout):
        serialized = json.dumps(payload)
        assert "fixture-secret-key" not in serialized and "NEVER_SEND_THIS" not in serialized
        context = json.loads(payload["messages"][1]["content"])
        assert context["hand"] == observation().hand and context["seat"] == 1
        assert any(c["action"][0] == "PASS" for c in context["candidates"])
        pass_id = next(c["candidate_id"] for c in context["candidates"] if c["action"][0] == "PASS")
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"candidate_id":pass_id})}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
    decision = LLMPolicy(seat=1, profile=profile, transport=transport).decide(observation())
    assert decision.index == 0 and not decision.fallback
    assert decision.search["usage"]["total_tokens"] == 110
    assert decision.search["estimated_cost"] == pytest.approx(.00012)


@pytest.mark.parametrize("response", [
    {}, {"choices": []},
    {"choices": [{"finish_reason": "length", "message": {"content": '{"candidate_id":0}'}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "bad JSON"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"candidate_id":999}'}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"candidate_id":true}'}}]},
])
def test_invalid_or_empty_output_always_falls_back_legally(response):
    policy = LLMPolicy(seat=1, transport=lambda *args: response)
    decision = policy.decide(observation())
    assert decision.fallback and 0 <= decision.index < len(observation().actions)


def test_model_timeout_and_secret_exception_are_not_exposed():
    def transport(*args):
        raise TimeoutError("Authorization fixture-secret-key")
    decision = LLMPolicy(seat=1, transport=transport).decide(observation())
    assert decision.fallback and "fixture-secret-key" not in json.dumps(decision.to_dict())


def test_unconfigured_provider_status_never_returns_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert not next(p for p in public_profiles() if p["id"] == "llm:deepseek")["ready"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-secret-key")
    assert "fixture-secret-key" not in json.dumps(public_profiles())
    assert next(p for p in public_profiles() if p["id"] == "llm:deepseek")["ready"]


def test_deepseek_default_game_calls_allow_real_reasoning(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    for name in ("DEEPSEEK_MAX_TOKENS", "DEEPSEEK_REASONING_EFFORT", "DEEPSEEK_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-secret-key")
    def transport(profile, payload, timeout):
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high" and payload["max_tokens"] == 8192
        assert 44 < timeout <= 45
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"candidate_id":0}'}}]}
    decision = LLMPolicy(seat=1, profile=profiles()["deepseek"], transport=transport).decide(observation())
    assert not decision.fallback


@pytest.mark.integration
def test_isolated_worker_calls_actual_mock_compatible_http_api(monkeypatch):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(payload)
            body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"candidate_id":1}'}}],
                               "usage": {"prompt_tokens": 80, "completion_tokens": 8, "total_tokens": 88}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-secret-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("DEEPSEEK_MODEL", "fixture-model")
    worker = ProcessPolicy("llm:deepseek", 1)
    try:
        worker.start()
        decision = worker.decide(observation(), 2500)
        assert not decision.fallback and decision.search["usage"]["total_tokens"] == 88
        assert calls and calls[0]["model"] == "fixture-model"
        assert "fixture-secret-key" not in json.dumps(calls)
        assert "NEVER_SEND_THIS" not in json.dumps(calls)
    finally:
        worker.close()
        server.shutdown()
        server.server_close()
