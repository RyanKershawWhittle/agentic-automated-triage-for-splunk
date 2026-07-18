"""Guardrail: validate agent-issued SPL before it reaches Splunk.

The triage agent composes its own SPL at runtime, so every query passes
through this gate first. The policy is deny-by-default for any command
that mutates state or exfiltrates data — the agent's Splunk access is
meant to be strictly read-only, and this enforces it at the choke point
rather than trusting the model to behave.
"""

from __future__ import annotations

import re

__all__ = ["SplGuardError", "check_spl"]


class SplGuardError(Exception):
    """Raised when SPL violates the read-only guardrail policy."""


# Commands that write, delete, or push data out of Splunk. Matched as
# pipeline commands (after a `|` or as the generating command), not as
# free-text, so a search for the literal word "delete" in log text is fine.
_FORBIDDEN_COMMANDS = frozenset({
    "delete",
    "collect",
    "mcollect",
    "meventcollect",
    "outputlookup",
    "outputcsv",
    "outputtext",
    "sendemail",
    "sendalert",
    "script",
    "runshellscript",
    "dump",
    "tscollect",
})

_PIPE_COMMAND_RE = re.compile(r"(?:^|\|)\s*`?([a-zA-Z_][a-zA-Z0-9_]*)")


def check_spl(spl: str) -> str:
    """Validate SPL against the read-only policy.

    Returns the SPL unchanged when clean; raises SplGuardError naming the
    offending command otherwise.
    """
    if not spl or not spl.strip():
        raise SplGuardError("Empty SPL query.")

    for match in _PIPE_COMMAND_RE.finditer(spl):
        command = match.group(1).lower()
        if command in _FORBIDDEN_COMMANDS:
            raise SplGuardError(
                f"SPL command '{command}' is blocked by the read-only guardrail. "
                "The triage agent may only run searches that read data."
            )
    return spl
