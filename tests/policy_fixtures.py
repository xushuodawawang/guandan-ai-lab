import time

from guandan_ai.models import Decision


class SlowPolicy:
    def __init__(self, **kwargs):
        pass

    def decide(self, observation, budget_ms=200):
        time.sleep(10)
        return Decision(0, "deliberately slow test policy")

