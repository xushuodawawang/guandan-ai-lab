from collections import Counter
from concurrent.futures import Future
import json
import time
import uuid

import pytest

from guandan_ai.cards import deck
from guandan_ai.models import Decision
from guandan_ai.multiplayer import RoomService, StaleAction
from guandan_ai.platform import RUNTIME
from guandan_ai.rules import JavaRules

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self):
        self.now = 1000.
    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def rules():
    if not (RUNTIME / "guandan-java/guandan-java-action.jar").is_file():
        pytest.skip("官方运行时未下载")
    with JavaRules() as rules:
        yield rules


@pytest.fixture
def service(rules):
    clock = Clock()
    server = RoomService(rules, auto_start=False, clock=clock, ai_delay=.01, human_timeout=5)
    server.test_clock = clock
    yield server
    server.close()


def cmd(service, user, name, command_id=None, **data):
    state = service.snapshot(user)
    return service.dispatch(user, name, {"command_id": command_id or uuid.uuid4().hex,
        "room_id": state.get("room", {}).get("code") if state.get("room") else None,
        "revision": state["revision"], **data})


def create(service):
    token, host = service.identity()
    state = cmd(service, host, "create", nickname="房主")["state"]
    return token, host, service.rooms[state["room"]["code"]]


def four_players(service):
    token, host, room = create(service)
    people = [host]
    for seat in (1, 2, 3):
        _, user = service.identity()
        cmd(service, user, "join", code=room.id, seat=seat, nickname=f"玩家{seat}")
        cmd(service, user, "ready", ready=True)
        people.append(user)
    return token, people, room


def settle(service, room):
    service.tick()
    if room.pending:
        room.pending["future"].result(timeout=15)
        service.tick()


def test_four_distinct_private_views_and_rotated_teammates(service):
    _, people, room = four_players(service)
    cmd(service, people[0], "start")
    hands = []
    for actual, user in enumerate(people):
        state = service.snapshot(user)
        assert state["room"]["your_seat"] == actual
        assert state["players"][2]["actual_seat"] == (actual+2)%4
        assert state["players"][0]["name"] == user.nickname
        assert "seed" not in state and state["revealed_hands"] is None
        assert all("participant" not in seat for seat in state["room"]["seats"])
        assert all("hand" not in seat for seat in state["players"])
        assert bool(state["actions"]) == (actual == 0)
        hands.extend(c["code"] for c in state["hand"])
    assert Counter(hands) == Counter(deck())


def test_room_identity_and_host_permissions(service):
    _, host, room = create(service)
    _, guest = service.identity()
    with pytest.raises(ValueError, match="座位"):
        cmd(service, guest, "join", code=room.id, seat=0)
    cmd(service, guest, "join", code=room.id, seat=2)
    with pytest.raises(ValueError, match="房主"):
        cmd(service, guest, "start")
    with pytest.raises(ValueError, match="未准备"):
        cmd(service, host, "start")
    cmd(service, guest, "ready", ready=True)
    cmd(service, host, "start")
    assert room.game and room.seats[1].kind == "team" and room.seats[3].kind == "team"
    with pytest.raises(ValueError, match="回合"):
        cmd(service, guest, "pass")


def test_empty_move_and_exactly_once_retry(service):
    _, host, room = create(service)
    cmd(service, host, "start")
    before = room.game.hand[:]
    with pytest.raises(ValueError, match="请先选择"):
        cmd(service, host, "play", selected=[])
    assert room.game.steps == 0 and room.game.hand == before
    obs = room.game.observation()
    index = next(i for i, action in enumerate(obs.actions) if action[0] == "Single")
    physical = obs.hand.index(obs.actions[index][2][0])
    payload = {"command_id": uuid.uuid4().hex, "room_id": room.id, "revision": service.revision(room), "selected": [physical], "action_index": index}
    service.dispatch(host, "play", payload)
    retry = service.dispatch(host, "play", payload)
    assert retry["duplicate"] and room.game.steps == 1
    with pytest.raises(StaleAction):
        service.dispatch(host, "play", {**payload, "command_id": uuid.uuid4().hex})


def test_ai_advances_without_browser_requests(service):
    _, host, room = create(service)
    cmd(service, host, "start")
    obs = room.game.observation()
    index = next(i for i, a in enumerate(obs.actions) if a[0] == "Single")
    cmd(service, host, "play", selected=[obs.hand.index(obs.actions[index][2][0])], action_index=index)
    service.test_clock.now += .1
    settle(service, room)
    assert room.game.steps == 2
    assert service.snapshot(host)["actions"] == []


def test_timeout_autopilot_and_resume(service):
    _, host, room = create(service)
    cmd(service, host, "start")
    service.test_clock.now += 6
    settle(service, room)
    assert room.seats[0].autopilot and room.game.steps == 1
    cmd(service, host, "autopilot", enabled=False)
    assert not room.seats[0].autopilot


def test_new_match_restores_human_manual_control(service):
    _, host, room = create(service)
    cmd(service, host, "start")
    cmd(service, host, "autopilot", enabled=True)
    cmd(service, host, "reset")
    cmd(service, host, "start")
    assert not room.seats[0].autopilot


def test_reconnect_cookie_recovers_same_seat(service):
    token, people, room = four_players(service)
    cmd(service, people[0], "start")
    _, reconnected = service.identity(token)
    assert reconnected is people[0]
    assert service.snapshot(reconnected)["room"]["your_seat"] == 0
    assert len(service.snapshot(reconnected)["hand"]) == 27


def test_late_ai_result_is_discarded_after_takeover(service):
    _, host, room = create(service)
    cmd(service, host, "start")
    room.seats[0].autopilot = True
    future = Future()
    future.set_running_or_notify_cancel()
    room.pending = {"future": future, "turn_key": service._turn_key(room), "obs": room.game.observation(), "deadline": 5000}
    cmd(service, host, "autopilot", enabled=False)
    service.tick()
    future.set_result(Decision(999999, "迟到且无效"))
    service.tick()
    assert room.game.steps == 0 and room.pending is None


def test_sqlite_restart_replays_same_hand_and_authentication(rules, tmp_path):
    path = tmp_path / "rooms.sqlite3"
    first = RoomService(rules, store_path=path, auto_start=False)
    token, host, room = create(first)
    cmd(first, host, "start")
    obs = room.game.observation()
    index = next(i for i, a in enumerate(obs.actions) if a[0] == "Single")
    cmd(first, host, "play", selected=[obs.hand.index(obs.actions[index][2][0])], action_index=index)
    saved_hands = [p.hand2json() for p in room.game.env.players]
    first.close()
    second = RoomService(rules, store_path=path, auto_start=False)
    try:
        _, user = second.identity(token)
        recovered = second.rooms[user.room_id]
        assert user.seat == 0 and recovered.game.steps == 1
        assert [p.hand2json() for p in recovered.game.env.players] == saved_hands
        assert token.encode() not in path.read_bytes()
    finally:
        second.close()


def test_other_players_autopilot_does_not_reset_active_players_clock(service):
    _, people, room = four_players(service)
    cmd(service, people[0], "start")
    deadline = room.due
    service.test_clock.now += 1
    cmd(service, people[1], "autopilot", enabled=True)
    assert room.due == deadline


def test_single_player_pause_freezes_and_restores_clock(service):
    _, host, room = create(service)
    cmd(service, host, "start")
    service.test_clock.now += 1
    cmd(service, host, "pause", paused=True)
    remaining = service.snapshot(host)["turn_seconds"]
    service.test_clock.now += 100
    service.tick()
    assert room.game.steps == 0 and service.snapshot(host)["turn_seconds"] == remaining
    cmd(service, host, "pause", paused=False)
    assert service.snapshot(host)["turn_seconds"] == pytest.approx(remaining)
