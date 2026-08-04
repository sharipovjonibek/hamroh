"""Provider-neutral JSON Schema enforced on each model turn's output."""

from __future__ import annotations

import json

#: Hard cap on ``reason``. At ~4 chars/token that's ~25 tokens worst case;
#: paired with the system-prompt nudge ("≤10 words, terse") a well-behaved
#: turn costs far less. Without a cap a single rambling justification can
#: burn 100+ tokens — cheap per turn, expensive over a long session.
REASON_MAX_LENGTH = 100

#: Keep this a flat object: the legacy Anthropic tool schema rejects top-level
#: conditionals, while OpenAI strict structured outputs require every property
#: to appear in ``required``. Nullable fields preserve the semantic optionality;
#: the "non-empty reason on stop/skip" invariant is still enforced client-side
#: by :class:`~hamroh.models.ControlAction`.
CONTROL_ACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["stop", "skip", "sleep", "heartbeat"],
            "description": (
                "What to do after this turn. 'stop' is valid only after a "
                "reply was delivered via telegram_send_message or telegram_reply_to_message — "
                "plain text blocks are never shown to the user. "
                "'skip' means deliberately sending nothing (group chatter not "
                "addressed to you, or the user explicitly asked for no reply)."
            ),
        },
        "reason": {
            "type": ["string", "null"],
            "maxLength": REASON_MAX_LENGTH,
            "description": (
                "Terse justification (≤10 words). "
                "REQUIRED non-empty when action is 'stop' or 'skip'. "
                "Use null when action is 'sleep' or 'heartbeat' and no reason is needed."
            ),
        },
        "sleep_ms": {
            "type": ["integer", "null"],
            "description": "Only used when action == 'sleep'.",
        },
    },
    "required": ["action", "reason", "sleep_ms"],
    "additionalProperties": False,
}


def schema_json() -> str:
    return json.dumps(CONTROL_ACTION_SCHEMA)
