"""
Write completed conversation turns to Amazon Bedrock AgentCore Memory.

This module is transport-agnostic on purpose. It takes a TurnIdentity and
plain strings, and knows nothing about Streamlit, Flutter or HTTP. Moving
the frontend means changing who calls write_turn(), not rewriting it.

Short-term storage is synchronous (CreateEvent returns once the event is
stored). Long-term extraction runs asynchronously on the service side, so
records appear in their namespace some time after this call returns.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .identity import TurnIdentity

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

# Namespace variable keys declared on the Memory resource. They must match
# CreateMemory exactly: lowercase alphanumeric, no underscores.
_NS_SITE = "siteid"
_NS_VESSEL = "vesselid"

# A memory write must never delay a turn the user already finished. These
# are deliberately short: losing an event is cheaper than hanging.
_CONNECT_TIMEOUT_S = 3.0
_READ_TIMEOUT_S = 8.0
_MAX_ATTEMPTS = 2

_client = None
_client_lock = threading.Lock()


class MemoryConfigError(RuntimeError):
    """Raised at startup when required configuration is missing."""


@dataclass(frozen=True)
class MemoryWriteResult:
    """Outcome of a write attempt. Never raises -- callers inspect this."""

    ok: bool
    event_id: str | None = None
    skipped_extraction: bool = False
    error: str | None = None


def memory_id() -> str:
    """The Memory resource id, from the environment."""
    value = (os.getenv("MARLIN_MEMORY_ID") or "").strip()
    if not value:
        raise MemoryConfigError(
            "MARLIN_MEMORY_ID is not set. Memory writes are disabled."
        )
    return value


def get_client():
    """
    Lazily build the data-plane client.

    Double-checked locking: Streamlit reruns and the suggester thread pool
    can both reach this. boto3 clients are thread-safe once built, but
    building one twice wastes a credential resolution.
    """
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client

        region = os.getenv("AWS_REGION") or "us-east-2"
        profile = os.getenv("AWS_PROFILE") or None

        session = (
            boto3.Session(profile_name=profile, region_name=region)
            if profile
            else boto3.Session(region_name=region)
        )
        _client = session.client(
            "bedrock-agentcore",
            config=Config(
                connect_timeout=_CONNECT_TIMEOUT_S,
                read_timeout=_READ_TIMEOUT_S,
                retries={"max_attempts": _MAX_ATTEMPTS, "mode": "standard"},
            ),
        )
        logger.info("AgentCore Memory client ready (region=%s)", region)
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests and credential rotation."""
    global _client
    with _client_lock:
        _client = None


def write_turn(
    identity: TurnIdentity,
    user_message: str,
    assistant_message: str,
    *,
    skip_extraction: bool = False,
    state_vessel_id: str | None = None,
) -> MemoryWriteResult:
    """
    Store one completed turn as a single event.

    Both messages travel in one event: they are one exchange, and the
    extractor reads them together. Two events would double the calls and
    split the context the extraction depends on.

    `skip_extraction` stores the turn in short-term memory but keeps it out
    of long-term extraction. Use it for greetings and out-of-scope turns,
    where there is no durable fact to extract.

    `state_vessel_id` is the vessel the graph detected this turn, passed in
    only to be checked. The identity wins: it comes from the vessel selector
    and is the key the namespace is built from. A mismatch means the graph
    drifted from the selected vessel, which is worth a warning but not a
    failed write.

    Never raises. A lost memory event must not break a finished turn.
    """
    if state_vessel_id and state_vessel_id != identity.vessel_id:
        logger.warning(
            "vessel mismatch at write time: identity=%s state=%s -- "
            "writing to the identity namespace",
            identity.vessel_id,
            state_vessel_id,
        )

    user_text = (user_message or "").strip()
    assistant_text = (assistant_message or "").strip()
    if not user_text and not assistant_text:
        return MemoryWriteResult(ok=False, error="EMPTY_TURN")

    try:
        mem_id = memory_id()
    except MemoryConfigError as exc:
        logger.warning("memory write skipped: %s", exc)
        return MemoryWriteResult(ok=False, error=str(exc))
    
    payload = []
    if user_text:
        payload.append({"conversational": {"role": "USER", "content": {"text": user_text}}})
    if assistant_text:
        payload.append(
            {"conversational": {"role": "ASSISTANT", "content": {"text": assistant_text}}}
        )

    request = {
        "memoryId": mem_id,
        "actorId": identity.user_id,
        "sessionId": identity.session_id,
        "eventTimestamp": datetime.now(timezone.utc),
        "payload": payload,
        # One turn, one write: turn_id makes a retry idempotent instead of
        # storing the same exchange twice.
        "clientToken": identity.turn_id,
    }

    if skip_extraction:
        request["extractionMode"] = "SKIP"
    else:
        # Namespace variables are per event. Send the wrong vessel here and
        # the facts land in another pool's namespace, silently.
        request["extractionConfig"] = {
            "namespaceVariables": {
                _NS_SITE: identity.site_id,
                _NS_VESSEL: identity.vessel_id,
            }
        }

    try:
        response = get_client().create_event(**request)
    except (ClientError, BotoCoreError, MemoryConfigError) as exc:
        logger.warning(
            "memory write failed (session=%s turn=%s): %s: %s",
            identity.session_id,
            identity.turn_id,
            type(exc).__name__,
            exc,
        )
        return MemoryWriteResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    event_id = (response.get("event") or {}).get("eventId")
    logger.info(
        "memory write ok (session=%s turn=%s event=%s extraction=%s)",
        identity.session_id,
        identity.turn_id,
        event_id,
        "SKIP" if skip_extraction else "ON",
    )
    return MemoryWriteResult(
        ok=True, event_id=event_id, skipped_extraction=skip_extraction
    )