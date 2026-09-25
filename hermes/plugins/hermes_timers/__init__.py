"""Hermes tool for durable voice timers and cron-backed alarm inventory."""
from __future__ import annotations

from .timer_tool import TimerToolError, handle

_TOOLSET = "hermes_timers"
_SCHEMA = {
    "name": "hermes_timers",
    "description": (
        "Set, list, and cancel relative voice timers, or list clock-time alarms. "
        "Timers accept a positive number of seconds and an optional spoken label. "
        "Use alarm_list only to inspect cron jobs whose names begin with 'alarm:'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["timer_set", "timer_list", "timer_cancel", "alarm_list"],
            },
            "seconds": {"type": "integer", "minimum": 1},
            "label": {"type": "string", "maxLength": 120},
            "id": {"type": "string"},
            "all": {"type": "boolean"},
        },
        "required": ["action"],
    },
}


def register(ctx) -> None:
    ctx.register_tool(
        name="hermes_timers",
        toolset=_TOOLSET,
        schema=_SCHEMA,
        handler=lambda args, **kwargs: handle(args),
        check_fn=_bridge_available,
        requires_env=[],
        description="Set/list/cancel voice timers and list alarm cron jobs",
        emoji="⏲️",
    )


def _bridge_available() -> bool:
    try:
        from .timer_tool import TOKEN_FILE
        return TOKEN_FILE.is_file()
    except Exception:
        return False


__all__ = ["register", "handle", "TimerToolError"]
