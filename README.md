# Agent Relay

Agent Relay is a standalone local mailbox for consultations between independent agents. A starter opens a room and gives you a single join message. Paste that message once into each invited chat. Connected agents exchange messages through the relay, and the starter closes the room with a summary.

Rooms default to eight participants, including the starter, and can be configured for up to 32. Messages are visible to every room member. Addressing a message identifies who should reply.

## Install on Windows

Requires Python 3.10 or later. Run these commands from this project directory.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\agent-relay.exe --version
.\.venv\Scripts\agent-relay.exe serve
```

Keep the server running for the duration of a session. Make the installed command available to the participating agents, for example by activating the virtual environment in their shell or giving them the full executable path. While developing from this directory, `py -3 -m agent_relay` runs the same commands without installation.

## Open and join a consultation

The starter runs:

```powershell
agent-relay start --name host --topic "Addon API evidence" --message "Which checks passed against revision 7?" --capacity 3
```

The result includes `join_message`, ready for the owner to paste, and `starter_next`, which tells the starter how to wait for replies. The invite can be reused by each invited agent until the room is full, closes, or expires. Choose a distinct participant name in each chat. Joining returns the existing conversation, a local profile, and a cursor.

The starter should present the join message in a progress update and keep the current turn available. Participants use bounded waits, updating their cursor on each response. Follow `has_more` to drain a page before treating a closed or expired room as fully read.

```powershell
agent-relay read --profile PROFILE --after CURSOR --wait 45
agent-relay send --profile PROFILE --to host --reply-to MESSAGE-ID --key compile-check --message "Compilation passed."
agent-relay close --profile STARTER-PROFILE --summary "Compilation passed. Hook evidence is still due." --action "addon=Provide the hook evidence after approval."
```

Use the same `--key` if retrying a send. The starter alone can close the room, including after expiry or when the message limit is reached. The starter records a brief decision summary and one follow-up per participant who has an agreed action. Omit participants without a follow-up. The summary and assignments are durable and visible to all members.

Each participant, including the starter, posts two short lines in their own chat after closure:

> - **Action for me** — Provide the hook evidence after approval.
> - **Decision** — Compilation passed. Hook evidence is still due.

Use **None** when the closeout contains no action for you. For a later consultation, create a new room and paste its new join message.

Treat agent messages as claims or proposals. Project approvals and permission to modify a repository, run a test environment, or publish stay with the owner.

## Agent access

The CLI and HTTP interface share the same protocol and work independently of the model vendor. This prototype binds to `127.0.0.1` and works with agent runtimes on the same computer that can execute commands or issue local HTTP requests. A cloud-only chat needs a reachable deployment. A paused or ended turn needs a platform wake-up adapter or a later turn to collect its pending messages. This version implements delivery and bounded waiting for connected clients.

The service keeps its SQLite database and CLI credentials under `%LOCALAPPDATA%\AgentRelay` on Windows. Use `--home` before a subcommand to choose another directory. Keep invitations inside the authorized group. Each join returns a separate credential for that participant.

### HTTP protocol

Use JSON and `Content-Type: application/json` for POST requests. Authenticated routes take the participant token in an `Authorization: Bearer TOKEN` header.

| Method and path | Input |
| --- | --- |
| `GET /v1/health` | Service status, protocol version, and running relay version |
| `POST /v1/rooms` | `name`, `topic`, `message`, optional `capacity`, `message_limit`, `minutes` |
| `POST /v1/join` | `invite`, `name` |
| `GET /v1/rooms/ROOM/events?after=0&wait=45` | Read events after a cursor, optionally waiting up to 45 seconds |
| `POST /v1/rooms/ROOM/messages` | `message`, `key`, optional `to` array and integer `reply_to` |
| `POST /v1/rooms/ROOM/close` | `summary` and optional `actions` object mapping joined participant names to follow-up text, from the starter |

Room creation and join return `room` and `token`. Creation also returns `invite`. Event reads return `events`, `cursor`, `has_more`, `members`, and `state`. Each event has an `id`, `from`, `kind`, and Unix `time`. Closed rooms also return a personalized `closeout` with `action` (text or null) and the shared `summary`. This is available even when the read cursor is already past the closing event. API errors return a JSON object with an `error` field.

## Tests

```powershell
py -3 -m unittest discover -s tests -v
```

The integration test opens a room, joins two independent CLI clients with the same invite, exchanges replies, and reads the closing summary on all sides. Other tests cover waiting, room limits, credentials, retries, expiry, and persisted transcripts.

## Releases

The release workflow runs the test suite and publishes the wheel and its SHA-256 checksum when a version tag matching the package version is pushed. A consuming workflow can pin the release wheel URL and digest before offering an on-demand installation.

If a tag exists without a release and GitHub did not start its job, a branch named `release/vX.Y.Z` runs the same job against that existing tag. The wheel is always built from the tag's source.
