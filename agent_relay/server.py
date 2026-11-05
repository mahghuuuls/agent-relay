"""Loopback HTTP service for agents that use a CLI or their own HTTP client."""

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Condition
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .store import RelayError


ROOM_ROUTE = re.compile(r"/v1/rooms/([0-9a-f]{16})/(events|messages|close)")


class RelayServer(ThreadingHTTPServer):
    def __init__(self, port, store):
        super().__init__(("127.0.0.1", port), Handler)
        self.store = store
        self.changed = Condition()
        self.generation = 0

    def notify_change(self):
        with self.changed:
            self.generation += 1
            self.changed.notify_all()

    def events(self, room, token, after, wait):
        deadline = time.monotonic() + wait
        while True:
            with self.changed:
                generation = self.generation
            result = self.store.read(room, token, after)
            remaining = deadline - time.monotonic()
            if result["events"] or result["state"] != "open" or remaining <= 0:
                return result
            with self.changed:
                self.changed.wait_for(lambda: self.generation != generation, min(remaining, 1))


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(55)

    def log_message(self, format, *args):
        # Invite and participant capabilities must stay out of HTTP logs.
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        try:
            port = self.server.server_port
            if self.headers.get("Host") not in {f"localhost:{port}", f"127.0.0.1:{port}"}:
                raise RelayError(403, "Only the local relay address is accepted.")
            if self.headers.get("Origin") is not None:
                raise RelayError(403, "Use the CLI or a local HTTP client.")
            result = self.route()
        except RelayError as error:
            self.respond(error.status, {"error": str(error)})
        except ValueError:
            self.respond(400, {"error": "Malformed relay request."})
        except (TimeoutError, ConnectionError):
            return
        else:
            self.respond(200, result)

    def route(self):
        url = urlsplit(self.path)
        store = self.server.store
        if self.command == "GET" and url.path == "/v1/health":
            return {"status": "ready", "protocol": 1, "version": __version__}
        if self.command == "POST" and url.path in {"/v1/rooms", "/v1/join"}:
            body = self.body()
            result = store.create(body) if url.path == "/v1/rooms" else store.join(body)
            self.server.notify_change()
            return result

        match = ROOM_ROUTE.fullmatch(url.path)
        if not match:
            raise RelayError(404, "Unknown relay endpoint.")
        room, action = match.groups()
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 100:
            raise RelayError(401, "A participant token is required.")
        if self.command == "GET" and action == "events":
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=8)
            after = self.query_number(query, "after", 2**63 - 1)
            wait = self.query_number(query, "wait", 45)
            return self.server.events(room, token, after, wait)
        if self.command == "POST" and action in {"messages", "close"}:
            body = self.body()
            result = store.send(room, token, body) if action == "messages" else store.close(room, token, body)
            self.server.notify_change()
            return result
        raise RelayError(405, "Method not allowed for this endpoint.")

    @staticmethod
    def query_number(query, key, maximum):
        values = query.get(key, ["0"])
        if len(values) != 1 or not re.fullmatch(r"[0-9]{1,19}", values[0]) or int(values[0]) > maximum:
            raise RelayError(400, f"{key} must be an integer from 0 to {maximum}.")
        return int(values[0])

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise RelayError(415, "Use Content-Type: application/json.")
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,5}", length) or not 0 < int(length) <= 65536:
            raise RelayError(400, "JSON body must be 1–65536 bytes.")
        try:
            data = json.loads(self.rfile.read(int(length)).decode("utf-8"))
        except (ValueError, RecursionError) as error:
            raise RelayError(400, "Invalid UTF-8 JSON body.") from error
        if not isinstance(data, dict):
            raise RelayError(400, "JSON body must be an object.")
        return data

    def respond(self, status, data):
        body = json.dumps(data).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (TimeoutError, ConnectionError):
            pass
