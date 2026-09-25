# Secrets this system needs

Nothing here is committed to this repository. This file is the manifest: what exists, where it lives,
what it is for, and how to make a new one. Values are never written down here.

| Secret | Lives at | Used by | Purpose | Mode |
|---|---|---|---|---|
| Announcement token | `state/announce-token.txt` | the bridge, the `voice_satellite` plugin | Bearer token for the announcement endpoint and the follow-up control endpoint | 600 |
| Hermes API server key | `~/.hermes/.env` as `API_SERVER_KEY` | the bridge | Authenticates the bridge's calls to Hermes on loopback 8642 | 600 |
| Anything your own plugins need | wherever that plugin reads it | that plugin | Optional: this project needs none of them | as required |

Notes:

- The announcement token is a shared secret between the bridge and the plugin that reads it. Both read
  the same file; any long random url-safe string works. If you rotate it, the bridge must be restarted.
- The bridge reads `API_SERVER_KEY` from `~/.hermes/.env` at run time, so rotating it also needs a
  bridge restart.
- The Hermes API server must stay bound to `127.0.0.1`. It is a full agent surface with tool access;
  putting it on the LAN would hand house control to anything on the network.
- Never put any of these in a chat message, a note, a commit, or a log line. The bridge itself never
  logs the key it reads.
- **Never keep a copy of a secret inside the repository tree.** A backup copy of the announcement
  token belongs outside it, where a `git add -A` cannot reach it, and the copy is a convenience only:
  it goes stale the moment the token is rotated, and the file the bridge reads stays the source of
  truth.
