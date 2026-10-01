"""
Check that long-term extraction produced records for a smoke-test identity.
Pass the actorId from smoke_memory_write.py as the first argument.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_PROFILE", "fluidra-dev")
os.environ.setdefault("AWS_REGION", "us-east-2")
os.environ.setdefault("MARLIN_MEMORY_ID", "marlin_memory_dev-hpPgDLBa66")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.memory_writer import get_client, memory_id  # noqa: E402

actor = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: <actorId>")
client = get_client()

for label, kwargs in (
    ("hierarchical (everything under the actor)", {"namespacePath": f"/{actor}"}),
    ("vessel facts", {"namespace": f"/{actor}/sites/smokesite01/vessels/smokepool01/facts"}),
    ("prefs", {"namespace": f"/{actor}/prefs"}),
):
    try:
        resp = client.list_memory_records(
            memoryId=memory_id(), maxResults=20, **kwargs
        )
    except Exception as exc:
        print(f"\n{label}: {type(exc).__name__}: {exc}")
        continue

    records = resp.get("memoryRecordSummaries") or resp.get("memoryRecords") or []
    print(f"\n{label}: {len(records)} record(s)")
    for r in records:
        text = ((r.get("content") or {}).get("text") or "")[:100]
        print(f"  [{r.get('memoryStrategyId', '?')}] {text}")
        print(f"      ns={r.get('namespaces')}")
        