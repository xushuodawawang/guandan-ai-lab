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


@dataclass(frozen=True)
class ModelProfile:
    id: str = "deepseek"
    label: str = "DeepSeek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    key_env: str = "DEEPSEEK_API_KEY"
    timeout: float = 12.0
    candidates: int = 24
    thinking: str | None = None
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    @property
    def ready(self):
        return bool(os.environ.get(self.key_env, "").strip() and self.model)


def profiles():
    result = {"deepseek": ModelProfile(model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        timeout=float(os.getenv("DEEPSEEK_TIMEOUT", "12")),
        thinking=os.getenv("DEEPSEEK_THINKING", "disabled"))}
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
             "timeout_sec": p.timeout, "reason": "" if p.ready else f"服务器未配置 {p.key_env}"} for p in profiles().values()]


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
        return list(dict.fromkeys(mandatory + rows))[:self.profile.candidates]

    def decide(self, obs, budget_ms=12000):
        started = time.perf_counter()
        indices = self.candidates_for(obs)
        if len(obs.actions) == 1:
            return Decision(0, "唯一合法动作，无需请求模型", search={"provider": self.profile.id,
                "model": self.profile.model, "request_attempted": False, "skipped": "唯一合法动作"})
        context = {"seat": obs.seat, "teammate": obs.teammate, "stage": obs.stage, "hand": obs.hand,
            "trump": obs.trump, "remaining": list(obs.rest), "leader": obs.greater_pos,
            "leading_action": obs.greater_action, "finished": list(obs.finished),
            "public_history": self.history.events[-32:],
            "candidates": [{"candidate_id": n, "action": obs.actions[i]} for n, i in enumerate(indices)]}
        payload = {"model": self.profile.model, "stream": False, "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": "你是掼蛋团队策略。只根据本人手牌与公开信息，选择一个候选动作。优先团队争先，合理让队友保持牌权，避免无意义拆牌或浪费炸弹。不要生成牌。必须仅输出 JSON，例如 {\"candidate_id\": 0}。"},
                         {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]}
        meta = {"provider": self.profile.id, "model": self.profile.model, "prompt_version": "guandan-v1",
                "candidate_count": len(indices), "request_attempted": self.profile.ready, "usage": {}, "estimated_cost": None}
        if self.profile.thinking is not None:
            payload["thinking"] = {"type": self.profile.thinking}
        try:
            response = self.transport(self.profile, payload, min(self.profile.timeout, max(.1, budget_ms / 1000)))
            usage = response.get("usage", {})
            meta["usage"] = {k: v for k, v in usage.items() if k in ("prompt_tokens", "completion_tokens", "total_tokens") and type(v) is int and v >= 0}
            if meta["usage"]:
                meta["usage"].setdefault("total_tokens", meta["usage"].get("prompt_tokens", 0)+meta["usage"].get("completion_tokens", 0))
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
            decision = Decision(indices[candidate], "大模型从官方合法候选中选择动作", search=meta)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ModelError) else "模型响应格式异常"
            decision = super().decide(obs, budget_ms=min(100, budget_ms))
            decision.fallback = True
            meta["fallback_reason"] = reason
            decision.search = meta
            decision.reason = f"{reason}，采用本地团队策略回退"
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
            "model_fallbacks": sum(d.get("fallback", False) for d in remote),
            "model_mean_ms": sum(times)/len(times) if times else None,
            "model_p95_ms": times[min(len(times)-1, int(len(times)*.95))] if times else None,
            "usage": usage, "estimated_cost": sum(costs) if costs else None,
            "models": [{"provider": p, "model": m, "prompt_version": v} for p, m, v in models]}
