"""Fair adapter of welkin03/guandan-ai's published MIT heuristic_policy.

All root legal actions come from OpenGuanDan. Opponent hands expose counts only;
optional upstream full-information guards are deliberately not invoked.
"""
import importlib
import sys
import types
from collections import Counter, defaultdict
from functools import lru_cache

from .models import Decision, action_cards, is_pass
from .platform import WELKIN_FILES, WELKIN_ROOT, blob_hash
from .policies import TeamPolicy


class CountOnlyHand:
    def __init__(self, count):
        self.count = count

    def __len__(self):
        return self.count

    def __iter__(self):
        raise RuntimeError("开源对照禁止读取其他玩家的隐藏手牌")

    def __getitem__(self, key):
        raise RuntimeError("开源对照禁止读取其他玩家的隐藏手牌")


@lru_cache(maxsize=1)
def load_baseline():
    for path, digest in WELKIN_FILES.items():
        file = WELKIN_ROOT / path
        if not file.is_file() or blob_hash(file.read_bytes()) != digest:
            raise RuntimeError("开源对照未下载或校验失败；先运行 python -m guandan_ai fetch-baseline")
    name = "_welkin_public_baseline"
    # Import only reviewed policy/rules modules, without the broad upstream __init__.
    package = types.ModuleType(name)
    package.__path__ = [str(WELKIN_ROOT / "src/guandan")]
    sys.modules[name] = package
    cards = importlib.import_module(name + ".cards")
    rules = importlib.import_module(name + ".rules")
    policy = importlib.import_module(name + ".policy")
    original = policy.min_play_count

    @lru_cache(maxsize=20000)
    def cached_min_count(hand, config, exact_limit=8):
        return original(tuple(sorted(hand)), config, exact_limit=exact_limit)
    policy.min_play_count = cached_min_count
    return cards, rules, policy


class WelkinPolicy(TeamPolicy):
    name = "welkin"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cards_module, self.rules_module, self.upstream = load_baseline()

    def _cards(self, labels):
        seen = Counter()
        out = []
        for code in labels:
            label = "X" if code == "SB" else "Y" if code == "HR" else code
            out.append(self.cards_module.parse_card(label, copy=seen[code]))
            seen[code] += 1
        return tuple(out)

    def decide(self, obs, budget_ms=200):
        if obs.stage != "play":
            result = super().decide(obs, budget_ms)
            result.reason = "开源策略没有进贡/还贡接口，使用平台交接适配：" + result.reason
            return result
        card_module, rules, upstream = self.cards_module, self.rules_module, self.upstream
        own = self._cards(obs.hand)
        pools = defaultdict(list)
        for label, card in zip(obs.hand, own):
            pools[label].append(card)
        types_map = {"Single": "SINGLE", "Pair": "PAIR", "Trips": "TRIPLE", "ThreeWithTwo": "TRIPLE_WITH_PAIR",
                     "Straight": "STRAIGHT", "ThreePair": "PAIR_STRAIGHT", "TwoTrips": "PLATE",
                     "Bomb": "BOMB", "StraightFlush": "STRAIGHT_FLUSH", "FourKings": "JOKER_BOMB"}

        def convert(action, ours=True):
            if is_pass(action):
                return rules.PASS
            labels = action_cards(action)
            if ours:
                used = Counter()
                cards = []
                for label in labels:
                    cards.append(pools[label][used[label]])
                    used[label] += 1
                cards = tuple(cards)
            else:
                cards = self._cards(labels)
            rank = action[1]
            if action[0] in ("Straight", "StraightFlush", "ThreePair", "TwoTrips"):
                length = 5 if action[0] in ("Straight", "StraightFlush") else 3 if action[0] == "ThreePair" else 2
                sequence = "A23456789TJQKA"
                rank = sequence[sequence.index(rank) + length - 1]
            main = None if action[0] == "FourKings" else card_module.Rank.SMALL_JOKER if rank == "B" else card_module.Rank.BIG_JOKER if rank == "R" else card_module.Rank(rank)
            return rules.Action(getattr(rules.ActionType, types_map[action[0]]), cards, main, len(cards))

        actions = [convert(a) for a in obs.actions]
        class PublicState:
            def legal_actions(self):
                return actions
        state = PublicState()
        state.hands = tuple(own if p == obs.seat else CountOnlyHand(obs.rest[p]) for p in range(4))
        state.current_player = obs.seat
        state.config = rules.RuleConfig(level_rank=card_module.Rank(obs.trump))
        state.finished = obs.finished
        leading = not any(is_pass(action) for action in obs.actions)
        state.last_actor = None if leading or is_pass(obs.greater_action) else obs.greater_pos
        state.last_action = None if leading or is_pass(obs.greater_action) else convert(obs.greater_action, ours=False)
        selected = upstream.heuristic_policy(state, self.rng)
        index = next(i for i, action in enumerate(actions) if action == selected)
        return Decision(index, "welkin03/guandan-ai 已发布启发式策略；只暴露本人手牌、公开剩余张数与平台合法动作")
