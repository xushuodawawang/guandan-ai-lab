import math
import random
from collections import Counter

from .cards import deck, rank_value
from .models import Observation, action_cards, is_pass


class BeliefError(ValueError):
    pass


class PublicHistory:
    """One player perspective; never accepts opponents' starting hands."""

    def __init__(self, seat: int):
        self.seat = seat
        self.reset()

    def reset(self):
        self.played = Counter()
        self.known = [Counter() for _ in range(4)]
        self.passes = []
        self.events = []
        self.rest = [27] * 4
        self.finished = []
        self.episode = 0

    def observe(self, message: dict):
        stage = message.get("stage")
        if message.get("type") == "notify" and stage == "beginning":
            episode = self.episode + 1
            self.reset()
            self.episode = episode
            return
        if message.get("type") == "act":
            if "publicInfo" in message:
                self.rest = [int(p["rest"]) for p in message["publicInfo"]]
            self.rest[self.seat] = len(message["handCards"])
            return
        if message.get("type") != "notify":
            return
        if stage == "play":
            pos = int(message["curPos"])
            action = message["curAction"]
            if is_pass(action):
                if not is_pass(message.get("greaterAction")):
                    self.passes.append({"seat": pos, "action": message["greaterAction"],
                                        "leader": message.get("greaterPos", -1)})
            else:
                cards = Counter(action_cards(action))
                self.played.update(cards)
                self.known[pos] -= cards
                self.rest[pos] = max(0, self.rest[pos] - sum(cards.values()))
                if not self.rest[pos] and pos not in self.finished:
                    self.finished.append(pos)
        elif stage in ("tribute", "back"):
            for giver, receiver, card in message.get("result", []):
                self.known[giver] -= Counter([card])
                self.known[receiver].update([card])
                self.rest[giver] -= 1
                self.rest[receiver] += 1
        if stage in ("play", "tribute", "back", "anti-tribute"):
            self.events.append(message)

    def unseen(self, obs: Observation) -> Counter:
        remaining = Counter(deck())
        remaining.subtract(self.played)
        remaining.subtract(obs.hand)
        if any(v < 0 for v in remaining.values()):
            raise BeliefError("公开记牌与手牌数量冲突")
        remaining = +remaining
        if sum(remaining.values()) != sum(obs.rest) - len(obs.hand):
            raise BeliefError("记牌事件不完整：未知牌数量与剩余张数不一致")
        return remaining

    def sample(self, obs: Observation, rng: random.Random, count=8, behavior=True) -> list[list[list[str]]]:
        unseen = self.unseen(obs)
        fixed = [Counter() for _ in range(4)]
        for seat in range(4):
            if seat != self.seat:
                fixed[seat] = +self.known[seat]
                if sum(fixed[seat].values()) > obs.rest[seat]:
                    raise BeliefError("已知交接牌多于玩家剩余张数")
                unseen.subtract(fixed[seat])
        if any(v < 0 for v in unseen.values()):
            raise BeliefError("交接牌约束与未见牌冲突")
        bag = list((+unseen).elements())
        samples, weights = [], []
        for _ in range(max(1, count * (3 if behavior else 1))):
            rng.shuffle(bag)
            allocation = [[] for _ in range(4)]
            allocation[self.seat] = list(obs.hand)
            offset = 0
            for seat in range(4):
                if seat == self.seat:
                    continue
                n = obs.rest[seat] - sum(fixed[seat].values())
                allocation[seat] = list(fixed[seat].elements()) + bag[offset:offset + n]
                offset += n
            samples.append(allocation)
            weights.append(self._weight(allocation, obs.trump) if behavior else 1.0)
        if behavior:
            return rng.choices(samples, weights=weights, k=count)
        return samples[:count]

    def _weight(self, allocation, trump):
        log_weight = 0.0
        for event in self.passes[-12:]:
            # PASS is only a weak behavioral likelihood. Never rules out an allocation.
            if (event["seat"] - event["leader"]) % 2 == 0:
                continue
            action = event["action"]
            if action[0] not in ("Single", "Pair", "Trips"):
                continue
            need = {"Single": 1, "Pair": 2, "Trips": 3}[action[0]]
            counts = Counter(c[1] for c in allocation[event["seat"]])
            if any(n >= need and rank_value(r, trump) > rank_value(action[1], trump) for r, n in counts.items()):
                log_weight -= 0.25
        return max(0.05, math.exp(log_weight))

    def summary(self, obs: Observation, rng=None, count=30, behavior=True):
        rng = rng or random.Random(0)
        samples = self.sample(obs, rng, count, behavior)
        rows = []
        for seat in range(4):
            if seat == obs.seat:
                continue
            for rank in "23456789TJQKABR":
                counts = [sum(c[1] == rank for c in s[seat]) for s in samples]
                rows.append({"seat": seat, "rank": rank, "expected_count": sum(counts) / len(counts),
                             "p_pair": sum(n >= 2 for n in counts) / len(counts),
                             "p_bomb": sum(n >= 4 for n in counts) / len(counts)})
        return rows

