"""
agent/identity.py
=================
Turn identity: who is asking, about which pool, in which conversation.

Five levels, slowest to fastest:

    user_id     the person             years
    site_id     the installation       years
    vessel_id   the body of water      years
    session_id  one conversation       hours
    turn_id     one exchange           a turn

vessel_id and session_id are independent axes: a vessel has many sessions
over its lifetime. The LangGraph thread key is derived from
(user_id, vessel_id, session_id), so switching pools opens a new thread by
construction rather than by remembering to reset one.

These identifiers end up in AgentCore namespaces, CloudWatch logs and
Langfuse traces. They must be opaque: never an email, phone number or any
other personal identifier.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

# Namespace path segments in AgentCore are "/"-separated, so an identifier
# carrying a slash, a space or a quote would silently reshape the path.
# Rejecting it here keeps the failure next to its cause.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")

_REQUIRED = ("user_id", "site_id", "vessel_id", "session_id")


class IdentityError(ValueError):
    """Raised when a turn identity is missing or malformed."""


def _check(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityError(f"{name} is required and must be a non-empty string")
    value = value.strip()
    if not _ID_PATTERN.match(value):
        raise IdentityError(
            f"{name}={value!r} is not a valid identifier: allowed characters "
            "are letters, digits and . _ : @ + - (max 128)"
        )
    return value


def new_session_id() -> str:
    """Opaque id for a new conversation."""
    return uuid.uuid4().hex


def new_turn_id() -> str:
    """Opaque id for a single exchange. Generated once per turn."""
    return uuid.uuid4().hex

def thread_key(user_id: str, vessel_id: str, session_id: str) -> str:
    """
    LangGraph thread key. Single definition, so callers that need the key
    before building a full TurnIdentity cannot drift from the format.
    """
    return f"{_check('user_id', user_id)}:{_check('vessel_id', vessel_id)}:{_check('session_id', session_id)}"

@dataclass(frozen=True)
class TurnIdentity:
    """Immutable identity for one turn. Built at the edge, read everywhere."""

    user_id: str
    site_id: str
    vessel_id: str
    session_id: str
    turn_id: str
    pool_pro_id: str | None = None

    def __post_init__(self) -> None:
        for field in _REQUIRED + ("turn_id",):
            object.__setattr__(self, field, _check(field, getattr(self, field)))
        if self.pool_pro_id is not None:
            object.__setattr__(
                self, "pool_pro_id", _check("pool_pro_id", self.pool_pro_id)
            )

    @property
    def thread_id(self) -> str:
        """
        LangGraph thread key. Includes vessel_id so that switching pools
        starts a new conversation instead of carrying the previous vessel's
        history into it.
        """
        return thread_key(self.user_id, self.vessel_id, self.session_id)

    def to_configurable(self) -> dict[str, str]:
        """Payload for config["configurable"]. thread_id is what LangGraph reads."""
        out = {
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "site_id": self.site_id,
            "vessel_id": self.vessel_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
        }
        if self.pool_pro_id:
            out["pool_pro_id"] = self.pool_pro_id
        return out

    def for_next_turn(self) -> "TurnIdentity":
        """Same conversation, fresh turn_id."""
        return TurnIdentity(
            user_id=self.user_id,
            site_id=self.site_id,
            vessel_id=self.vessel_id,
            session_id=self.session_id,
            turn_id=new_turn_id(),
            pool_pro_id=self.pool_pro_id,
        )


def identity_from_config(config: Mapping[str, Any] | None) -> TurnIdentity:
    """
    Read the identity a node needs out of a LangGraph config.

    Raises IdentityError rather than returning a partial identity: a node
    that silently falls back to a default would write memory under the wrong
    actor, and that is not recoverable once written.
    """
    configurable = (config or {}).get("configurable") or {}
    missing = [k for k in _REQUIRED if not configurable.get(k)]
    if missing:
        raise IdentityError(
            f"config['configurable'] is missing {', '.join(missing)}. "
            "Build it with TurnIdentity.to_configurable()."
        )
    return TurnIdentity(
        user_id=configurable["user_id"],
        site_id=configurable["site_id"],
        vessel_id=configurable["vessel_id"],
        session_id=configurable["session_id"],
        turn_id=configurable.get("turn_id") or new_turn_id(),
        pool_pro_id=configurable.get("pool_pro_id"),
    )