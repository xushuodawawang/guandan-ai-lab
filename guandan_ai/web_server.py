"""HTTP commands and private WebSocket snapshots for shared Guandan rooms."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

from websockets.sync.server import serve as websocket_serve

from .card_art import card_svg
from .multiplayer import RoomService, StaleAction
from .rules import JavaRules

WEB_ROOT = Path(__file__).parent / "web"


def cookie_token(headers):
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie", ""))
        return cookie["gd_identity"].value if "gd_identity" in cookie else None
    except Exception:
        return None


def same_host(origin, host):
    if not origin:
        return True
    parsed = urlsplit(origin)
    target = urlsplit(parsed.scheme + "://" + host)
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme in ("http", "https") and parsed.hostname == target.hostname and (parsed.port or default_port) == (target.port or default_port)


def make_server(port=8502, rules=None, *, host="127.0.0.1", ws_port=8503, store_path=None, **service_options):
    rules = rules or JavaRules()
    service = RoomService(rules, store_path=store_path, **service_options)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body, content_type="application/json; charset=utf-8", token=None):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400" if content_type.startswith("image/") else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if token:
                secure = "; Secure" if os.getenv("GUANDAN_COOKIE_SECURE") == "1" else ""
                self.send_header("Set-Cookie", f"gd_identity={token}; HttpOnly; SameSite=Lax; Path=/{secure}")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def user(self):
            return service.identity(cookie_token(self.headers))

        def do_GET(self):
            path = urlsplit(self.path).path
            token = None
            try:
                if path == "/health":
                    return self.respond(200, {"app": "guandan-browser", "version": 2, "ws_port": self.server.ws_port})
                if path in ("/api/state", "/api/log", "/api/providers"):
                    token, user = self.user()
                    if path == "/api/log":
                        return self.respond(200, service.export(user), "application/jsonl; charset=utf-8", token)
                    if path == "/api/providers":
                        from .llm import public_profiles
                        return self.respond(200, {"providers": public_profiles()}, token=token)
                    ws_url = os.getenv("GUANDAN_PUBLIC_WS_URL")
                    if not ws_url:
                        hostname = urlsplit("http://" + self.headers.get("Host", "127.0.0.1")).hostname
                        if ":" in hostname:
                            hostname = "[" + hostname + "]"
                        ws_url = f"ws://{hostname}:{self.server.ws_port}/"
                    return self.respond(200, {"state": service.snapshot(user), "ws_url": ws_url}, token=token)
                if path.startswith("/cards/") and path.endswith(".svg"):
                    return self.respond(200, card_svg(path[7:-4]), "image/svg+xml; charset=utf-8")
                files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
                if path in files:
                    file, mime = files[path]
                    return self.respond(200, (WEB_ROOT / file).read_bytes(), mime + "; charset=utf-8")
                return self.respond(404, {"error": "页面不存在"})
            except (ValueError, RuntimeError, OSError) as exc:
                self.respond(400, {"error": str(exc)}, token=token)

        def do_POST(self):
            if not same_host(self.headers.get("Origin"), self.headers.get("Host", "")):
                return self.respond(403, {"error": "请从游戏页面操作"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.respond(415, {"error": "请求应为 JSON"})
            token, user = None, None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768:
                    raise ValueError("请求大小无效")
                payload = json.loads(self.rfile.read(length))
                path = urlsplit(self.path).path
                if not path.startswith("/api/"):
                    return self.respond(404, {"error": "操作不存在"})
                token, user = self.user()
                self.respond(200, service.dispatch(user, path[5:], payload), token=token)
            except (ValueError, RuntimeError, OSError) as exc:
                body = {"error": str(exc)}
                if user:
                    body["state"] = service.snapshot(user)
                self.respond(409 if isinstance(exc, StaleAction) else 400, body, token=token)

    def ws_handler(connection):
        request = connection.request
        origin = request.headers.get("Origin")
        allowed = True
        if origin:
            parsed = urlsplit(origin)
            target = urlsplit("http://" + request.headers.get("Host", ""))
            default = 443 if parsed.scheme == "https" else 80
            allowed = parsed.scheme in ("http", "https") and parsed.hostname == target.hostname and (parsed.port or default) == server.server_port
            public = os.getenv("GUANDAN_PUBLIC_WS_URL")
            if public:
                endpoint = urlsplit(public)
                allowed = allowed or (parsed.hostname == endpoint.hostname and parsed.scheme == ("https" if endpoint.scheme == "wss" else "http") and (parsed.port or default) == (endpoint.port or (443 if endpoint.scheme == "wss" else 80)))
        if not allowed:
            connection.close(1008, "Origin rejected")
            return
        token = cookie_token(request.headers)
        uid = hashlib.sha256((token or "").encode()).hexdigest()
        with service.lock:
            user = service.users.get(uid)
            if user is None:
                connection.close(1008, "Load the game page first")
                return
            user.sockets += 1
        previous = None
        try:
            while not service.stop.is_set():
                user.seen = service.clock()
                state = service.snapshot(user)
                signature = (state["revision"], state.get("thinking", False))
                if signature != previous:
                    connection.send(json.dumps({"type": "state", "state": state}, ensure_ascii=False))
                    previous = signature
                try:
                    raw = connection.recv(timeout=.3)
                except TimeoutError:
                    continue
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("WebSocket 请求应为 JSON 对象")
                    if message.get("type") == "ping":
                        connection.send('{"type":"pong"}')
                    elif message.get("type") == "command":
                        reply = service.dispatch(user, message.get("command"), message.get("payload"))
                        connection.send(json.dumps({"type": "ack", **reply}, ensure_ascii=False))
                except (ValueError, TypeError, RuntimeError) as exc:
                    connection.send(json.dumps({"type": "error", "error": str(exc)}, ensure_ascii=False))
        except Exception:
            pass
        finally:
            with service.lock:
                user.sockets = max(0, user.sockets-1)
                user.seen = service.clock()

    class GameServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            self.ws_server.shutdown()
            service.close()
            super().server_close()

    try:
        server = GameServer((host, port), Handler)
        server.ws_server = websocket_serve(ws_handler, host, 0 if port == 0 and ws_port == 8503 else ws_port,
            max_size=32768, ping_interval=10, ping_timeout=10)
        server.ws_port = server.ws_server.socket.getsockname()[1]
        server.service = service
        threading.Thread(target=server.ws_server.serve_forever, name="room-websocket", daemon=True).start()
        return server
    except Exception:
        service.close()
        raise


def start_background_server(port=8502):
    from .platform import ROOT
    server = make_server(port, store_path=ROOT / "data/server/rooms.sqlite3")
    threading.Thread(target=server.serve_forever, name="guandan-http", daemon=True).start()
    return server


def serve(port=8502, host="127.0.0.1", ws_port=8503, store_path=None, human_timeout=90):
    from .platform import ROOT
    with JavaRules() as rules:
        server = make_server(port, rules, host=host, ws_port=ws_port,
            store_path=store_path or ROOT / "data/server/rooms.sqlite3", human_timeout=human_timeout)
        print(f"网页：http://{'127.0.0.1' if host=='0.0.0.0' else host}:{server.server_port} · WebSocket {server.ws_port}", flush=True)
        if host == "0.0.0.0":
            print("已监听局域网；其他设备使用本机局域网 IP 打开网页，分享房间码加入。", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
