import http.cookiejar
import json
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
import xml.etree.ElementTree as ET

import pytest
from websockets.sync.client import connect

from guandan_ai.card_art import card_svg
from guandan_ai.cards import CARD_TYPES
from guandan_ai.platform import RUNTIME
from guandan_ai.rules import JavaRules
from guandan_ai.web_server import make_server


def test_all_original_card_art_is_valid_svg():
    for card in CARD_TYPES:
        root = ET.fromstring(card_svg(card))
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert not any(e.tag.endswith("image") for e in root.iter())


@pytest.fixture
def server():
    if not (RUNTIME / "guandan-java/guandan-java-action.jar").is_file():
        pytest.skip("官方运行时未下载")
    with JavaRules() as rules:
        server = make_server(0, rules, ai_delay=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield server
        server.shutdown()
        server.server_close()


class Client:
    def __init__(self, server):
        self.server = server
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.state = self.get("/api/state")[1]["state"]
    def get(self, path):
        with self.opener.open(self.base + path, timeout=15) as response:
            body = response.read()
            return response.status, json.loads(body) if response.headers["Content-Type"].startswith("application/json") else body
    def post(self, command, data=None, origin=None):
        data = {"command_id": uuid.uuid4().hex, "revision": self.state["revision"],
                "room_id": self.state["room"]["code"] if self.state.get("room") else None, **(data or {})}
        request = Request(self.base + "/api/" + command, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json", "Origin": origin or self.base})
        try:
            with self.opener.open(request, timeout=15) as response:
                status, result = response.status, json.load(response)
        except HTTPError as exc:
            status, result = exc.code, json.loads(exc.read())
        if "state" in result:
            self.state = result["state"]
        return status, result
    def refresh(self):
        self.state = self.get("/api/state")[1]["state"]
    def websocket(self):
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.jar)
        return connect(f"ws://127.0.0.1:{self.server.ws_port}/", additional_headers={"Cookie": cookie},
                       origin=self.base, proxy=None, open_timeout=5)


@pytest.mark.integration
def test_http_empty_play_and_pass_are_errors_not_crashes(server):
    client = Client(server)
    assert client.post("new", {"strategy": "greedy"})[0] == 200
    assert client.post("play", {"selected": []})[0] == 400
    assert client.state["steps"] == 0
    status, body = client.post("pass")
    assert status == 400 and "领出" in body["error"]
    assert client.get("/api/state")[0] == 200
    assert client.post("new", {"strategy": "greedy"}, "http://outside.example")[0] == 403
    assert client.get("/cards/S3.svg")[0] == 200
    assert client.get("/")[0] == 200


@pytest.mark.integration
def test_real_websocket_pushes_only_each_viewers_cards_and_reconnects(server):
    host, guest = Client(server), Client(server)
    assert host.post("create", {"nickname": "甲"})[0] == 200
    code = host.state["room"]["code"]
    assert guest.post("join", {"code": code, "seat": 2, "nickname": "乙"})[0] == 200
    assert guest.post("ready", {"ready": True})[0] == 200
    host.refresh()
    assert host.post("start")[0] == 200
    def active(ws):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            msg = json.loads(ws.recv(timeout=5))
            if msg.get("state", {}).get("active"):
                return msg["state"]
        pytest.fail("WebSocket 没有推送牌局")
    with host.websocket() as a, guest.websocket() as b:
        sa, sb = active(a), active(b)
        assert sa["room"]["your_seat"] == 0 and sb["room"]["your_seat"] == 2
        assert sa["hand"] != sb["hand"]
        assert sa["actions"] and not sb["actions"]
        assert "seed" not in sa and "seed" not in sb
        assert sb["players"][2]["actual_seat"] == 0
    with guest.websocket() as b:
        assert active(b)["room"]["your_seat"] == 2


@pytest.mark.integration
def test_missing_model_configuration_does_not_start_paid_calls(server, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = Client(server)
    client.post("create")
    assert client.post("configure", {"seat": 1, "kind": "llm:deepseek"})[0] == 200
    status, body = client.post("start")
    assert status == 400 and "尚未配置" in body["error"]
    assert not client.state["active"]
