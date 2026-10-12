"""Budgeted determinization search. This is an approximation, not an equilibrium solver."""
import json
import time
from dataclasses import dataclass

from .analysis import features
from .cards import hand_key, remove_cards
from .models import action_cards, is_pass


@dataclass(frozen=True)
class SearchState:
    hands: tuple[tuple[str, ...], ...]
    turn: int
    trump: str
    leader: int = -1
    greater: str = ""
    order: tuple[int, ...] = ()

    @property
    def terminal(self):
        return any((p + 2) % 4 in self.order for p in self.order)

    def play(self, action):
        if self.terminal:
            raise ValueError("不能在已结束状态出牌")
        hands = list(self.hands)
        order = self.order
        leader, greater = self.leader, self.greater
        if not is_pass(action):
            hands[self.turn] = hand_key(remove_cards(hands[self.turn], action_cards(action)))
            leader = self.turn
            greater = json.dumps(action, separators=(",", ":"))
            if not hands[self.turn] and self.turn not in order:
                order += (self.turn,)
        elif not greater:
            raise ValueError("领出时不能 PASS")
        state = SearchState(tuple(hands), self.turn, self.trump, leader, greater, order)
        if state.terminal:
            return state
        for step in range(1, 5):
            nxt = (self.turn + step) % 4
            if is_pass(action) and nxt == leader:
                lead = leader if hands[leader] else (leader + 2) % 4
                if not hands[lead]:
                    lead = next((leader + i) % 4 for i in range(1, 5) if hands[(leader + i) % 4])
                return SearchState(tuple(hands), lead, self.trump, -1, "", order)
            if hands[nxt]:
                return SearchState(tuple(hands), nxt, self.trump, leader, greater, order)
        raise ValueError("没有可行动玩家")

    def value(self, team: int):
        if self.terminal:
            order = list(self.order) + [i for i in range(4) if i not in self.order]
            partner_place = order.index((order[0] + 2) % 4)
            upgrade = 3 if partner_place == 1 else 2 if partner_place == 2 else 1
            return (120 + 8 * upgrade) * (1 if order[0] % 2 == team else -1)
        costs = [features(h, self.trump)["cost"] for h in self.hands]
        ours = [costs[p] for p in range(4) if p % 2 == team]
        theirs = [costs[p] for p in range(4) if p % 2 != team]
        finished_score = sum(15 if p % 2 == team else -15 for p in self.order)
        return 0.65 * (sum(theirs) - sum(ours)) + finished_score


class SearchBudget(Exception):
    pass


class EndgameSearch:
    def __init__(self, rules, max_depth=3, max_nodes=450, branch_limit=6):
        self.rules = rules
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.branch_limit = branch_limit

    def choose(self, obs, roots, samples, deadline):
        self.nodes = 0
        self.cache = {}
        completed_depth = 0
        best_values = {}
        cutoffs = 0

        def minimax(state, depth, alpha, beta):
            nonlocal cutoffs
            if time.perf_counter() >= deadline or self.nodes >= self.max_nodes:
                raise SearchBudget
            self.nodes += 1
            if state.terminal or depth == 0:
                return state.value(obs.seat % 2)
            key = (state, depth)
            original_alpha, original_beta = alpha, beta
            if key in self.cache:
                cached, bound = self.cache[key]
                if bound == "exact":
                    return cached
                if bound == "lower":
                    alpha = max(alpha, cached)
                else:
                    beta = min(beta, cached)
                if beta <= alpha:
                    return cached
            greater = json.loads(state.greater) if state.greater else None
            moves = self.rules.legal(state.hands[state.turn], state.trump, greater)
            transitions = [(a, state.play(a)) for a in moves]
            maximizing = state.turn % 2 == obs.seat % 2
            transitions.sort(key=lambda x: x[1].value(obs.seat % 2), reverse=maximizing)
            chosen = transitions[:self.branch_limit]
            # Always retain PASS and immediate finishes, even when candidate-pruning.
            for pair in transitions[self.branch_limit:]:
                if is_pass(pair[0]) or not pair[1].hands[state.turn]:
                    chosen.append(pair)
            value = float("-inf") if maximizing else float("inf")
            pruned = False
            for _, child in chosen:
                v = minimax(child, depth - 1, alpha, beta)
                if maximizing:
                    value = max(value, v)
                    alpha = max(alpha, value)
                else:
                    value = min(value, v)
                    beta = min(beta, value)
                if beta <= alpha:
                    pruned = True
                    cutoffs += 1
                    break
            # Fail-low/high values are bounds even if this node itself did not
            # cut off. Retain their type rather than treating them as exact.
            bound = "upper" if value <= original_alpha else "lower" if value >= original_beta else "exact"
            self.cache[key] = (value, bound)
            return value

        for depth in range(1, self.max_depth + 1):
            values = {i: [] for i in roots}
            try:
                for allocation in samples:
                    state = SearchState(tuple(hand_key(h) for h in allocation), obs.seat, obs.trump,
                        obs.greater_pos, "" if is_pass(obs.greater_action) else json.dumps(obs.greater_action, separators=(",", ":")),
                        tuple(obs.finished))
                    for index in roots:
                        values[index].append(minimax(state.play(obs.actions[index]), depth - 1, float("-inf"), float("inf")))
            except SearchBudget:
                break
            best_values = {i: sum(v) / len(v) for i, v in values.items()}
            completed_depth = depth
        meta = {"nodes": self.nodes, "completed_depth": completed_depth, "samples": len(samples),
                "cutoffs": cutoffs, "values": best_values,
                "approximate": True, "candidate_pruning": True}
        return (max(best_values, key=best_values.get) if best_values else None), meta

