"""Process isolation for online decisions; timed-out solvers are terminated."""
import multiprocessing as mp
import os
import signal
import time

from .models import Decision, validate_decision
from .policies import GreedyPolicy, decide_safely, make_policy
from .rules import JavaRules


def _worker(conn, name, seat, seed, config):
    rules = JavaRules()
    try:
        try:
            rules.legal(["S3"], "2")
            usable = rules
        except RuntimeError:
            usable = None
        policy = make_policy(name, seat, seed, usable, config)
        conn.send({"ready": True, "java_pid": rules.proc.pid if rules.proc else None})
        while True:
            command, value = conn.recv()
            if command == "close":
                break
            if command == "observe":
                if callable(getattr(policy, "observe", None)):
                    policy.observe(value)
            elif command == "decide":
                obs, budget = value
                conn.send(decide_safely(policy, obs, budget))
    except EOFError:
        pass
    except Exception as exc:
        try:
            conn.send({"error": f"{type(exc).__name__}: {exc}"})
        except (OSError, BrokenPipeError):
            pass
    finally:
        rules.close()
        conn.close()


class ProcessPolicy:
    STARTUP_TIMEOUT = 20
    def __init__(self, name, seat, seed=0, config=None):
        self.name, self.seat, self.seed, self.config = name, seat, seed, config
        self.process = self.conn = None
        self.java_pid = None
        self.events = []

    def start(self):
        if self.process is not None and self.process.is_alive():
            return
        self.close()
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.process = ctx.Process(target=_worker, args=(child, self.name, self.seat, self.seed, self.config), daemon=True)
        self.process.start()
        child.close()
        if not self.conn.poll(self.STARTUP_TIMEOUT):
            self.close()
            raise RuntimeError("策略子进程启动超时")
        ready = self.conn.recv()
        if not ready.get("ready"):
            self.close()
            raise RuntimeError(f"策略启动失败: {ready}")
        self.java_pid = ready.get("java_pid")
        for event in self.events:
            self.conn.send(("observe", event))

    def observe(self, event):
        if event.get("stage") == "beginning" and event.get("type") == "notify":
            self.events = []
        self.events.append(event)
        if self.process and self.process.is_alive():
            self.conn.send(("observe", event))

    def decide(self, obs, budget_ms=200):
        started = time.perf_counter()
        if self.process is None or not self.process.is_alive():
            decision = GreedyPolicy(obs.seat).decide(obs)
            decision.reason = "策略进程重启期间，使用合法贪心回退"
            decision.fallback = True
            return decision
        self.conn.send(("decide", (obs, budget_ms)))
        # Allow small IPC overhead beyond the solver's own soft target.
        if self.conn.poll(budget_ms / 1000 + .08):
            reply = self.conn.recv()
            if isinstance(reply, Decision):
                reply.elapsed_ms = (time.perf_counter() - started) * 1000
                return validate_decision(reply, obs)
        self.close()
        decision = GreedyPolicy(obs.seat).decide(obs)
        decision.reason = "超过在线预算或进程异常，终止搜索并使用合法贪心回退"
        decision.fallback = True
        decision.elapsed_ms = (time.perf_counter() - started) * 1000
        return decision

    def close(self):
        if self.process:
            if self.process.is_alive():
                try:
                    self.conn.send(("close", None))
                except (OSError, BrokenPipeError):
                    pass
                self.process.join(timeout=.1)
                if self.process.is_alive():
                    if self.java_pid:
                        try:
                            os.kill(self.java_pid, signal.SIGTERM)
                        except OSError:
                            pass
                    self.process.terminate()
                    self.process.join(timeout=2)
            self.process.close()
        if self.conn:
            self.conn.close()
        self.process = self.conn = None
        self.java_pid = None

