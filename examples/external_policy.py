"""Integration contract example, not a claim of an existing strong Guandan AI."""
from guandan_ai.models import Decision


class FirstLegalPolicy:
    def __init__(self, seat, seed, rules, config):
        self.seat = seat
        self.history = []

    def observe(self, event):
        if event.get("stage") == "beginning":
            self.history = []
        self.history.append(event)

    def decide(self, observation, budget_ms=200):
        return Decision(0, "插件示例：选择第一个合法动作。此示例不代表教师或既有强 AI。")

