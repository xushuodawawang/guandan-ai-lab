"""OpenAI-compatible model policy. Keys never enter observations or game snapshots."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import Decision, action_cards, is_pass
from .policies import TeamPolicy
from .llm_prompts import (PROMPT_VERSION, CONTROL_TYPES, action_power, build_context,
                          candidate_detail, system_prompt)


@dataclass(frozen=True)
class ModelProfile:
    id: str = "deepseek"
    label: str = "DeepSeek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    key_env: str = "DEEPSEEK_API_KEY"
    timeout: float = 45.0
    candidates: int = 24
    thinking: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int = 512
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    @property
    def ready(self):
        return bool(os.environ.get(self.key_env, "").strip() and self.model)


def profiles():
    thinking = os.getenv("DEEPSEEK_THINKING", "enabled")
    result = {"deepseek": ModelProfile(model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        timeout=float(os.getenv("DEEPSEEK_TIMEOUT", "45")), thinking=thinking,
        reasoning_effort=os.getenv("DEEPSEEK_REASONING_EFFORT", "high") if thinking == "enabled" else None,
        max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "8192" if thinking == "enabled" else "512")))}
    config = os.getenv("GUANDAN_LLM_CONFIG")
    if config:
        data = json.loads(Path(config).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            raise ValueError("模型配置应包含 profiles 列表")
        for item in data["profiles"]:
            if "api_key" in item:
                raise ValueError("模型配置只接受 key_env，不接受明文 api_key")
            profile = ModelProfile(**item)
            result[profile.id] = profile
    for p in result.values():
        if p.thinking not in (None, "disabled", "enabled"):
            raise ValueError("模型 thinking 参数须为 disabled、enabled 或 null")
        if p.reasoning_effort not in (None, "low", "high", "max"):
            raise ValueError("模型 reasoning_effort 参数无效")
        if p.thinking == "disabled" and p.reasoning_effort is not None:
            raise ValueError("关闭思考模式时不能同时指定思考强度")
        if type(p.max_tokens) is not int or not 128 <= p.max_tokens <= 16384:
            raise ValueError("模型 max_tokens 应为128到16384的整数")
        if any(not isinstance(v, str) or not v for v in (p.id, p.label, p.model, p.key_env, p.base_url)):
            raise ValueError("模型的名称、地址与密钥变量名必须为非空文本")
        parsed = urlsplit(p.base_url)
        if ":" in p.id or type(p.timeout) not in (int, float) or not 1 <= p.timeout <= 60 or type(p.candidates) is not int or not 4 <= p.candidates <= 64:
            raise ValueError("模型配置字段无效")
        if any(v is not None and (type(v) not in (int, float) or v < 0) for v in (p.input_price_per_million, p.output_price_per_million)):
            raise ValueError("模型计价需为非负数字或 null")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("远程模型须使用 HTTPS，本机兼容接口可使用 HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("模型地址不能包含凭据或查询参数")
    return result


def public_profiles():
    return [{"id": "llm:" + p.id, "label": p.label, "model": p.model, "ready": p.ready,
             "timeout_sec": p.timeout, "thinking": p.thinking, "reasoning_effort": p.reasoning_effort,
             "max_tokens": p.max_tokens, "reason": "" if p.ready else f"服务器未配置 {p.key_env}"} for p in profiles().values()]


class ModelError(RuntimeError):
    pass


def request_completion(profile, payload, timeout):
    key = os.environ.get(profile.key_env, "").strip()
    if not key:
        raise ModelError("模型密钥未配置")
    req = Request(profile.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ModelError("模型响应过大")
        return json.loads(raw)
    except HTTPError as exc:
        raise ModelError(f"模型服务 HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise ModelError("模型连接失败或请求超时") from None
    except (ValueError, UnicodeError):
        raise ModelError("模型响应不是有效 JSON") from None


class LLMPolicy(TeamPolicy):
    name = "llm"

    def __init__(self, seat=0, seed=0, rules=None, config=None, profile=None, transport=None):
        super().__init__(seat, seed, rules, config)
        self.profile = profile or profiles()["deepseek"]
        self.transport = transport or request_completion

    def candidates_for(self, obs):
        rows = sorted(range(len(obs.actions)), key=lambda i: self.score(obs, obs.actions[i])[0], reverse=True)
        mandatory = [i for i, a in enumerate(obs.actions) if is_pass(a)]
        finish = next((i for i in rows if len(action_cards(obs.actions[i])) == len(obs.hand)), None)
        if finish is not None:
            mandatory.append(finish)
        # Preserve a representative of each control family, then fill by local score.
        for kind in ("Bomb", "StraightFlush", "FourKings"):
            index = next((i for i in rows if obs.actions[i][0] == kind), None)
            if index is not None:
                mandatory.append(index)
        # Keep low-cost representatives, so coaching can actually choose a
        # cheap follow or a different opening pattern instead of only top scores.
        for kind in dict.fromkeys(obs.actions[i][0] for i in rows if not is_pass(obs.actions[i])):
            same = [i for i in rows if obs.actions[i][0] == kind]
            cheapest = min(same, key=lambda i: (
                len(action_cards(obs.actions[i])) if kind in CONTROL_TYPES else 0,
                action_power(obs.actions[i], obs.trump),
                action_cards(obs.actions[i]).count("H" + obs.trump)))
            mandatory.append(cheapest)
        retained = set(list(dict.fromkeys(mandatory + rows))[:self.profile.candidates])
        return [i for i in rows if i in retained]

    def decide(self, obs, budget_ms=45000):
        started = time.perf_counter()
        indices = self.candidates_for(obs)
        if len(obs.actions) == 1:
            return Decision(0, "唯一合法动作，无需请求模型", search={"provider": self.profile.id,
                "model": self.profile.model, "prompt_version": PROMPT_VERSION,
                "action_kind": obs.actions[0][0], "pass_classification": "forced" if is_pass(obs.actions[0]) else None,
                "request_attempted": False, "skipped": "唯一合法动作"})
        finish = next((i for i in indices if obs.stage == "play" and not is_pass(obs.actions[i])
                       and len(action_cards(obs.actions[i])) == len(obs.hand)), None)
        if finish is not None:
            return Decision(finish, "已有合法动作可直接出完，无需等待模型", search={"provider": self.profile.id,
                "model": self.profile.model, "prompt_version": PROMPT_VERSION, "action_kind": obs.actions[finish][0],
                "request_attempted": False, "skipped": "直接出完"})
        candidates = [candidate_detail(obs, obs.actions[i], n, self.score(obs, obs.actions[i])[0])
                      for n, i in enumerate(indices)]
        context = build_context(obs, self.history, candidates)
        payload = {"model": self.profile.model, "stream": False, "max_tokens": self.profile.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system_prompt(obs)},
                         {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]}
        meta = {"provider": self.profile.id, "model": self.profile.model, "prompt_version": PROMPT_VERSION,
                "thinking": self.profile.thinking, "reasoning_effort": self.profile.reasoning_effort,
                "max_tokens": self.profile.max_tokens, "timeout_sec": self.profile.timeout,
                "candidate_count": len(indices), "request_attempted": self.profile.ready, "usage": {}, "estimated_cost": None}
        if self.profile.thinking is not None:
            payload["thinking"] = {"type": self.profile.thinking}
        if self.profile.reasoning_effort is not None:
            payload["reasoning_effort"] = self.profile.reasoning_effort
        try:
            remaining_budget = max(.1, budget_ms / 1000 - (time.perf_counter() - started))
            response = self.transport(self.profile, payload, min(self.profile.timeout, remaining_budget))
            usage = response.get("usage", {})
            meta["usage"] = {k: v for k, v in usage.items() if k in ("prompt_tokens", "completion_tokens", "total_tokens") and type(v) is int and v >= 0}
            if meta["usage"]:
                meta["usage"].setdefault("total_tokens", meta["usage"].get("prompt_tokens", 0)+meta["usage"].get("completion_tokens", 0))
            details = usage.get("completion_tokens_details") or {}
            reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
            if type(reasoning_tokens) is int and reasoning_tokens >= 0:
                meta["reasoning_tokens"] = reasoning_tokens
            if self.profile.input_price_per_million is not None and self.profile.output_price_per_million is not None and meta["usage"]:
                meta["estimated_cost"] = (meta["usage"].get("prompt_tokens", 0) * self.profile.input_price_per_million + meta["usage"].get("completion_tokens", 0) * self.profile.output_price_per_million) / 1000000
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ModelError("模型输出未正常结束")
            content = choice["message"].get("content")
            if not content:
                raise ModelError("模型返回空内容")
            parsed = json.loads(content)
            candidate = parsed.get("candidate_id") if isinstance(parsed, dict) else None
            if type(candidate) is not int or not 0 <= candidate < len(indices):
                raise ModelError("模型候选编号无效")
            intent = parsed.get("intent")
            intent_labels = {"teammate_support": "配合队友", "conserve_control": "保留控制资源",
                "intercept_opponent": "拦截对手", "improve_structure": "整理手牌", "handover": "交接保留结构"}
            intent = intent if isinstance(intent, str) and intent in intent_labels else None
            if intent == "teammate_support" and context["leader_relation"] != "队友":
                intent = None
            meta["intent"] = intent
            reason = "模型选择：" + intent_labels[intent] if intent else "大模型从官方合法候选中选择动作"
            decision = Decision(indices[candidate], reason, search=meta)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ModelError) else "模型响应格式异常"
            decision = super().decide(obs, budget_ms=min(100, budget_ms))
            decision.fallback = True
            meta["fallback_reason"] = reason
            decision.search = meta
            decision.reason = f"{reason}，采用本地团队策略回退"
        action = obs.actions[decision.index]
        meta["action_kind"] = action[0]
        if is_pass(action):
            meta["pass_classification"] = "teammate" if context["leader_relation"] == "队友" else "voluntary"
            if not decision.fallback:
                decision.reason = ("模型不出：队友持有牌权" if meta["pass_classification"] == "teammate"
                                   else "模型主动不出（存在合法压牌）") + ("；" + intent_labels[meta["intent"]] if meta.get("intent") else "")
        decision.elapsed_ms = (time.perf_counter() - started) * 1000
        return decision


def model_statistics(decisions):
    records = [d.to_dict() if isinstance(d, Decision) else d for d in decisions]
    remote = [d for d in records if d.get("search", {}).get("provider")]
    times = sorted(d.get("elapsed_ms", 0) for d in remote)
    usage = {k: sum(d.get("search", {}).get("usage", {}).get(k, 0) for d in remote) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    costs = [d["search"]["estimated_cost"] for d in remote if d["search"].get("estimated_cost") is not None]
    models = sorted({(str(d["search"].get("provider")), str(d["search"].get("model", "unknown")), str(d["search"].get("prompt_version", "unknown"))) for d in remote})
    return {"model_decisions": len(remote), "model_requests": sum(bool(d["search"].get("request_attempted")) for d in remote),
            "model_passes": sum(d["search"].get("action_kind") == "PASS" for d in remote),
            "model_forced_passes": sum(d["search"].get("pass_classification") == "forced" for d in remote),
            "model_teammate_passes": sum(d["search"].get("pass_classification") == "teammate" and not d.get("fallback") for d in remote),
            "model_voluntary_passes": sum(d["search"].get("pass_classification") == "voluntary" and not d.get("fallback") for d in remote),
            "reasoning_tokens": sum(d["search"].get("reasoning_tokens", 0) for d in remote),
            "model_fallbacks": sum(d.get("fallback", False) for d in remote),
            "model_mean_ms": sum(times)/len(times) if times else None,
            "model_p95_ms": times[min(len(times)-1, int(len(times)*.95))] if times else None,
            "usage": usage, "estimated_cost": sum(costs) if costs else None,
            "models": [{"provider": p, "model": m, "prompt_version": v} for p, m, v in models]}
