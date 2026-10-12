from dataclasses import asdict, dataclass, field
from copy import deepcopy
from typing import Any

Action = list[Any]
PASS = ["PASS", "PASS", "PASS"]


def is_pass(action: Action | None) -> bool:
    return not action or action[0] in (None, "PASS")


def action_cards(action: Action) -> list[str]:
    return [] if is_pass(action) else list(action[2])


@dataclass
class Observation:
    seat: int
    hand: list[str]
    rest: tuple[int, int, int, int]
    trump: str
    actions: list[Action]
    stage: str = "play"
    greater_pos: int = -1
    greater_action: Action | None = None
    self_rank: str = "2"
    oppo_rank: str = "2"
    finished: tuple[int, ...] = ()
    wire: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_message(cls, msg: dict, seat: int, finished=()) -> "Observation":
        if seat not in range(4):
            raise ValueError("座位必须是 0..3")
        if not msg.get("actionList"):
            raise ValueError("服务器没有提供可选合法动作")
        rest = [int(p.get("rest", 27)) for p in msg.get("publicInfo", [{}] * 4)]
        if len(rest) != 4 or any(n < 0 for n in rest):
            raise ValueError("publicInfo 剩余张数格式不正确")
        rest[seat] = len(msg["handCards"])
        obs = cls(seat, list(msg["handCards"]), tuple(rest), str(msg.get("curRank", "2")),
                   deepcopy(msg["actionList"]), msg.get("stage", "play"), int(msg.get("greaterPos", -1)),
                   msg.get("greaterAction"), str(msg.get("selfRank", "2")),
                   str(msg.get("oppoRank", "2")), tuple(finished), deepcopy(msg))
        # Upstream can carry an old greaterAction through tribute/back while
        # generating a fresh lead list. The actual legal list is authoritative.
        if obs.stage == "play" and not any(is_pass(a) for a in obs.actions):
            obs.greater_pos, obs.greater_action = -1, None
        return obs

    @property
    def teammate(self) -> int:
        return (self.seat + 2) % 4


@dataclass
class Decision:
    index: int
    reason: str
    scores: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0.0
    search: dict = field(default_factory=dict)
    fallback: bool = False

    def to_dict(self):
        return asdict(self)


def validate_decision(decision: Decision, obs: Observation) -> Decision:
    if not isinstance(decision.index, int) or not 0 <= decision.index < len(obs.actions):
        raise ValueError("策略返回了非法动作索引")
    return decision

