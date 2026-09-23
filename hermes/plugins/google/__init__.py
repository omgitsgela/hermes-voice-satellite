"""Google plugin — registers the ``google`` tool.

Split like the hubitat plugin: registration and the schema here, logic in ``google_tool.py``.

This exists so the voice surface can reach Angela's tasks, calendar and contacts without a
terminal, which that surface deliberately does not have. Everything runs through her `gog` CLI
with two independent allowlists: the subcommand list here, and gog's own
``--enable-commands-exact``. There is no delete action, and no file or path argument anywhere.
"""

from __future__ import annotations

import json

from .google_tool import GoogleError, gog_bin, handle

_TOOLSET = "google"

_SCHEMA = {
    "name": "google",
    "description": (
        "Angela's Google Workspace: tasks, calendar and contacts, through her gog CLI. "
        "Tasks: action='tasks_lists' to see the lists, 'tasks_list' with a list name to read one "
        "(defaults to My Tasks), 'task_add' with a title to add one, 'task_complete' with a title "
        "to mark one done. Calendar: 'calendar_today' for today, 'calendar_list' with from/to "
        "(accepts 2026-09-22, today, tomorrow) and an optional query, 'calendar_calendars' to list "
        "the calendars, 'event_add' with summary, from, to and an optional calendar/location to "
        "Contacts: 'contacts_search' with a query for a name, email or phone. "
        "Places: 'places_search' with a query finds businesses near home and returns their address, "
        "distance in miles and real opening hours, which is what to use for any 'nearest', "
        "'near me' or 'what are their hours' question. Never state opening hours from a web search "
        "result when this action is available: call it. "
        "Writes are verified by reading back: report the 'verified' field, never assume success. "
        "There is no way to delete anything here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["tasks_lists", "tasks_list", "task_add", "task_complete",
                         "calendar_today", "calendar_list", "calendar_calendars", "event_add",
                         "contacts_search", "places_search"],
                "description": "What to do. The read actions are tasks_list, calendar_today, "
                               "calendar_list, calendar_calendars and contacts_search.",
            },
            "list": {
                "type": "string",
                "description": "Task list name, e.g. 'My Tasks', 'Backlog', 'Connections Museum', "
                               "'Rocky Mountain Rails', 'Canadian Citizenship'. Defaults to My Tasks.",
            },
            "title": {
                "type": "string",
                "description": "Task title, for task_add or for matching one in task_complete.",
            },
            "due": {
                "type": "string",
                "description": "Task due date for task_add: YYYY-MM-DD, today or tomorrow.",
            },
            "notes": {
                "type": "string",
                "description": "Optional task notes for task_add.",
            },
            "calendar": {
                "type": "string",
                "description": "Calendar name for event_add, e.g. 'Angela Personal'. Defaults to "
                               "the personal calendar.",
            },
            "summary": {
                "type": "string",
                "description": "Event title, for event_add.",
            },
            "from": {
                "type": "string",
                "description": "Start, as 2026-09-22T14:00 (a time is required for event_add). "
                               "For listing, a date or the words today or tomorrow.",
            },
            "to": {
                "type": "string",
                "description": "End, same format as from.",
            },
            "location": {
                "type": "string",
                "description": "Optional event location for event_add.",
            },
            "description": {
                "type": "string",
                "description": "Optional event description for event_add.",
            },
            "query": {
                "type": "string",
                "description": "Search text, for calendar_list, contacts_search or places_search "
                               "(e.g. 'Las Delicias').",
            },
            "near": {
                "type": "string",
                "description": "Optional origin for places_search: 'home' (the default) or a place "
                               "or address to search around, e.g. 'Golden'.",
            },
        },
        "required": ["action"],
    },
}


def register(ctx) -> None:
    """Register the google tool with the plugin context."""
    ctx.register_tool(
        name="google",
        toolset=_TOOLSET,
        schema=_SCHEMA,
        handler=lambda args, **kwargs: handle(args),
        check_fn=_gog_available,
        requires_env=[],
        description="Google tasks, calendar and contacts via gog",
        emoji="📆",
    )


def _gog_available() -> bool:
    try:
        gog_bin()
        return True
    except GoogleError:
        return False


__all__ = ["register", "handle", "GoogleError", "json"]
