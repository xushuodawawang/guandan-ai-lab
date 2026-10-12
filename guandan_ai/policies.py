import importlib
import random
import time
from dataclasses import asdict, dataclass

from .analysis import HandAnalyzer, features
from .belief import BeliefError, PublicHistory
from .cards import rank_value, remove_cards
from .models import Decision, Observation, action_cards, is_pass, validate_decision
from .search import EndgameSearch


@dataclass
class PolicyConfig:
    structure: bool = True
    behavior: bool = True
    search: bool = True
    candidate_limit: int = 5
    search_cards: int = 20
    samples: int = 3
    max_depth: int = 3
    max_nodes: int = 450

    def __post_init__(self):
        if not all(isinstance(v, bool) for v in (self.structure, self.behavior, self.search)):
            raise ValueError("structure/behavior/search 必须为布尔值")
        for name in ("candidate_limit", "search_cards", "samples", "max_depth", "max_nodes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正整数")


class Policy:
    def __init__(self, seat=0, seed=0, rules=None, config=None):
        self.seat = seat
        self.rng = random.Random(seed)
        self.rules = rules
        self.config = config or PolicyConfig()
        self.history = PublicHistory(seat)

    def observe(self, event):
        self.history.observe(event)


class RandomPolicy(Policy):
    name = "random"

    def decide(self, obs, budget_ms=200):
        return Decision(self.rng.randrange(len(obs.actions)), "随机选择合法动作")


class GreedyPolicy(Policy):
    name = "greedy"

    def decide(self, obs, budget_ms=200):
        index = max(range(len(obs.actions)), key=lambda i: (len(action_cards(obs.actions[i])),
                    -rank_value(obs.actions[i][1], obs.trump) if obs.stage == "play" and not is_pass(obs.actions[i]) else 0))
        return Decision(index, "优先出更多牌；同张数保留高牌")


class TeamPolicy(Policy):
    name = "team"

    def score(self, obs, action):
        cfg = self.config
        leading_mate = obs.greater_pos == obs.teammate
        threat = min(obs.rest[(obs.seat + 1) % 4], obs.rest[(obs.seat + 3) % 4]) <= 2
        if is_pass(action):
            return (18.0 if leading_mate else -2.0) - (9.0 if threat and not leading_mate else 0), ["让队友保持牌权" if leading_mate else "保留资源等待牌权"]
        cards = action_cards(action)
        remaining = remove_cards(obs.hand, cards)
        before, after = features(obs.hand, obs.trump), features(remaining, obs.trump)
        if obs.stage != "play":
            return -after["cost"], ["交接后保留较完整的手牌结构"]
        if not remaining:
            return 10000.0, ["本次可以直接出完"]
        score = 1.0 * len(cards)
        reasons = [f"出 {len(cards)} 张，剩 {len(remaining)} 张"]
        if cfg.structure:
            improvement = before["cost"] - after["cost"]
            score += 1.1 * improvement
            reasons.append(f"结构收益 {improvement:.1f}")
        if leading_mate:
            score -= 18
            reasons.append("接管队友牌权有成本")
        strength = rank_value(action[1], obs.trump)
        score -= max(0, strength - 11) * 0.6
        wild_used = cards.count("H" + obs.trump)
        score -= 1.2 * wild_used
        if action[0] in ("Bomb", "StraightFlush", "FourKings"):
            score -= 9
            reasons.append("消耗炸弹控制资源")
        if threat and not leading_mate:
            score += 12 + strength * 0.15
            reasons.append("对手剩余少，需要争夺牌权")
        if obs.rest[obs.teammate] <= 2 and not leading_mate:
            score += 2
            reasons.append("尝试为队友取得下一轮牌权")
        return score, reasons

    def decide(self, obs: Observation, budget_ms=200):
        start = time.perf_counter()
        deadline = start + max(1, budget_ms) / 1000
        rows = []
        for i, action in enumerate(obs.actions):
            score, reasons = self.score(obs, action)
            rows.append({"index": i, "action": action, "score": round(score, 4), "reasons": reasons})
        ranked = sorted(rows, key=lambda row: row["score"], reverse=True)
        decomposition_deadline = start + budget_ms * 0.3 / 1000
        if self.config.structure and self.rules and ranked[0]["score"] < 10000:
            analyzer = HandAnalyzer(self.rules)
            for row in ranked[:self.config.candidate_limit]:
                if time.perf_counter() >= decomposition_deadline:
                    break
                action = row["action"]
                remaining = remove_cards(obs.hand, action_cards(action))
                try:
                    plans = analyzer.decompose(remaining, obs.trump,
                        budget_ms=max(1, (decomposition_deadline - time.perf_counter()) * 1000), max_nodes=45, alternatives=1)
                    turns = plans[0].turns
                    row["decomposition_turns"] = turns
                    row["score"] += 2.0 * (features(remaining, obs.trump)["estimated_turns"] - turns)
                    row["reasons"].append(f"合法组牌约 {turns} 手")
                except RuntimeError as exc:
                    row["reasons"].append(f"组牌引擎不可用，使用结构估计: {exc}")
                    break
            ranked.sort(key=lambda row: row["score"], reverse=True)
        chosen = ranked[0]
        return Decision(chosen["index"], "；".join(chosen["reasons"]), ranked[:12],
                        (time.perf_counter() - start) * 1000)


class HybridPolicy(TeamPolicy):
    name = "hybrid"

    def decide(self, obs, budget_ms=200):
        start = time.perf_counter()
        deadline = start + max(1, budget_ms) / 1000
        decision = super().decide(obs, budget_ms)
        if self.config.search and obs.stage == "play" and self.rules and sum(obs.rest) <= self.config.search_cards and len(obs.actions) > 1 and decision.scores[0]["score"] < 10000:
            try:
                allocation = self.history.sample(obs, self.rng, self.config.samples, self.config.behavior)
                roots = [row["index"] for row in decision.scores[:self.config.candidate_limit]]
                search = EndgameSearch(self.rules, self.config.max_depth, self.config.max_nodes)
                index, meta = search.choose(obs, roots, allocation, deadline)
                decision.search = meta
                if index is not None:
                    decision.index = index
                    decision.reason = f"{len(allocation)} 个公开信息可行分配上的末局搜索，完成深度 {meta['completed_depth']}"
            except (BeliefError, RuntimeError, ValueError) as exc:
                decision.search = {"skipped": str(exc)}
        decision.elapsed_ms = (time.perf_counter() - start) * 1000
        return decision


def make_policy(name, seat=0, seed=0, rules=None, config=None):
    config = config or PolicyConfig()
    if name.startswith("llm:"):
        from .llm import LLMPolicy, profiles
        profile = profiles().get(name[4:])
        if profile is None:
            raise ValueError("未知模型配置")
        return LLMPolicy(seat, seed, rules, config, profile=profile)
    if name == "welkin":
        from .existing_ai import WelkinPolicy
        return WelkinPolicy(seat, seed, rules, config)
    if name.startswith("plugin:"):
        module_name, factory_name = name[len("plugin:"):].rsplit(":", 1)
        factory = getattr(importlib.import_module(module_name), factory_name)
        policy = factory(seat=seat, seed=seed, rules=rules, config=config)
        if not callable(getattr(policy, "decide", None)):
            raise ValueError("外部策略必须提供 decide(observation, budget_ms)")
        return policy
    variants = {"no-structure": "structure", "no-belief": "behavior", "no-search": "search"}
    if name in variants:
        config = PolicyConfig(**{**asdict(config), variants[name]: False})
        return HybridPolicy(seat, seed, rules, config)
    classes = {"random": RandomPolicy, "greedy": GreedyPolicy, "team": TeamPolicy, "hybrid": HybridPolicy}
    if name not in classes:
        raise ValueError(f"未知策略: {name}")
    return classes[name](seat, seed, rules, config)


def decide_safely(policy, obs, budget_ms=200):
    started = time.perf_counter()
    try:
        decision = validate_decision(policy.decide(obs, budget_ms=budget_ms), obs)
    except Exception as exc:
        decision = GreedyPolicy(obs.seat).decide(obs, budget_ms)
        decision.reason = f"策略异常，合法贪心回退: {type(exc).__name__}: {exc}"
        decision.fallback = True
    decision.elapsed_ms = (time.perf_counter() - started) * 1000
    return decision

