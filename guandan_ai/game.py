"""A local human seat on the official table, with public-information AI seats."""

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
import random
import threading
import time

from .cards import deck
from .evaluation import episode_result, normalize_result
from .models import Decision, Observation, action_cards, is_pass
from .platform import REVISION
from .policies import decide_safely, make_policy
from .rules import load_environment


_TABLE_LOCK = threading.RLock()
PATTERNS = {"Single": "单张", "Pair": "对子", "Trips": "三张", "ThreeWithTwo": "三带二",
            "Straight": "顺子", "ThreePair": "三连对", "TwoTrips": "钢板",
            "Bomb": "炸弹", "StraightFlush": "同花顺", "FourKings": "四王炸",
            "PASS": "不出", "tribute": "进贡", "back": "还贡"}
PLAYERS = ("你", "右侧对手", "对家队友", "左侧对手")


def action_label(action):
    name = PATTERNS.get(action[0], action[0])
    return name if action[0] in ("PASS", "FourKings", "tribute", "back") else f"{name} · {str(action[1]).replace('T', '10')}"


class HumanGame:
    human_seat = 0

    def __init__(self, rules, strategy="team", seed=1, budget_ms=100, policy_names=None, first_player=0, player_names=None):
        self.rules, self.seed, self.budget_ms = rules, seed, budget_ms
        self.strategy = strategy
        self.policy_names = policy_names or ["hybrid", strategy, strategy, strategy]
        if len(self.policy_names) != 4:
            raise ValueError("需要四个座位的策略")
        self.policies = [make_policy(self.policy_names[i], i, seed * 101 + i, rules) for i in range(4)]
        self._random_state = random.Random(seed).getstate()
        self.events, self.rounds = [], []
        self.request = None
        self.round_over = self.match_over = False
        self.outcome = None
        self.steps = 0
        self.selected = set()
        self.hint_text = ""
        self.latest = {}
        self.last_plays = {}
        self.trick_no = 0
        self.last_lead_key = None
        self._cached_hint = None
        self._log("match_start", mode="human", seed=seed, strategies=["human", strategy, strategy, strategy],
                  platform_revision=REVISION)
        Environment = load_environment(rules)
        self.env = Environment(first_player=first_player)
        shoe = deck()
        random.Random(seed).shuffle(shoe)
        types = __import__(Environment.__module__.rsplit('.', 1)[0] + '.types', fromlist=['parse'])
        self.env.deck_data, self.env.deck = shoe[:], types.parse(shoe)
        for seat, name in enumerate(player_names or PLAYERS):
            self.env.add_player(name, seat)
        self._consume(self._call(self.env.start))

    def _call(self, function, *args):
        # Official reshuffles use the global RNG. Isolate each browser session's shoe.
        with _TABLE_LOCK:
            previous = random.getstate()
            random.setstate(self._random_state)
            try:
                return function(*args)
            finally:
                self._random_state = random.getstate()
                random.setstate(previous)

    def _log(self, kind, **data):
        self.events.append(deepcopy({"seq": len(self.events) + 1, "time": time.time(), "kind": kind, **data}))

    def _consume(self, packets):
        self.request = None
        for packet in packets:
            seat, message = packet.seat, packet.body
            self.policies[seat].observe(message)
            stage = message.get("stage")
            if message["type"] == "act":
                self.request = (seat, deepcopy(message))
            elif stage == "beginning":
                self._log("begin", recipient=seat, message=message, episode=len(self.rounds))
                if seat == 0:
                    self.latest = {}
                    self.last_plays = {}
                    self.trick_no = 0
                    self.last_lead_key = None
            elif seat == 0:
                self._log("public_event", message=message, episode=len(self.rounds))
                if stage == "play":
                    self.latest[message["curPos"]] = deepcopy(message["curAction"])
                    self.last_plays[message["curPos"]] = {"action": deepcopy(message["curAction"]), "trick": self.trick_no}
                elif stage == "episodeOver":
                    self.rounds.append(episode_result(message))
                    self.round_over = True
                elif stage == "gameResult":
                    self.outcome = normalize_result(message)
                    self.match_over = True
                elif stage == "gameOver":
                    self.match_over = True
        if self.request:
            obs = self.observation()
            if obs.stage == "play" and obs.greater_pos == -1:
                key = (len(self.rounds), self.steps)
                if key != self.last_lead_key:
                    self.trick_no += 1
                    self.latest = {}
                    self.last_lead_key = key

    def observation(self):
        if self.request is None:
            return None
        seat, message = self.request
        return Observation.from_message(message, seat, self.policies[seat].history.finished)

    @property
    def human_turn(self):
        return not self.round_over and not self.match_over and self.request is not None and self.request[0] == 0

    @property
    def hand(self):
        return self.env.players[0].hand2json()

    @property
    def rest(self):
        return [len(p.hand_cards) for p in self.env.players]

    @property
    def ranks(self):
        from .cards import RANKS
        return [RANKS[p.rank - 2] for p in self.env.players[:2]]

    def matching_actions(self, indices=None):
        if not self.human_turn:
            return []
        indices = self.selected if indices is None else indices
        if not indices or any(type(i) is not int or i < 0 or i >= len(self.hand) for i in indices):
            return []
        cards = Counter(self.hand[i] for i in indices)
        return [i for i, action in enumerate(self.observation().actions)
                if not is_pass(action) and Counter(action_cards(action)) == cards]

    def pass_index(self):
        if not self.human_turn:
            return None
        return next((i for i, a in enumerate(self.observation().actions) if is_pass(a)), None)

    def _apply(self, index, decision=None, human=False):
        obs = self.observation()
        if obs is None or self.round_over or self.match_over:
            raise ValueError("当前没有待执行的动作")
        if type(index) is not int or not 0 <= index < len(obs.actions):
            raise ValueError("请选择平台允许的合法动作")
        decision = decision or Decision(index, "玩家选择")
        self._log("decision", seat=obs.seat, strategy="human" if human else self.policy_names[obs.seat],
                  observation=asdict(obs), decision=decision.to_dict(), action=obs.actions[index], episode=len(self.rounds))
        self.steps += 1
        self.selected.clear()
        self.hint_text = ""
        self._cached_hint = None
        self._consume(self._call(self.env.loop, {"actIndex": index}))

    def play_selected(self, index=None):
        if not self.selected:
            raise ValueError("请先选择要出的牌，或点击“不出 / 要不起”")
        matches = self.matching_actions()
        if not matches or (index is not None and index not in matches):
            raise ValueError("所选牌不能组成当前允许的牌型，或不能压过上一手")
        self._apply(matches[0] if index is None else index, human=True)

    def pass_turn(self):
        index = self.pass_index()
        if index is None:
            raise ValueError("领出、进贡和还贡时不能不出")
        self._apply(index, human=True)

    def hint(self):
        if not self.human_turn:
            raise ValueError("轮到你时才能使用提示")
        if self._cached_hint is None:
            self._cached_hint = decide_safely(self.policies[0], self.observation(), self.budget_ms)
        decision = self._cached_hint
        action = self.observation().actions[decision.index]
        wanted = Counter(action_cards(action))
        self.selected = set()
        for i, card in enumerate(self.hand):
            if wanted[card]:
                self.selected.add(i)
                wanted[card] -= 1
        self.hint_text = f"建议{action_label(action)}：{decision.reason}"
        return decision

    def step_ai(self):
        if self.human_turn or self.round_over or self.match_over:
            return False
        if self.request is None:
            raise RuntimeError("平台没有返回下一行动请求")
        obs = self.observation()
        decision = decide_safely(self.policies[obs.seat], obs, self.budget_ms)
        self._apply(decision.index, decision)
        return True

    def next_round(self):
        if not self.round_over or self.match_over:
            raise ValueError("当前不能开始下一副")
        self.round_over = False
        self.selected.clear()
        self.hint_text = ""
        self._consume(self._call(self.env.loop))

    def export_log(self):
        if not (self.round_over or self.match_over):
            raise ValueError("请在本副结束后导出复盘，避免透露隐藏手牌")
        return '\n'.join(json.dumps(e, ensure_ascii=False) for e in self.events).encode('utf-8')
