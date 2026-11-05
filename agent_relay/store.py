"""Durable rooms and participant-scoped capabilities."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass


class RelayError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def name_of(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", value):
        raise RelayError(400, "Name must be 1–40 letters, numbers, underscores or hyphens.")
    return value


def required_text(value, field, limit=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RelayError(400, f"{field} must contain 1–{limit} characters.")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise RelayError(400, f"{field} must be valid UTF-8 text.") from error
    return value.strip()


def integer(value, field, low, high):
    if type(value) is not int or not low <= value <= high:
        raise RelayError(400, f"{field} must be an integer from {low} to {high}.")
    return value


def digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Member:
    room: str
    name: str
    starter: bool


class Store:
    def __init__(self, path):
        self.path = path
        with self.connection(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY, topic TEXT NOT NULL, invite_hash TEXT UNIQUE NOT NULL,
                    capacity INTEGER NOT NULL, message_limit INTEGER NOT NULL, expires INTEGER NOT NULL,
                    closed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS members (
                    room TEXT NOT NULL REFERENCES rooms(id), name TEXT NOT NULL COLLATE NOCASE,
                    token_hash TEXT UNIQUE NOT NULL, starter INTEGER NOT NULL,
                    PRIMARY KEY (room, name)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL REFERENCES rooms(id),
                    author TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL,
                    request_key TEXT, UNIQUE (room, author, request_key)
                );
                CREATE INDEX IF NOT EXISTS events_room ON events(room, id);
            """)

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, data):
        name = name_of(data.get("name"))
        topic = required_text(data.get("topic"), "Topic", 200)
        question = required_text(data.get("message"), "First message")
        capacity = integer(data.get("capacity", 8), "Capacity", 2, 32)
        limit = integer(data.get("message_limit", 30), "Message limit", 2, 200)
        minutes = integer(data.get("minutes", 60), "Lifetime in minutes", 1, 1440)
        room, invite = secrets.token_hex(8), secrets.token_urlsafe(24)
        expires = int(time.time()) + minutes * 60
        with self.connection(write=True) as db:
            db.execute("INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?, 0)",
                       (room, topic, digest(invite), capacity, limit, expires))
            token = self.add_member(db, room, name, True)
            self.append(db, room, name, "message", {"text": question, "to": [], "reply_to": None})
        return {"room": room, "token": token, "invite": invite, "name": name, "topic": topic, "expires": expires}

    def join(self, data):
        name = name_of(data.get("name"))
        invite = required_text(data.get("invite"), "Invitation", 100)
        with self.connection(write=True) as db:
            room = db.execute("SELECT * FROM rooms WHERE invite_hash = ?", (digest(invite),)).fetchone()
            if room is None:
                raise RelayError(404, "Invitation not found.")
            self.require_open(room)
            count = db.execute("SELECT COUNT(*) FROM members WHERE room = ?", (room["id"],)).fetchone()[0]
            if count >= room["capacity"]:
                raise RelayError(409, "Room is full.")
            if self.lookup_name(db, room["id"], name) is not None:
                raise RelayError(409, "That participant name is already in use.")
            token = self.add_member(db, room["id"], name, False)
            return {"room": room["id"], "token": token, "name": name, "topic": room["topic"]}

    @staticmethod
    def add_member(db, room, name, starter):
        token = secrets.token_urlsafe(32)
        db.execute("INSERT INTO members VALUES (?, ?, ?, ?)", (room, name, digest(token), starter))
        Store.append(db, room, name, "join", {})
        return token

    @staticmethod
    def append(db, room, author, kind, body, key=None):
        cursor = db.execute("INSERT INTO events(room, author, kind, body, created, request_key)"
                            " VALUES (?, ?, ?, ?, ?, ?)",
                            (room, author, kind, json.dumps(body), int(time.time()), key))
        return cursor.lastrowid

    @staticmethod
    def lookup_name(db, room, name):
        row = db.execute("SELECT name FROM members WHERE room = ? AND name = ?", (room, name)).fetchone()
        return row["name"] if row else None

    @staticmethod
    def authorized(db, room, token):
        row = db.execute("SELECT name, starter FROM members WHERE room = ? AND token_hash = ?",
                         (room, digest(token))).fetchone()
        if row is None:
            raise RelayError(401, "A valid participant token is required for this room.")
        return Member(room, row["name"], bool(row["starter"]))

    @staticmethod
    def state(room):
        if room["closed"]:
            return "closed"
        if room["expires"] <= time.time():
            return "expired"
        return "open"

    @staticmethod
    def require_open(room):
        state = Store.state(room)
        if state != "open":
            raise RelayError(409, f"Room is {state}.")

    def read(self, room, token, after):
        with self.connection() as db:
            member = self.authorized(db, room, token)
            info = db.execute("SELECT * FROM rooms WHERE id = ?", (room,)).fetchone()
            rows = db.execute("SELECT id, author, kind, body, created FROM events"
                              " WHERE room = ? AND id > ? ORDER BY id LIMIT 101", (room, after)).fetchall()
            members = db.execute("SELECT name, starter FROM members WHERE room = ? ORDER BY rowid",
                                 (room,)).fetchall()
            closeout = None
            if info["closed"]:
                final = db.execute("SELECT body FROM events WHERE room = ? AND kind = 'close'", (room,)).fetchone()
                closeout = self.closeout(json.loads(final["body"]), member)
        events = [{"id": row["id"], "from": row["author"], "kind": row["kind"],
                   "time": row["created"], **json.loads(row["body"])} for row in rows[:100]]
        return {"room": room, "topic": info["topic"], "state": self.state(info), "you": member.name,
                "members": [dict(row) for row in members], "events": events,
                "cursor": events[-1]["id"] if events else after, "has_more": len(rows) > 100,
                "closeout": closeout}

    def send(self, room, token, data):
        message = required_text(data.get("message"), "Message")
        key = required_text(data.get("key"), "Request key", 100)
        recipients = data.get("to", [])
        if not isinstance(recipients, list) or len(recipients) > 32:
            raise RelayError(400, "Recipients must be a list of participant names.")
        recipients = [name_of(name) for name in recipients]
        reply = data.get("reply_to")
        if reply is not None:
            integer(reply, "Reply ID", 1, 2**63 - 1)
        with self.connection(write=True) as db:
            member = self.authorized(db, room, token)
            info = db.execute("SELECT * FROM rooms WHERE id = ?", (room,)).fetchone()
            targets = []
            for name in recipients:
                target = self.lookup_name(db, room, name)
                if target is None or target == member.name:
                    raise RelayError(400, "Recipients must be other joined participants.")
                if target not in targets:
                    targets.append(target)
            if reply is not None and not db.execute(
                    "SELECT 1 FROM events WHERE room = ? AND id = ? AND kind = 'message'", (room, reply)).fetchone():
                raise RelayError(400, "Reply must refer to a message in this room.")
            body = {"text": message, "to": targets, "reply_to": reply}
            prior = db.execute("SELECT id, body FROM events WHERE room = ? AND author = ? AND request_key = ?",
                               (room, member.name, key)).fetchone()
            if prior is not None:
                if json.loads(prior["body"]) != body:
                    raise RelayError(409, "Request key was already used for a different message.")
                return {"id": prior["id"], "duplicate": True}
            self.require_open(info)
            count = db.execute("SELECT COUNT(*) FROM events WHERE room = ? AND kind = 'message'",
                               (room,)).fetchone()[0]
            if count >= info["message_limit"]:
                raise RelayError(409, "Message limit reached. The starter can still close the room.")
            return {"id": self.append(db, room, member.name, "message", body, key), "duplicate": False}

    def close(self, room, token, data):
        summary = required_text(data.get("summary"), "Closing summary")
        requested = data.get("actions", {})
        if not isinstance(requested, dict) or len(requested) > 32:
            raise RelayError(400, "Actions must map participant names to text.")
        with self.connection(write=True) as db:
            member = self.authorized(db, room, token)
            if not member.starter:
                raise RelayError(403, "Only the starter can close the room.")
            names = db.execute("SELECT name FROM members WHERE room = ? ORDER BY rowid", (room,)).fetchall()
            actions = dict.fromkeys(row["name"] for row in names)
            for name, action in requested.items():
                participant = self.lookup_name(db, room, name_of(name))
                if participant is None:
                    raise RelayError(400, "Actions must refer to joined participants.")
                if actions[participant] is not None:
                    raise RelayError(400, f"More than one action entry for {participant}.")
                actions[participant] = required_text(action, "Action", 500)
            closing = {"summary": summary, "actions": actions}
            closeout = self.closeout(closing, member)
            previous = db.execute("SELECT id, body FROM events WHERE room = ? AND kind = 'close'",
                                  (room,)).fetchone()
            if previous:
                saved = json.loads(previous["body"])
                saved.setdefault("actions", dict.fromkeys(actions))
                if saved != closing:
                    raise RelayError(409, "Room already closed with a different outcome.")
                return {"id": previous["id"], "state": "closed", "duplicate": True, "closeout": closeout}
            db.execute("UPDATE rooms SET closed = 1 WHERE id = ?", (room,))
            return {"id": self.append(db, room, member.name, "close", closing),
                    "state": "closed", "duplicate": False, "closeout": closeout}

    @staticmethod
    def closeout(closing, member):
        return {"action": closing.get("actions", {}).get(member.name), "summary": closing["summary"]}
