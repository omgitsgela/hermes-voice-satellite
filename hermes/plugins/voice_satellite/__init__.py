"""Voice satellite plugin — registers the ``voice_satellite`` tool.

Registration and schema here, logic in ``voice_tool.py``, split the same way as the hubitat and
google plugins.

This exists so the assistant can change how the device behaves in the room, including follow-up mode
and the bounded endpointing profile, by calling a tool instead of relying on the bridge recognising a
literal phrase.
"""

from __future__ import annotations

from .voice_tool import VoiceControlError, handle

_TOOLSET = "voice_satellite"

_SCHEMA = {
    "name": "voice_satellite",
    "description": (
        "Control for the voice satellite this assistant is speaking through. "
        "'follow_up_on' makes the device keep listening for about fifteen seconds after each answer, "
        "so a conversation continues without the wake word. 'follow_up_off' ends that. "
        "'follow_up_status' reports the current state. Use this for any request about following up, "
        "staying listening, or continuing the conversation after an answer, whatever words are used. "
        "'endpointing_pause_tolerant' allows longer mid-sentence pauses while retaining finite "
        "no-speech and utterance limits; 'endpointing_standard' restores ordinary timing, and "
        "'endpointing_status' reports the current profile. Use these for requests to wait longer "
        "while someone speaks. Every change is read back from the device before it is reported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "follow_up_on",
                    "follow_up_off",
                    "follow_up_status",
                    "endpointing_pause_tolerant",
                    "endpointing_standard",
                    "endpointing_status",
                ],
                "description": "Change or report a device conversation setting.",
            },
        },
        "required": ["action"],
    },
}


def register(ctx) -> None:
    """Register the voice_satellite tool with the plugin context."""
    ctx.register_tool(
        name="voice_satellite",
        toolset=_TOOLSET,
        schema=_SCHEMA,
        handler=lambda args, **kwargs: handle(args),
        check_fn=_bridge_available,
        requires_env=[],
        description="Follow-up mode control for the voice satellite",
        emoji="🔊",
    )


def _bridge_available() -> bool:
    """Available when the bridge's token file exists, which is the same host the bridge runs on."""
    try:
        from .voice_tool import TOKEN_FILE

        return TOKEN_FILE.is_file()
    except Exception:  # noqa: BLE001 - never let availability checking break registration
        return False


__all__ = ["register", "handle", "VoiceControlError"]
