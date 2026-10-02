"""
Smoke check: can we open a session against the configured Neo4j instance?

Reads NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD from the environment (same
names as .env.example) — never hardcode credentials here. Run manually
against a given environment; not wired into pytest/CI since it needs live
network access and real credentials.
"""

import os
import sys
import traceback

import certifi
from dotenv import load_dotenv
from neo4j import GraphDatabase

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
load_dotenv()

uri = os.environ.get("NEO4J_URI")
user = os.environ.get("NEO4J_USER")
password = os.environ.get("NEO4J_PASSWORD")

if not all([uri, user, password]):
    sys.exit("Set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD (see .env.example)")

driver = GraphDatabase.driver(uri, auth=(user, password))
try:
    driver.verify_connectivity()
    print("OK")
except Exception as e:
    traceback.print_exception(type(e), e, e.__traceback__)
    sys.exit(1)
