# Session-reset sidebar bug: update-gating case study

## Why this reference exists

Use this case when a Hermes messaging session is searchable but absent from a Desktop platform section, especially after repeated `/new` commands. It also illustrates how to decide between an official update, a targeted upstream hotfix, and an unsafe jump to unreleased `main` when the production checkout has local patches.

This is a dated case study from 2026-08-28, not a claim about current Hermes behavior. Re-check the live checkout and current upstream before acting.

## Symptom and boundary isolation

Observed behavior:

- Ctrl+Shift+F found recent Telegram sessions.
- Desktop's Telegram sidebar showed no sessions newer than 17 days.
- The canonical `state.db` contained the recent sessions.
- Reproducing the Desktop messaging-sidebar `SessionDB.list_sessions_rich()` call directly against the live database returned only old root rows.

That combination isolated the defect to the **remote backend session-list query**, not the Electron renderer and not Telegram storage. The processes were distinct:

- Telegram gateway: writes and routes messaging sessions.
- Dashboard/serve backend: answers Desktop sidebar/session requests.
- Desktop renderer: displays the backend response.

Use precise language: this was a remote backend/session-listing bug, not a rendering bug.

## Root cause pattern

Telegram `/new` created a new user-visible session while retaining `parent_session_id` lineage to the prior session. The parent ended with `end_reason='session_reset'`.

The old listability rule surfaced roots and recognized branches, while compression projection followed only parents ended as `compression`. Reset-created children were therefore neither independently listable nor valid compression tips. Search used a broader path and still found them.

A key semantic requirement: `/new` sessions are separate user-visible conversations. Do **not** fix this by projecting the whole reset chain to one latest tip.

## Upstream implementation that resolved the class

At the time of the case, current upstream had a three-commit sequence:

1. `ce89afa59ccaaf063694f7670153d62ff69dfa55` — keep reset conversations listable
2. `5a10537b24f84b1f16f5d8d84201558b56b92c23` — stabilize legacy reset lineage on resume
3. `91c7a67f44f60c984d12283569bfe073a7d57624` — close the `session_switch` legacy gap and fence resume walking

The implementation combined:

- A stable `model_config._reset_from` marker on newly created reset children.
- A conservative legacy fallback requiring the same nonempty `session_key` plus a recognized parent reset end reason.
- Inclusion of reset children in `_LISTABLE_CHILD_SQL`.
- Exclusion of reset children from `_ephemeral_child_sql()`.
- Preservation of `_reset_from` through create/upsert paths.
- Stabilization of markerless children before reopening clears a parent's mutable reset end reason.
- Exclusion of reset children from compression/resume forward walkers.

This was superior to simply allowing every child whose parent once had `session_reset`: the stable marker and same-routing-key fallback avoid exposing subagents or unrelated children.

## Read-only confirmation against existing data

Before installing anything, reproduce the candidate listability predicate in SQLite read-only mode or in a temporary copied database. Verify that it returns recent reset children newest-first while preserving separate rows for the prior sessions.

Never open the live database through a writable `SessionDB` merely to test a proposed listing fix. Use SQLite URI `mode=ro`, the authenticated read-only sidebar endpoint, or a temporary SQLite backup.

## Update-target gate

In this case:

- The production checkout was at `9829746dfe3d5077f4f076e257505ec7f8feaa65`.
- `hermes version` reported `Up to date`.
- `git ls-remote origin refs/heads/main` showed a newer upstream commit containing the fix.

Lesson: **the updater's release target and GitHub `main` are not necessarily the same thing.** Before saying “update will fix it”:

1. Resolve the exact updater/release target.
2. Confirm the required commits are ancestors of that target.
3. If the fix is only on unreleased `main`, choose explicitly:
   - wait for the official release; or
   - apply the complete reviewed hotfix sequence.
4. Do not move a patched production install wholesale to unreleased `main` just to obtain one bug fix.
5. Do not cherry-pick only the first commit when known follow-ups close correctness gaps.

Useful ancestry check after fetching into an isolated candidate checkout:

```bash
git merge-base --is-ancestor <required-commit> <target-sha>
```

Exit 0 means the target contains the required commit.

## Dirty production checkout procedure

When the live Hermes repo has local changes:

1. Inventory tracked modifications, untracked additions, stashes, config, service units, and live process identities.
2. Export a binary-safe tracked patch, archive untracked files with paths/modes, and create a source snapshot.
3. Back up `state.db` with SQLite's backup API, not plain `cp` against a live WAL database.
4. Classify each customization by purpose. In the case study these were MiMo voice, Trafilatura extraction, geolocation exposure, and background-review filing policy.
5. Rehearse the target update and semantic patch replay in an isolated checkout and candidate venv.
6. Keep each surviving local customization as its own commit so future updates can rebase/cherry-pick them independently.
7. Use an out-of-band shell or detached user service for deployment because restarting the Desktop backend disconnects the controlling Desktop session.
8. Restart the Desktop backend and verify the sidebar first; only then restart the messaging gateway so future reset rows receive the new marker.

## Acceptance tests

A complete verification should prove:

- root and reset-created sessions are both listable;
- newest reset sessions sort first;
- old unmarked same-session-key reset children are recognized;
- compression projection is unchanged;
- branch visibility is unchanged;
- subagent/delegate children remain hidden;
- reset children are not ephemeral/cascade-delete targets;
- reset children are not followed as compression continuations;
- Desktop's messaging endpoint includes the reset child;
- a new post-update Telegram `/new` session appears immediately and carries `_reset_from`;
- no archiving, schema migration, or database rewrite was required to reveal existing sessions.

## Service restart split

For the listing fix:

- Restart the dashboard/serve backend to load the corrected query.
- Restart the messaging gateway to load future `_reset_from` marker creation.
- A renderer rebuild is unnecessary when the Electron client already displays the backend's response correctly.

On Angela's system, follow the healthy gateway's documented SIGUSR1/systemd graceful-restart procedure from the main `hermes-agent` skill, and verify the replacement PID/start time. Restart the dashboard from an out-of-band shell because doing so drops the active Desktop connection.