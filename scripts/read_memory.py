"""Read every memory record under one actor. Usage: read_memory.py <actorId>"""

import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_PROFILE", "fluidra-dev")
os.environ.setdefault("AWS_REGION", "us-east-2")
os.environ.setdefault("MARLIN_MEMORY_ID", "marlin_memory_dev-hpPgDLBa66")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.memory_writer import get_client, memory_id  # noqa: E402

if len(sys.argv) < 2:
    sys.exit("usage: read_memory.py <actorId>")
actor = sys.argv[1]

client = get_client()
resp = client.list_memory_records(
    memoryId=memory_id(), namespacePath=f"/{actor}", maxResults=50
)
records = resp.get("memoryRecordSummaries") or resp.get("memoryRecords") or []

print(f"\n{len(records)} record(s) under /{actor}\n")
for r in records:
    ns = (r.get("namespaces") or ["?"])[0]
    text = (r.get("content") or {}).get("text", "")
    print(f"[{ns}]\n  {text}\n")