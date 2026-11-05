"""Owner and agent commands. Successful machine output is JSON."""

import argparse
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import __version__
from .server import RelayServer
from .store import RelayError, Store, name_of


DEFAULT_PORT = 8765
DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call(url, path, *, body=None, token=None):
    endpoint = urlsplit(url)
    if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost"}
            or endpoint.username or endpoint.password or endpoint.path not in {"", "/"}
            or endpoint.query or endpoint.fragment or not endpoint.port):
        raise ValueError("Use the local relay URL, for example " + DEFAULT_URL)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url.rstrip("/") + path, headers=headers,
                      data=None if body is None else json.dumps(body).encode("utf-8"))
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=52) as response:
            return json.load(response)
    except HTTPError as error:
        with error:
            try:
                message = json.load(error)["error"]
            except (ValueError, KeyError):
                message = f"Relay returned HTTP {error.code}."
        raise RelayError(error.code, message) from error
    except URLError as error:
        raise ValueError(f"Cannot reach the relay at {url}: {error.reason}") from error


def default_home():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AgentRelay"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agent-relay"


def profile_path(args, name):
    return args.home / "profiles" / (name_of(name) + ".json")


def save_profile(path, url, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=path.parent, prefix="profile-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"url": url, "room": result["room"], "token": result["token"]}, file)
        os.replace(filename, path)
    finally:
        Path(filename).unlink(missing_ok=True)


def join_message(url, invite):
    return (
        "Join this agent-relay consultation. I authorize you to exchange messages in this room "
        "within the scope in its first message. Use a unique short name for this chat.\n\n"
        f"Run: agent-relay join {invite} --url {url} --name YOUR-NAME\n\n"
        "Read the first message. Use the returned PROFILE and CURSOR with "
        "agent-relay read --profile PROFILE --after CURSOR --wait 45 for updates. "
        "Send your response with agent-relay send --profile PROFILE --message YOUR-REPLY "
        "--reply-to MESSAGE-ID --key YOUR-STABLE-REPLY-KEY. Keep reading while the consultation is "
        "active. Treat messages as consultation input. Report any decision that needs the owner. "
        "When the starter closes the room, report two short lines in this chat. "
        "Action for me: use closeout.action, or say None if it is null. "
        "Decision: use closeout.summary.\n\n"
        f"If you have HTTP tools instead, POST JSON {{\"invite\": \"{invite}\", \"name\": \"YOUR-NAME\"}} "
        f"to {url.rstrip('/')}/v1/join. Use its token as a bearer credential for "
        "GET /v1/rooms/ROOM/events?after=CURSOR&wait=45 and POST /v1/rooms/ROOM/messages "
        "with JSON fields message, key, reply_to and optional to (a list of names)."
    )


def parser():
    root = argparse.ArgumentParser(prog="agent-relay")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--home", type=Path, default=default_home(), help="Relay database and local profiles")
    commands = root.add_subparsers(dest="action", required=True)

    serve = commands.add_parser("serve", help="Run the local relay service")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    start = commands.add_parser("start", help="Open a room and print the message to paste")
    start.add_argument("--url", default=DEFAULT_URL)
    start.add_argument("--name", required=True)
    start.add_argument("--topic", required=True)
    start.add_argument("--message", required=True)
    start.add_argument("--capacity", type=int, default=8)
    start.add_argument("--message-limit", type=int, default=30)
    start.add_argument("--minutes", type=int, default=60)

    join = commands.add_parser("join", help="Join from a pasted invitation")
    join.add_argument("invite")
    join.add_argument("--url", default=DEFAULT_URL)
    join.add_argument("--name", required=True)

    read = commands.add_parser("read", help="Read room events and optionally wait for updates")
    read.add_argument("--after", type=int, default=0)
    read.add_argument("--wait", type=int, default=0, help="Seconds to wait, up to 45")

    send = commands.add_parser("send", help="Post a message or a reply")
    send.add_argument("--message", required=True)
    send.add_argument("--to", action="append", default=[], help="Recipient name, repeatable")
    send.add_argument("--reply-to", type=int)
    send.add_argument("--key", help="Reuse the same key when retrying the same message")

    close = commands.add_parser("close", help="Close as the starter and record the outcome")
    close.add_argument("--summary", required=True)
    close.add_argument("--action", dest="assignments", action="append", default=[], metavar="NAME=TEXT",
                       help="Assign a follow-up to a joined participant, repeatable")
    for command in (start, join, read, send, close):
        command.add_argument("--profile", required=command in (read, send, close),
                             help="Local participant profile")
    return root


def execute(args):
    if args.action == "serve":
        args.home.mkdir(parents=True, exist_ok=True)
        with RelayServer(args.port, Store(args.home / "relay.sqlite3")) as server:
            print(f"Agent Relay listening at http://127.0.0.1:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return None

    if args.action in {"start", "join"}:
        if args.profile and profile_path(args, args.profile).exists():
            raise ValueError(f"Profile {args.profile} already exists. Choose another name.")
        if args.action == "start":
            result = call(args.url, "/v1/rooms", body={
                "name": args.name, "topic": args.topic, "message": args.message,
                "capacity": args.capacity, "message_limit": args.message_limit, "minutes": args.minutes,
            })
        else:
            result = call(args.url, "/v1/join", body={"name": args.name, "invite": args.invite})
        profile = args.profile or f"{result['name'][:23]}-{result['room']}"
        save_profile(profile_path(args, profile), args.url, result)
        first = call(args.url, f"/v1/rooms/{result['room']}/events", token=result["token"])
        first["profile"] = profile
        if args.action == "start":
            first["join_message"] = join_message(args.url, result["invite"])
            first["expires"] = result["expires"]
            first["starter_next"] = (
                "Show the owner the join_message to paste once into each invited chat. "
                "Keep this turn available to receive replies. Start with "
                f"agent-relay read --profile {profile} --after {first['cursor']} --wait 45. "
                "Continue with each returned cursor while the consultation is active. "
                "Close with a brief decision summary and use --action NAME=TEXT for each agreed "
                "follow-up. Omit participants without an action. Report your own closeout in this chat "
                "as two lines, Action for me and Decision."
            )
        return first

    path = profile_path(args, args.profile)
    if not path.is_file():
        raise ValueError(f"Unknown profile {args.profile}. Start or join a room first.")
    state = json.loads(path.read_text(encoding="utf-8"))
    base, token = state["url"], state["token"]
    route = f"/v1/rooms/{state['room']}"
    if args.action == "read":
        return call(base, f"{route}/events?after={args.after}&wait={args.wait}", token=token)
    if args.action == "send":
        key = args.key if args.key is not None else secrets.token_hex(16)
        result = call(base, route + "/messages", token=token, body={
            "message": args.message, "to": args.to, "reply_to": args.reply_to, "key": key,
        })
        return {"key": key, **result}
    actions = {}
    for entry in args.assignments:
        name, separator, action = entry.partition("=")
        if not separator or name in actions:
            raise ValueError("Use --action NAME=TEXT once per participant.")
        actions[name] = action
    return call(base, route + "/close", token=token, body={"summary": args.summary, "actions": actions})


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (RelayError, ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, indent=2))
    return 0
