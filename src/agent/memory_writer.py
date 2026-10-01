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
import time
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

_READ_CONNECT_TIMEOUT_S = 2.0
_READ_TIMEOUT_DEADLINE_S = 3.0
_READ_MAX_ATTEMPTS = 1

_client = None
_read_client = None
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


def _build_client(connect_timeout: float, read_timeout: float, attempts: int):
    """Build a data-plane client with an explicit timeout budget."""
    region = os.getenv("AWS_REGION") or "us-east-2"
    profile = os.getenv("AWS_PROFILE") or None

    session = (
        boto3.Session(profile_name=profile, region_name=region)
        if profile
        else boto3.Session(region_name=region)
    )
    return session.client(
        "bedrock-agentcore",
        config=Config(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"max_attempts": attempts, "mode": "standard"},
        ),
    )


def get_client():
    """
    The write client. Generous timeouts: it runs after the user already has
    their answer, so waiting costs nothing they can perceive.

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
        _client = _build_client(
            _CONNECT_TIMEOUT_S, _READ_TIMEOUT_S, _MAX_ATTEMPTS
        )
        logger.info("AgentCore Memory write client ready")
    return _client


def get_read_client():
    """
    The read client. Separate from the write client because it needs a
    tighter budget: a stalled read delays the turn itself, and no retry --
    a second attempt would double the worst case for data the caller is
    willing to do without.
    """
    global _read_client
    if _read_client is not None:
        return _read_client

    with _client_lock:
        if _read_client is not None:
            return _read_client
        _read_client = _build_client(
            _READ_CONNECT_TIMEOUT_S,
            _READ_TIMEOUT_DEADLINE_S,
            _READ_MAX_ATTEMPTS,
        )
        logger.info("AgentCore Memory read client ready")
    return _read_client


def reset_client() -> None:
    """Drop both cached clients. For tests and credential rotation."""
    global _client, _read_client
    with _client_lock:
        _client = None
        _read_client = None
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

# ================================================================
# READ
# ================================================================


# How many facts to inject. The answer is consumed on a mobile app and the
# turn runs under TURN_DEADLINE_S; an unbounded block costs tokens every turn.
_MAX_FACTS = 12
_MAX_FACT_CHARS = 400


def vessel_facts_namespace(identity: TurnIdentity) -> str:
    """
    The namespace the vessel-facts strategy writes to.

    Single definition of the path, mirroring thread_key() in identity.py:
    a namespace built in two places will drift in one of them.
    """
    return (
        f"/{identity.user_id}"
        f"/sites/{identity.site_id}"
        f"/vessels/{identity.vessel_id}/facts"
    )


def read_vessel_facts(identity: TurnIdentity) -> list[str]:
    """
    Return the durable facts stored for the active vessel.

    Enumerates rather than searching. A vessel has few durable facts and we
    want all of them, deterministically: semantic search would add a way for
    the volume to go missing on the day it matters.

    Filtered by strategy id on purpose. The user-preference strategy extracts
    near-duplicates of the same statements, so an unfiltered read injects the
    same fact twice.

    Never raises. A retrieval failure yields an empty list and the turn runs
    without memory, which is how the agent behaved before this existed.
    """
    strategy_id = (os.getenv("MARLIN_STRATEGY_VESSEL_FACTS") or "").strip()
    if not strategy_id:
        logger.warning(
            "MARLIN_STRATEGY_VESSEL_FACTS is not set -- skipping memory read"
        )
        return []

    try:
        mem_id = memory_id()
    except MemoryConfigError as exc:
        logger.warning("memory read skipped: %s", exc)
        return []

    started = time.monotonic()
    try:
        response = get_read_client().list_memory_records(
            memoryId=mem_id,
            namespace=vessel_facts_namespace(identity),
            memoryStrategyId=strategy_id,
            maxResults=_MAX_FACTS,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "memory read failed (vessel=%s): %s: %s",
            identity.vessel_id,
            type(exc).__name__,
            exc,
        )
        return []

    facts: list[str] = []
    for record in response.get("memoryRecordSummaries") or []:
        text = ((record.get("content") or {}).get("text") or "").strip()
        if not text:
            continue
        facts.append(text[:_MAX_FACT_CHARS])

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        "memory read: %d fact(s) for vessel=%s in %.0f ms",
        len(facts),
        identity.vessel_id,
        elapsed_ms,
    )
    return facts

# ================================================================
# SESSION CONSOLIDATION
# ================================================================

_RECORD_KIND_SESSION_SUMMARY = "session_summary"
_MAX_SUMMARY_CHARS = 4000
_MAX_EVENTS_PER_SESSION = 100


def vessel_history_namespace(identity: TurnIdentity) -> str:
    """Where session summaries live for the active vessel."""
    return (
        f"/{identity.user_id}"
        f"/sites/{identity.site_id}"
        f"/vessels/{identity.vessel_id}/history"
    )


def read_session_transcript(
    identity: TurnIdentity, session_id: str
) -> list[tuple[str, str]]:
    """
    Return a finished session's exchanges as (role, text) pairs, oldest first.

    Reads from AgentCore rather than the checkpointer: the session being
    consolidated is not the one in state, and may be weeks old.
    """
    try:
        mem_id = memory_id()
    except MemoryConfigError as exc:
        logger.warning("transcript read skipped: %s", exc)
        return []

    try:
        response = get_read_client().list_events(
            memoryId=mem_id,
            actorId=identity.user_id,
            sessionId=session_id,
            includePayloads=True,
            maxResults=_MAX_EVENTS_PER_SESSION,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "transcript read failed (session=%s): %s: %s",
            session_id,
            type(exc).__name__,
            exc,
        )
        return []

    turns: list[tuple[str, str]] = []
    for event in response.get("events") or []:
        for item in event.get("payload") or []:
            conv = item.get("conversational") or {}
            text = ((conv.get("content") or {}).get("text") or "").strip()
            if text:
                turns.append((conv.get("role", "USER"), text))
    return turns


def session_already_consolidated(
    identity: TurnIdentity, session_id: str
) -> bool:
    """
    Has this session already been summarized?

    Compares in the client rather than filtering on metadata: sessionid is
    not among the memory's indexed keys, and those are immutable. A vessel
    accumulates one summary per session, so the list stays small.
    """
    try:
        mem_id = memory_id()
    except MemoryConfigError:
        return False

    try:
        response = get_read_client().list_memory_records(
            memoryId=mem_id,
            namespace=vessel_history_namespace(identity),
            maxResults=50,
        )
    except (ClientError, BotoCoreError) as exc:
        # Unknown rather than false, but returning False here would risk a
        # duplicate while returning True would lose the summary entirely.
        # A duplicate summary is the cheaper mistake.
        logger.warning(
            "consolidation check failed: %s: %s", type(exc).__name__, exc
        )
        return False

    for record in response.get("memoryRecordSummaries") or []:
        metadata = record.get("metadata") or {}
        stored = (metadata.get("sessionid") or {}).get("stringValue")
        if stored == session_id:
            return True
    return False


def write_session_summary(
    identity: TurnIdentity,
    session_id: str,
    summary_text: str,
    session_started_at: datetime | None = None,
) -> MemoryWriteResult:
    """
    Store a finished session's summary as a durable record.

    Written directly, with no strategy: the text is already a summary, so
    paying an extraction pass to summarize a summary buys nothing. This is
    the same path the robot inventory will use, which is why every record
    carries recordkind.

    Idempotent on two levels: the caller checks
    session_already_consolidated() first, and clientToken closes the short
    race where two tabs consolidate at once.
    """
    text = (summary_text or "").strip()
    if not text:
        return MemoryWriteResult(ok=False, error="EMPTY_SUMMARY")

    try:
        mem_id = memory_id()
    except MemoryConfigError as exc:
        logger.warning("summary write skipped: %s", exc)
        return MemoryWriteResult(ok=False, error=str(exc))

    record = {
        "requestIdentifier": f"summary-{session_id}"[:80],
        "namespaces": [vessel_history_namespace(identity)],
        "content": {"text": text[:_MAX_SUMMARY_CHARS]},
        "timestamp": session_started_at or datetime.now(timezone.utc),
        "metadata": {
            # recordkind separates this from robot inventory, which shares
            # the direct-write namespace pattern.
            "recordkind": {"stringValue": _RECORD_KIND_SESSION_SUMMARY},
            "sessionid": {"stringValue": session_id},
        },
    }

    try:
        response = get_client().batch_create_memory_records(
            memoryId=mem_id,
            records=[record],
            clientToken=f"consolidate-{session_id}"[:80],
        )
    except ClientError as exc:
        # Same clientToken with a different payload: another process already
        # consolidated this session. The idempotency token did its job, so
        # this is a successful outcome, not a failure to log.
        code = (exc.response.get("Error") or {}).get("Code", "")
        message = str(exc)
        if code == "ValidationException" and "hash does not match" in message:
            logger.info(
                "session %s already consolidated by another writer", session_id
            )
            return MemoryWriteResult(ok=True)
        logger.warning(
            "summary write failed (session=%s): %s: %s",
            session_id,
            type(exc).__name__,
            exc,
        )
        return MemoryWriteResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    except BotoCoreError as exc:
        logger.warning(
            "summary write failed (session=%s): %s: %s",
            session_id,
            type(exc).__name__,
            exc,
        )
        return MemoryWriteResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    failed = response.get("failedRecords") or []
    if failed:
        logger.warning("summary write rejected: %s", failed)
        return MemoryWriteResult(ok=False, error=str(failed))

    logger.info(
        "session summary stored (session=%s vessel=%s, %d chars)",
        session_id,
        identity.vessel_id,
        len(text),
    )
    return MemoryWriteResult(ok=True)


def read_session_summaries(identity: TurnIdentity, limit: int = 3) -> list[str]:
    """
    Return recent session summaries for the active vessel, newest first.

    This is what cold resumption injects. Never raises.
    """
    try:
        mem_id = memory_id()
    except MemoryConfigError:
        return []

    try:
        response = get_read_client().list_memory_records(
            memoryId=mem_id,
            namespace=vessel_history_namespace(identity),
            maxResults=50,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "summary read failed: %s: %s", type(exc).__name__, exc
        )
        return []

    rows = []
    for record in response.get("memoryRecordSummaries") or []:
        metadata = record.get("metadata") or {}
        kind = (metadata.get("recordkind") or {}).get("stringValue")
        if kind != _RECORD_KIND_SESSION_SUMMARY:
            continue
        text = ((record.get("content") or {}).get("text") or "").strip()
        if text:
            rows.append((record.get("createdAt"), text))

    rows.sort(key=lambda r: r[0] or "", reverse=True)
    return [text for _, text in rows[:limit]]


def previous_session_id(
    identity: TurnIdentity, current_session_id: str
) -> str | None:
    """
    The most recent session for this actor other than the current one.

    No threshold: a new session id means the previous one is already
    closed in practice -- app.py only mints one when the user starts a new
    conversation or switches vessel. Asking whether twelve hours passed
    would answer a question the caller already decided.

    Returns None when this is the actor's first session.
    """
    try:
        mem_id = memory_id()
    except MemoryConfigError:
        return None

    try:
        response = get_read_client().list_sessions(
            memoryId=mem_id, actorId=identity.user_id, maxResults=20
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "session list failed: %s: %s", type(exc).__name__, exc
        )
        return None

    others = [
        s for s in (response.get("sessionSummaries") or [])
        if s.get("sessionId") and s.get("sessionId") != current_session_id
    ]
    if not others:
        return None

    others.sort(key=lambda s: s.get("createdAt") or 0, reverse=True)
    return others[0]["sessionId"]


@dataclass(frozen=True)
class SessionSummary:
    """A consolidated session, as the UI needs it."""

    session_id: str
    text: str
    created_at: datetime | None


def read_session_history(
    identity: TurnIdentity, limit: int = 20
) -> list[SessionSummary]:
    """
    Consolidated sessions for the active vessel, newest first.

    Scoped by namespace, so the vessel filter is free -- list_sessions is
    per actor and would need a transcript read per session to tell which
    pool it belonged to.

    Only consolidated sessions appear. The current one and any that held
    nothing worth summarizing are absent by design.
    """
    try:
        mem_id = memory_id()
    except MemoryConfigError:
        return []

    try:
        response = get_read_client().list_memory_records(
            memoryId=mem_id,
            namespace=vessel_history_namespace(identity),
            maxResults=50,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "session history read failed: %s: %s", type(exc).__name__, exc
        )
        return []

    rows: list[SessionSummary] = []
    for record in response.get("memoryRecordSummaries") or []:
        metadata = record.get("metadata") or {}
        if (metadata.get("recordkind") or {}).get("stringValue") != (
            _RECORD_KIND_SESSION_SUMMARY
        ):
            continue
        text = ((record.get("content") or {}).get("text") or "").strip()
        session_id = (metadata.get("sessionid") or {}).get("stringValue") or ""
        if not text:
            continue
        rows.append(
            SessionSummary(
                session_id=session_id,
                text=text,
                created_at=record.get("createdAt"),
            )
        )

    rows.sort(
        key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return rows[:limit]