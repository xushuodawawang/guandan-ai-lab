"""Authoritative four-seat rooms, private views, recovery and independent AI turns."""
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import secrets
import threading
import time

from .cards import format_card, RANKS
from .game import HumanGame, action_label
from .llm import profiles, public_profiles
from .models import Decision, action_cards, is_pass
from .policies import GreedyPolicy, decide_safely
from .room_store import RoomStore


class StaleAction(ValueError):
    pass


@dataclass
class Participant:
    id: str
    nickname: str = "玩家"
    room_id: str | None = None
    seat: int | None = None
    membership: int = 0
    seen: float = field(default_factory=time.monotonic)
    sockets: int = 0
    receipts: OrderedDict = field(default_factory=OrderedDict)


@dataclass
class Seat:
    kind: str = "open"
    participant: str | None = None
    nickname: str = "等待加入"
    ready: bool = False
    autopilot: bool = False
    online: bool = False


@dataclass
class SharedRoom:
    id: str
    owner: str
    seats: list = field(default_factory=lambda: [Seat() for _ in range(4)])
    game: HumanGame | None = None
    seed: int | None = None
    generation: int = 0
    version: int = 0
    trace: list = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    pending: object = None
    due: float = 0
    ai_after: float = 0
    paused: bool = False
    pause_at: float = 0
    delay: float = 1.4
    fault: str = ""


class RoomService:
    def __init__(self, rules, *, store_path=None, human_timeout=90, ai_delay=1.4, auto_start=True, clock=time.monotonic):
        if human_timeout <= 0 or ai_delay < 0:
            raise ValueError("回合时间必须为正，AI 延迟不能为负")
        self.rules, self.clock = rules, clock
        self.human_timeout, self.ai_delay = human_timeout, ai_delay
        self.rooms, self.users = {}, {}
        self.lock = threading.RLock()
        self.changed = threading.Condition()
        self.store = RoomStore(store_path)
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="room-ai")
        self.capacity = threading.BoundedSemaphore(8)
        self.stop = threading.Event()
        self.thread = None
        self._restore()
        if auto_start:
            self.thread = threading.Thread(target=self._run, daemon=True, name="room-clock")
            self.thread.start()

    def identity(self, token=None):
        with self.lock:
            uid = hashlib.sha256((token or "").encode()).hexdigest()
            if uid not in self.users:
                token = secrets.token_urlsafe(32)
                uid = hashlib.sha256(token.encode()).hexdigest()
                self.users[uid] = Participant(uid)
                self._save_user(self.users[uid])
            user = self.users[uid]
            user.seen = self.clock()
            return token, user

    def _save_user(self, user):
        self.store.put("participant", user.id, {"id": user.id, "nickname": user.nickname,
            "room_id": user.room_id, "seat": user.seat, "membership": user.membership})

    def _save_room(self, room):
        self.store.put("room", room.id, {"id": room.id, "owner": room.owner,
            "seats": [asdict(s) for s in room.seats], "seed": room.seed,
            "generation": room.generation, "version": room.version, "trace": room.trace,
            "active": room.game is not None, "paused": room.paused, "delay": room.delay})

    def _restore(self):
        for data in self.store.all("participant"):
            user = Participant(**data)
            user.seen = -1e9
            self.users[user.id] = user
        for data in self.store.all("room"):
            room = SharedRoom(data["id"], data["owner"], [Seat(**s) for s in data["seats"]])
            room.seed, room.generation, room.version = data["seed"], data["generation"], data["version"] + 1
            room.trace = data["trace"]
            room.paused, room.delay = data.get("paused", False), data.get("delay", 1.4)
            room.pause_at = self.clock()
            for seat in room.seats:
                seat.online = False
            self.rooms[room.id] = room
            if data["active"]:
                try:
                    room.game = self._build_game(room)
                    for event in room.trace:
                        if event["op"] == "next":
                            room.game.next_round()
                        else:
                            room.game._apply(event["index"], Decision(**event["decision"]), human=event["human"])
                    self._reset_clock(room)
                except Exception:
                    room.game = None
                    room.fault = "牌局恢复失败，请房主重新开始"

    @staticmethod
    def revision(room):
        return f"{room.id}:{room.generation}:{room.version}"

    def _bump(self, room):
        room.version += 1
        self._save_room(room)
        with self.changed:
            self.changed.notify_all()

    def _reset_clock(self, room):
        if room.paused:
            room.pause_at = self.clock()
        room.due = self.clock() + self.human_timeout
        room.ai_after = self.clock() + room.delay

    def _build_game(self, room):
        names = ["hybrid" if s.participant else s.kind for s in room.seats]
        owner_seat = next((i for i, s in enumerate(room.seats) if s.participant == room.owner), 0)
        return HumanGame(self.rules, seed=room.seed, policy_names=names, first_player=owner_seat,
                         player_names=[s.nickname for s in room.seats])

    @staticmethod
    def _name(value):
        if not isinstance(value, str):
            raise ValueError("昵称无效")
        result = "".join(c for c in value.strip() if c.isprintable())[:16]
        return result or "玩家"

    def _new_room(self, user, nickname):
        if user.room_id in self.rooms:
            raise ValueError("请先离开当前房间")
        if len(self.rooms) >= 64:
            raise ValueError("房间数量已达上限")
        code = str(secrets.randbelow(900000) + 100000)
        while code in self.rooms:
            code = str(secrets.randbelow(900000) + 100000)
        room = SharedRoom(code, user.id, delay=self.ai_delay)
        user.nickname = self._name(nickname)
        room.seats[0] = Seat("human", user.id, user.nickname, True, online=True)
        user.room_id, user.seat = code, 0
        user.membership += 1
        self.rooms[code] = room
        self._save_user(user)
        self._bump(room)
        return room

    def _start(self, room):
        if any(s.participant and not s.ready for s in room.seats):
            raise ValueError("仍有真人玩家未准备")
        for seat in room.seats:
            if seat.kind.startswith("llm:"):
                profile = profiles().get(seat.kind[4:])
                if profile is None or not profile.ready:
                    raise ValueError("大模型尚未配置，请在服务器设置密钥和模型")
        for seat in room.seats:
            if seat.participant:
                seat.autopilot = False
            if seat.kind == "open":
                seat.kind, seat.nickname, seat.ready = "team", "团队 AI", True
        room.seed = secrets.randbits(128)
        room.game = self._build_game(room)
        room.trace, room.pending, room.fault = [], None, ""
        room.generation += 1
        room.paused = False
        self._reset_clock(room)

    def dispatch(self, user, command, payload):
        if not isinstance(payload, dict):
            raise ValueError("请求应为 JSON 对象")
        command_id = payload.get("command_id")
        if not isinstance(command_id, str) or not 8 <= len(command_id) <= 96:
            raise ValueError("请求缺少有效 command_id")
        fingerprint = hashlib.sha256(json.dumps([command, payload], sort_keys=True).encode()).hexdigest()
        with self.lock:
            if command_id in user.receipts:
                saved = user.receipts[command_id]
                if saved[0] != fingerprint:
                    raise ValueError("同一请求编号不能用于不同操作")
                return {"state": self.snapshot(user), "duplicate": True, **saved[1]}
            room = self.rooms.get(user.room_id)
            if command == "create":
                room = self._new_room(user, payload.get("nickname", user.nickname))
            elif command == "join":
                code = str(payload.get("code", ""))
                if user.room_id == code and code in self.rooms:
                    room = self.rooms[code]
                else:
                    if user.room_id in self.rooms:
                        raise ValueError("请先离开当前房间")
                    room = self.rooms.get(code)
                    if room is None:
                        raise ValueError("房间码不存在")
                    with room.lock:
                        if room.game:
                            raise ValueError("比赛已开始，无法加入新座位")
                        index = payload.get("seat")
                        if index is None:
                            index = next((i for i, s in enumerate(room.seats) if s.kind == "open" and not s.participant), None)
                        if type(index) is not int or index not in range(4) or room.seats[index].kind != "open" or room.seats[index].participant:
                            raise ValueError("所选座位无法加入")
                        user.nickname = self._name(payload.get("nickname", user.nickname))
                        room.seats[index] = Seat("human", user.id, user.nickname, online=True)
                        user.room_id, user.seat = code, index
                        user.membership += 1
                        self._save_user(user)
                        self._bump(room)
            elif command == "new" and room is None:
                room = self._new_room(user, payload.get("nickname", user.nickname))
                with room.lock:
                    strategy = payload.get("strategy", "team")
                    if strategy not in ("greedy", "team", "hybrid"):
                        raise ValueError("请选择有效难度")
                    for i in range(1, 4):
                        room.seats[i] = Seat(strategy, nickname="本地 AI", ready=True)
                    self._start(room)
                    self._bump(room)
            elif room is None:
                raise ValueError("请先创建或加入房间")
            else:
                with room.lock:
                    if payload.get("room_id") != room.id:
                        raise ValueError("房间身份不匹配")
                    if payload.get("revision") != self.revision(room):
                        raise StaleAction("回合已更新，请按当前牌桌重新操作")
                    self._room_command(room, user, command, payload)
            # Hints are calculated without holding the room or registry lock.
            if command != "hint":
                user.receipts[command_id] = (fingerprint, {})
                while len(user.receipts) > 64:
                    user.receipts.popitem(last=False)
                return {"state": self.snapshot(user)}
            expected = self.revision(room)
            obs, policy = deepcopy(room.game.observation()), room.game.policies[user.seat]
        decision = decide_safely(policy, obs, 100)
        with self.lock, room.lock:
            if self.revision(room) != expected:
                raise StaleAction("提示期间回合已改变")
            wanted, selected = Counter(action_cards(obs.actions[decision.index])), []
            for i, card in enumerate(obs.hand):
                if wanted[card]:
                    selected.append(i)
                    wanted[card] -= 1
            extra = {"selected": selected, "hint": decision.reason}
            user.receipts[command_id] = (fingerprint, extra)
            return {"state": self.snapshot(user), **extra}

    def _room_command(self, room, user, command, payload):
        seat = room.seats[user.seat]
        if seat.participant != user.id:
            raise ValueError("座位身份已失效")
        owner = room.owner == user.id
        game = room.game
        if command in ("start", "new", "reset", "configure", "next", "pause", "speed", "step") and not owner:
            raise ValueError("此操作需要房主执行")
        if command == "ready":
            if game:
                raise ValueError("牌局已经开始")
            seat.ready = bool(payload.get("ready", True))
        elif command == "configure":
            if game:
                raise ValueError("请先返回房间设置")
            index, kind = payload.get("seat"), payload.get("kind")
            if type(index) is not int or index not in range(4):
                raise ValueError("座位无效")
            if room.seats[index].participant:
                raise ValueError("不能替换已入座的真人")
            if kind not in ("open", "greedy", "team", "hybrid") and kind not in [p["id"] for p in public_profiles()]:
                raise ValueError("座位类型无效")
            labels = {"open": "等待加入", "greedy": "休闲 AI", "team": "团队 AI", "hybrid": "综合 AI"}
            labels.update({p["id"]: p["label"] for p in public_profiles()})
            room.seats[index] = Seat(kind, nickname=labels[kind], ready=kind != "open")
        elif command in ("start", "new"):
            self._start(room)
        elif command == "reset":
            room.game, room.pending = None, None
            room.trace = []
            room.generation += 1
        elif command == "leave":
            user.room_id, user.seat = None, None
            user.membership += 1
            seat.participant = None
            seat.kind, seat.nickname, seat.autopilot = ("team", "托管 AI", False) if game else ("open", "等待加入", False)
            if owner:
                room.owner = next((s.participant for s in room.seats if s.participant), "")
            self._save_user(user)
        elif command == "autopilot":
            seat.autopilot = bool(payload.get("enabled", True))
            if game and game.request and game.request[0] == user.seat:
                self._reset_clock(room)
        elif command == "pause":
            if sum(bool(s.participant) for s in room.seats) > 1:
                raise ValueError("多人对局不支持暂停整桌")
            paused = bool(payload.get("paused", True))
            if paused and not room.paused:
                room.pause_at = self.clock()
            elif not paused and room.paused:
                room.due += self.clock() - room.pause_at
                room.ai_after = self.clock() + room.delay
            room.paused = paused
        elif command == "speed":
            delay = payload.get("delay")
            if not isinstance(delay, (int, float)) or not .5 <= delay <= 5:
                raise ValueError("AI 节奏需在 0.5–5 秒之间")
            room.delay = float(delay)
            room.ai_after = self.clock() + room.delay
        elif command == "next":
            if not game:
                raise ValueError("牌局尚未开始")
            game.next_round()
            room.trace.append({"op": "next"})
            self._reset_clock(room)
        elif command == "step":
            if not room.paused:
                raise ValueError("请先暂停单人对局")
            room.ai_after = -1
        elif command in ("play", "pass", "hint"):
            if not game or game.round_over or game.match_over or not game.request or game.request[0] != user.seat:
                raise ValueError("请等待轮到你的回合")
            if seat.autopilot:
                raise ValueError("请先取消托管再出牌")
            obs = game.observation()
            if command == "hint":
                return
            if command == "pass":
                index = next((i for i, a in enumerate(obs.actions) if is_pass(a)), None)
                if index is None:
                    raise ValueError("领出、进贡或还贡时必须出牌")
            else:
                chosen = payload.get("selected", [])
                if not chosen:
                    raise ValueError("请先选择要出的牌，或点击不出")
                if not isinstance(chosen, list) or len(chosen) > 27 or any(type(i) is not int or i < 0 or i >= len(obs.hand) for i in chosen) or len(set(chosen)) != len(chosen):
                    raise ValueError("选牌无效")
                cards = Counter(obs.hand[i] for i in chosen)
                matches = [i for i, a in enumerate(obs.actions) if not is_pass(a) and Counter(action_cards(a)) == cards]
                index = payload.get("action_index", matches[0] if matches else None)
                if type(index) is not int or index not in matches:
                    raise ValueError("所选牌不是当前合法动作")
            self._apply(room, index, Decision(index, "真人出牌"), human=True)
            return
        else:
            raise ValueError("未知操作")
        self._bump(room)

    def _apply(self, room, index, decision, human=False):
        try:
            room.game._apply(index, decision, human=human)
        except Exception:
            room.fault = "裁判处理失败，牌局已暂停。请房主重新开局"
            room.pending = None
            self._bump(room)
            raise ValueError(room.fault) from None
        room.trace.append({"op": "act", "index": index, "decision": decision.to_dict(), "human": human})
        room.pending = None
        self._reset_clock(room)
        self._bump(room)

    def snapshot(self, user):
        with self.lock:
            room = self.rooms.get(user.room_id)
            if room is None:
                return {"active": False, "revision": "none:0:0", "session_version": user.membership, "room": None, "nickname": user.nickname,
                        "providers": public_profiles()}
            with room.lock:
                viewer = user.seat
                if viewer not in range(4) or room.seats[viewer].participant != user.id:
                    raise ValueError("座位身份已失效")
                relative = lambda s: (s - viewer) % 4 if s >= 0 else -1
                game, owner = room.game, room.owner == user.id
                base = {"active": bool(game), "revision": self.revision(room), "session_version": user.membership, "nickname": user.nickname,
                    "providers": public_profiles(), "room": {"code": room.id, "your_seat": viewer,
                        "owner": owner, "paused": room.paused, "delay": room.delay,
                        "seats": [{"seat": i, "nickname": s.nickname, "kind": s.kind, "ready": s.ready,
                            "occupied": bool(s.participant), "you": s.participant == user.id,
                            "owner": s.participant == room.owner, "online": s.online,
                            "autopilot": s.autopilot} for i, s in enumerate(room.seats)]}, "fault": room.fault}
                if not game:
                    return base
                obs = game.observation()
                manual = bool(obs and obs.seat == viewer and not room.seats[viewer].autopilot and not game.round_over and not game.match_over)
                hand = game.env.players[viewer].hand2json()
                finished = game.rounds[-1]["order"] if game.round_over else game.policies[viewer].history.finished
                episode = len(game.rounds) - int(game.round_over)
                timeline = []
                for e in game.events:
                    if e["kind"] != "public_event" or e["episode"] != episode:
                        continue
                    msg = e["message"]
                    if msg["stage"] == "play":
                        timeline.append({"id": e["seq"], "seat": relative(msg["curPos"]), "action": msg["curAction"], "label": action_label(msg["curAction"])})
                    elif msg["stage"] in ("tribute", "back"):
                        for giver, receiver, card in msg["result"]:
                            timeline.append({"id": e["seq"], "seat": relative(giver), "action": [msg["stage"], msg["stage"], [card]], "label": f"向{room.seats[receiver].nickname}{'进贡' if msg['stage']=='tribute' else '还贡'}"})
                result = deepcopy(game.rounds[-1]) if game.round_over else None
                if result:
                    result["winner"] = int(result["winner"] != viewer % 2)
                    result["order"] = [relative(s) for s in result["order"]]
                actions = obs.actions if manual else []
                base.update({"steps": game.steps, "generation": room.generation,
                    "trump": game.env.rank, "ranks": [RANKS[game.env.players[viewer].rank-2], RANKS[game.env.players[(viewer+1)%4].rank-2]],
                    "episode": episode + 1, "trick": game.trick_no,
                    "hand": [{"id": i, "code": c, "name": format_card(c), "wild": c == "H"+game.env.rank} for i, c in enumerate(hand)],
                    "players": [{"seat": i, "actual_seat": (viewer+i)%4, "name": room.seats[(viewer+i)%4].nickname,
                        "rest": game.rest[(viewer+i)%4], "ally": i % 2 == 0,
                        "online": room.seats[(viewer+i)%4].online, "kind": room.seats[(viewer+i)%4].kind,
                        "place": finished.index((viewer+i)%4)+1 if (viewer+i)%4 in finished else None,
                        "last": game.last_plays.get((viewer+i)%4)} for i in range(4)],
                    "turn": relative(obs.seat) if obs and not game.round_over else None,
                    "stage": obs.stage if obs else "play", "human_turn": manual,
                    "leader": relative(obs.greater_pos) if obs and not game.round_over and obs.stage == "play" else -1,
                    "leading_action": obs.greater_action if obs and not game.round_over and obs.stage == "play" else None,
                    "actions": actions, "can_pass": manual and any(is_pass(a) for a in actions),
                    "cannot_beat": manual and all(is_pass(a) for a in actions), "round_over": game.round_over,
                    "match_over": game.match_over, "result": result, "timeline": timeline[-100:],
                    "thinking": bool(room.pending), "turn_seconds": max(0, room.due-(room.pause_at if room.paused else self.clock())),
                    "autopilot": room.seats[viewer].autopilot,
                    "revealed_hands": [game.env.players[(viewer+i)%4].hand2json() for i in range(4)] if game.round_over else None,
                    "stats": self.stats(room)})
                return base

    @staticmethod
    def stats(room):
        from .llm import model_statistics
        moves = [e for e in room.trace if e["op"] == "act"]
        return {"decisions": len(moves), "fallbacks": sum(e["decision"].get("fallback", False) for e in moves),
                **model_statistics([e["decision"] for e in moves])}

    def export(self, user):
        room = self.rooms.get(user.room_id)
        if room is None or room.game is None:
            raise ValueError("还没有牌局日志")
        with room.lock:
            if not room.game.round_over:
                raise ValueError("本副结束后才能下载复盘")
            events = deepcopy(room.game.events)
            for event in events:
                event.pop("seed", None)  # A live match's future shuffle must remain private.
            return "\n".join(json.dumps(e, ensure_ascii=False) for e in events).encode()

    def tick(self):
        with self.lock:
            rooms = list(self.rooms.values())
        for room in rooms:
            with room.lock:
                now = self.clock()
                dirty = False
                for seat in room.seats:
                    user = self.users.get(seat.participant)
                    online = bool(user and (user.sockets or now-user.seen < 8))
                    if seat.online != online:
                        seat.online, dirty = online, True
                if dirty:
                    self._bump(room)
                game = room.game
                if not game or game.round_over or game.match_over or room.fault:
                    continue
                if room.pending:
                    job = room.pending
                    if job["turn_key"] != self._turn_key(room):
                        room.pending = None
                        job["future"].cancel()
                        continue
                    if job["future"].done():
                        try:
                            decision = job["future"].result()
                        except Exception:
                            decision = GreedyPolicy(job["obs"].seat).decide(job["obs"])
                            decision.fallback, decision.reason = True, "策略进程异常，合法回退"
                    elif now > job["deadline"]:
                        decision = GreedyPolicy(job["obs"].seat).decide(job["obs"])
                        decision.fallback, decision.reason = True, "超时结果丢弃，合法回退"
                        decision.search = {"provider": job.get("provider"), "timed_out": True}
                        job["future"].cancel()
                    else:
                        continue
                    self._apply(room, decision.index, decision)
                    continue
                obs = game.observation()
                if not obs:
                    continue
                seat = room.seats[obs.seat]
                timed_out = bool(seat.participant and not seat.autopilot and now > room.due)
                bot = not seat.participant or seat.autopilot or timed_out
                single_step = room.paused and room.ai_after == -1
                if not bot or (room.paused and not single_step) or (now < room.ai_after and not timed_out):
                    continue
                if not self.capacity.acquire(blocking=False):
                    continue
                if timed_out:
                    seat.autopilot = True
                name = "hybrid" if seat.participant or seat.kind == "human" else seat.kind
                obs = deepcopy(obs)
                replay = [e["message"] for e in game.events if (e["kind"] == "begin" and e["recipient"] == obs.seat) or e["kind"] == "public_event"]
                profile = profiles().get(name[4:]) if name.startswith("llm:") else None
                future = self.pool.submit(self._decide, game, obs, name, replay)
                future.add_done_callback(lambda _: self.capacity.release())
                room.pending = {"future": future, "obs": obs, "turn_key": self._turn_key(room),
                    "deadline": now + (profile.timeout+6 if profile else 5), "provider": profile.id if profile else None}

    @staticmethod
    def _turn_key(room):
        obs = room.game.observation()
        seat = room.seats[obs.seat] if obs else None
        return (room.generation, room.game.steps, len(room.game.rounds), room.paused,
                seat.participant if seat else None, seat.kind if seat else None, seat.autopilot if seat else None)

    def _decide(self, game, obs, name, replay):
        if name.startswith("llm:"):
            from .worker import ProcessPolicy
            worker = ProcessPolicy(name, obs.seat, game.seed)
            try:
                worker.events = replay
                worker.start()
                decision = worker.decide(obs, profiles()[name[4:]].timeout * 1000)
                if not decision.search.get("provider"):
                    profile = profiles()[name[4:]]
                    decision.search.update(provider=profile.id, model=profile.model, request_attempted=False)
                return decision
            finally:
                worker.close()
        return decide_safely(game.policies[obs.seat], obs, 100)

    def _run(self):
        while not self.stop.wait(.05):
            try:
                self.tick()
            except Exception:
                # Keep unrelated rooms running if a particular rule worker fails.
                continue

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.store.close()
