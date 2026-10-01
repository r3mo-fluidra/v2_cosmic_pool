"""
Smoke test for the AgentCore Memory writer. Writes one fake turn and reads
it back. Run against DEV only -- it creates a real event.
"""

import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO)

os.environ.setdefault("AWS_PROFILE", "fluidra-dev")
os.environ.setdefault("AWS_REGION", "us-east-2")
os.environ.setdefault("MARLIN_MEMORY_ID", "marlin_memory_dev-hpPgDLBa66")

from pathlib import Path                               # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.identity import TurnIdentity          # noqa: E402
from src.agent.memory_writer import get_client, memory_id, write_turn  # noqa: E402

SUFFIX = str(int(time.time()))

identity = TurnIdentity(
    user_id=f"smokeuser{SUFFIX}",
    site_id="smokesite01",
    vessel_id="smokepool01",
    session_id=f"smokesession{SUFFIX}",
    turn_id=f"smoketurn{SUFFIX}",
    pool_pro_id="smokepro01",
)

result = write_turn(
    identity,
    user_message=(
        "My pool is 45000 liters, saltwater, outdoor. "
        "Combined chlorine has been high for three weeks."
    ),
    assistant_message=(
        "For a 45000 liter saltwater pool, persistent combined chlorine "
        "usually points to insufficient oxidation. Check your CYA level first."
    ),
)

print("\nWRITE RESULT:", result)
if not result.ok:
    sys.exit(1)

client = get_client()
events = client.list_events(
    memoryId=memory_id(),
    actorId=identity.user_id,
    sessionId=identity.session_id,
    includePayloads=True,
    maxResults=10,
)
print("\nEVENTS FOUND:", len(events.get("events", [])))
for ev in events.get("events", []):
    print(" -", ev.get("eventId"), ev.get("eventTimestamp"))
    for item in ev.get("payload", []):
        conv = item.get("conversational") or {}
        text = (conv.get("content") or {}).get("text", "")
        print(f"   [{conv.get('role')}] {text[:70]}")

print("\nIdentity for the follow-up check:")
print(f"  actorId   = {identity.user_id}")
print(f"  sessionId = {identity.session_id}")
print(f"  namespace = /{identity.user_id}/sites/{identity.site_id}"
      f"/vessels/{identity.vessel_id}/facts")