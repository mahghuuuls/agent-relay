import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent_relay.cli import call
from agent_relay.server import RelayServer
from agent_relay.store import RelayError, Store


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.database = self.home / "relay.sqlite3"
        self.server = RelayServer(0, Store(self.database))
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=2)

    def cli(self, *args):
        run = subprocess.run([sys.executable, "-m", "agent_relay", "--home", str(self.home), *args],
                             capture_output=True, text=True, check=True, timeout=12)
        return json.loads(run.stdout)

    def start(self, **settings):
        return call(self.url, "/v1/rooms",
                    body={"name": "starter", "topic": "Evidence", "message": "First question", **settings})

    def join(self, room, name):
        return call(self.url, "/v1/join", body={"name": name, "invite": room["invite"]})

    def room_call(self, room, action, *, body=None, actor=None):
        return call(self.url, f"/v1/rooms/{room['room']}/{action}", body=body, token=(actor or room)["token"])

    def check_error(self, status, action):
        with self.assertRaises(RelayError) as caught:
            action()
        self.assertEqual(status, caught.exception.status)

    def test_three_independent_cli_clients_use_one_invitation_and_exchange_replies(self):
        starter = self.cli("start", "--url", self.url, "--name", "host", "--topic", "X-3",
                           "--message", "Can the addon compile against revision 7?", "--capacity", "3")
        self.assertNotIn("token", starter)
        invite = re.search(r"agent-relay join (\S+)", starter["join_message"])[1]
        first = starter["events"][-1]
        addon = self.cli("join", invite, "--url", self.url, "--name", "addon")
        reviewer = self.cli("join", invite, "--url", self.url, "--name", "reviewer")
        self.assertEqual(first, addon["events"][1])
        self.assertEqual(3, len(reviewer["members"]))
        self.assertEqual(starter["room"], reviewer["room"])

        with ThreadPoolExecutor() as pool:
            waiting = pool.submit(self.cli, "read", "--profile", starter["profile"], "--after",
                                  str(reviewer["cursor"]), "--wait", "8")
            reply = self.cli("send", "--profile", addon["profile"], "--message", "Compilation passed.",
                             "--reply-to", str(first["id"]), "--to", "host", "--key", "compile")
            result = waiting.result(timeout=10)
        self.assertEqual(reply["id"], result["events"][-1]["id"])
        self.assertEqual(["host"], result["events"][-1]["to"])
        observed = self.cli("read", "--profile", reviewer["profile"], "--after", str(reviewer["cursor"]))
        self.assertEqual("Compilation passed.", observed["events"][0]["text"])
        self.cli("send", "--profile", reviewer["profile"], "--message", "Evidence reviewed.",
                 "--reply-to", str(reply["id"]), "--key", "review")
        latest = self.cli("read", "--profile", starter["profile"], "--after", str(result["cursor"]))
        self.assertEqual("reviewer", latest["events"][0]["from"])
        closed = self.cli("close", "--profile", starter["profile"], "--summary", "Compile evidence verified.",
                          "--action", "addon=Attach the compile log.", "--action", "host=Record the evidence link.")
        self.assertEqual("Record the evidence link.", closed["closeout"]["action"])
        for member in (addon, reviewer):
            report = self.cli("read", "--profile", member["profile"], "--after", str(closed["id"]))
            self.assertEqual("closed", report["state"])
            self.assertEqual([], report["events"])
            self.assertEqual("Compile evidence verified.", report["closeout"]["summary"])
            action = "Attach the compile log." if member is addon else None
            self.assertEqual(action, report["closeout"]["action"])

    def test_wait_returns_new_events_without_polling_from_the_client(self):
        room = self.start()
        addon = self.join(room, "addon")
        cursor = self.room_call(room, "events")["cursor"]
        entered = threading.Event()
        original = self.server.store.read

        def observe(*args):
            result = original(*args)
            entered.set()
            return result

        with patch.object(self.server.store, "read", side_effect=observe), ThreadPoolExecutor() as pool:
            waiting = pool.submit(self.room_call, room, f"events?after={cursor}&wait=8")
            self.assertTrue(entered.wait(timeout=2))
            self.assertFalse(waiting.done())
            sent = self.room_call(room, "messages", actor=addon, body={"key": "a", "message": "Reply"})
            self.assertEqual(sent["id"], waiting.result(timeout=2)["cursor"])

    def test_room_capacity_auth_and_closure_are_enforced(self):
        room = self.start(capacity=3, message_limit=2)
        addon = self.join(room, "addon")
        self.check_error(409, lambda: self.join(room, "ADDON"))
        self.join(room, "reviewer")
        self.check_error(409, lambda: self.join(room, "fourth"))
        unrelated = self.start()
        for name in ("first", "second", "third"):
            self.join(unrelated, name)
        self.assertEqual(4, len(self.room_call(unrelated, "events")["members"]))
        self.check_error(401, lambda: self.room_call(room, "events", actor=unrelated))
        self.check_error(403, lambda: self.room_call(room, "close", actor=addon, body={"summary": "Done"}))
        body = {"key": "same", "message": "My evidence"}
        sent = self.room_call(room, "messages", actor=addon, body=body)
        duplicate = self.room_call(room, "messages", actor=addon, body=body)
        self.assertEqual({"id": sent["id"], "duplicate": True}, duplicate)
        self.check_error(409, lambda: self.room_call(room, "messages", actor=addon, body={**body, "message": "Changed"}))
        self.check_error(409, lambda: self.room_call(room, "messages", actor=addon, body={**body, "key": "new"}))
        self.check_error(400, lambda: self.room_call(
            room, "close", body={"summary": "Done", "actions": {"missing": "Review."}}))
        outcome = {"summary": "Done", "actions": {"addon": "Review."}}
        report = self.room_call(room, "close", body=outcome)
        replay = self.room_call(room, "close", body={"summary": "Done", "actions": {"ADDON": "Review."}})
        self.assertEqual(report["id"], replay["id"])
        self.check_error(409, lambda: self.room_call(room, "close", body={"summary": "Different"}))
        self.check_error(409, lambda: self.room_call(room, "close", body={"summary": "Done"}))

    def test_legacy_closed_room_still_returns_a_closeout(self):
        room = self.start()
        addon = self.join(room, "addon")
        store = self.server.store
        with store.connection(write=True) as db:
            db.execute("UPDATE rooms SET closed = 1 WHERE id = ?", (room["room"],))
            last_id = store.append(db, room["room"], "starter", "close", {"summary": "Legacy summary."})
        report = self.room_call(room, f"events?after={last_id}", actor=addon)
        self.assertEqual({"action": None, "summary": "Legacy summary."}, report["closeout"])
        retry = self.room_call(room, "close", body={"summary": "Legacy summary."})
        self.assertEqual(last_id, retry["id"])
        self.assertTrue(retry["duplicate"])

    def test_room_survives_server_restart_and_expiry_preserves_transcript(self):
        room = self.start()
        addon = self.join(room, "addon")
        with Store(self.database).connection(write=True) as db:
            db.execute("UPDATE rooms SET expires = ? WHERE id = ?", (int(time.time()) - 1, room["room"]))
        restarted = Store(self.database)
        events = restarted.read(room["room"], addon["token"], 0)
        self.assertEqual("expired", events["state"])
        self.assertEqual("First question", events["events"][1]["text"])
        self.check_error(409, lambda: self.join(room, "late"))
        self.check_error(409, lambda: self.room_call(room, "messages", body={"key": "x", "message": "Late"}))
        self.room_call(room, "close", body={"summary": "Expired before agreement."})
        self.assertEqual("closed", restarted.read(room["room"], addon["token"], 0)["state"])


if __name__ == "__main__":
    unittest.main()
