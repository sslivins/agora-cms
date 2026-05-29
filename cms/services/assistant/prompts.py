"""System-prompt builder for the Assistant feature.

PR 3a uses a deliberately minimal system prompt — no tool descriptions,
no schema dumps, just the role and a short list of conversational
rules.  PR 3b will extend this with the MCP tool catalogue once the
agent can actually invoke tools.

The prompt is rebuilt fresh on every turn so context (current UTC
time, the caller's display name) is always up to date.  It is NOT
persisted to ``chat_messages`` — only user / assistant / tool turns
are stored.
"""
from __future__ import annotations

from datetime import datetime, timezone

from cms.models.user import User


SYSTEM_PROMPT_TEMPLATE = """\
You are the Agora CMS Assistant, an in-app helper for operators of a
digital-signage platform.  The signed-in operator is **{username}**
({email}).  The current UTC time is {utc_now}.

Guidelines:
* Be concise.  Operators want answers, not essays.
* You do not have access to any tools yet.  If the user asks you to
  perform an action (create a schedule, modify a group, etc.), tell
  them that feature is coming soon and offer to walk them through how
  to do it in the UI.
* Never invent device IDs, asset IDs, schedule IDs, or other data
  about this deployment — you have no access to the database in this
  release.
* If the user asks who built you or how you work, you can say that
  you are powered by Azure OpenAI and run inside the Agora CMS.
"""


def build_system_prompt(user: User, *, now: datetime | None = None) -> str:
    """Render the system prompt for ``user``.

    ``now`` is injected for deterministic testing; defaults to current
    UTC time.
    """
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    return SYSTEM_PROMPT_TEMPLATE.format(
        username=user.username,
        email=user.email or "no email",
        utc_now=when,
    )
