import math
import time
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache

from .cards import RANKS, hand_key, rank_value, remove_cards
from .models import action_cards, is_pass


@lru_cache(maxsize=30000)
def quick_features(hand: tuple[str, ...], trump: str) -> dict:
    counts = Counter(c[1] for c in hand)
    singles = sum(n == 1 for n in counts.values())
    bombs = sum(n >= 4 for n in counts.values())
    control = sum(rank_value(c[1], trump) >= 14 for c in hand)
    wild = hand.count("H" + trump)
    triples = sum(n == 3 for n in counts.values())
    pairs = sum(n == 2 for n in counts.values())
    turns = len(counts) - min(triples, pairs)
    # Natural runs are a heuristic feature, not a replacement legal-action generator.
    longest_run = run = 0
    for r in RANKS:
        run = run + 1 if counts[r] else 0
        longest_run = max(longest_run, run)
    if longest_run >= 5:
        turns -= 2
    turns = max(0 if not hand else 1, turns - min(wild, 1))
    cost = 7.0 * turns + 1.7 * singles - 0.35 * control - 0.8 * bombs
    return {"cards": len(hand), "estimated_turns": turns, "singletons": singles,
            "bombs": bombs, "control_cards": control, "wild_cards": wild,
            "longest_natural_run": longest_run, "cost": cost}


def features(hand, trump):
    return dict(quick_features(hand_key(hand), trump))


@dataclass
class HandPlan:
    groups: list
    turns: int
    exact: bool
    nodes: int
    elapsed_ms: float
    method: str

    def to_dict(self):
        return asdict(self)


class HandAnalyzer:
    def __init__(self, rules):
        self.rules = rules
        self.exact_cache = {}

    def decompose(self, hand, trump="2", budget_ms=100, max_nodes=350, alternatives=2) -> list[HandPlan]:
        hand = hand_key(hand)
        started = time.perf_counter()
        deadline = started + max(0, budget_ms) / 1000
        if not hand:
            return [HandPlan([], 0, True, 0, 0, "empty")]
        if (hand, trump) in self.exact_cache:
            return [HandPlan(self.exact_cache[hand, trump], len(self.exact_cache[hand, trump]), True, 0, 0, "cached exact")]
        nodes = 0
        roots = [a for a in self.rules.legal(hand, trump) if not is_pass(a)]
        roots.sort(key=lambda a: (-len(action_cards(a)), features(remove_cards(hand, action_cards(a)), trump)["cost"]))
        plans = []
        signatures = set()
        for root in roots:
            signature = tuple(sorted(action_cards(root)))
            if signature in signatures:
                continue
            signatures.add(signature)
            remaining = remove_cards(hand, action_cards(root))
            groups = [root]
            while remaining and time.perf_counter() < deadline and nodes < max_nodes:
                moves = [a for a in self.rules.legal(remaining, trump) if not is_pass(a)]
                nodes += 1
                best = max(moves, key=lambda a: (len(action_cards(a)), -features(remove_cards(remaining, action_cards(a)), trump)["cost"]))
                groups.append(best)
                remaining = remove_cards(remaining, action_cards(best))
            groups.extend([["Single", c[1], [c]] for c in remaining])
            plans.append(groups)
            if len(plans) >= alternatives or time.perf_counter() >= deadline:
                break
        best_groups = min(plans, key=len)
        exact = False
        if len(hand) <= 8 and time.perf_counter() < deadline:
            memo = {(): []}

            def solve(cards):
                nonlocal nodes
                if cards in memo:
                    return memo[cards]
                if time.perf_counter() >= deadline or nodes >= max_nodes:
                    raise TimeoutError
                nodes += 1
                moves = [a for a in self.rules.legal(cards, trump) if not is_pass(a)]
                moves.sort(key=lambda a: -len(action_cards(a)))
                best = [["Single", c[1], [c]] for c in cards]
                seen = set()
                max_cards = max(len(action_cards(a)) for a in moves)
                lower = math.ceil(len(cards) / max_cards)
                for action in moves:
                    rest = hand_key(remove_cards(cards, action_cards(action)))
                    if rest in seen:
                        continue
                    seen.add(rest)
                    candidate = [action] + solve(rest)
                    if len(candidate) < len(best):
                        best = candidate
                    if len(best) == lower:
                        break
                memo[cards] = best
                return best

            try:
                best_groups = solve(hand)
                exact = True
                if len(self.exact_cache) >= 5000:
                    self.exact_cache.clear()
                self.exact_cache[hand, trump] = best_groups
            except TimeoutError:
                pass
        unique = [best_groups]
        for groups in sorted(plans, key=len):
            if groups not in unique and len(unique) < alternatives:
                unique.append(groups)
        elapsed = (time.perf_counter() - started) * 1000
        return [HandPlan(groups, len(groups), exact and i == 0, nodes, elapsed,
                         "exact subset search" if exact and i == 0 else "bounded greedy decomposition")
                for i, groups in enumerate(unique)]

