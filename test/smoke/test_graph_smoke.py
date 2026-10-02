"""
Smoke test: does the real LangGraph graph still compile and wire up?

Unlike test/agent/test_graph.py (which mocks StateGraph entirely to assert
the declared edges), this builds the actual graph with the default
InMemorySaver checkpointer. No network client is constructed: every Neo4j /
Gemini / Qdrant client in this codebase is built lazily inside a function,
not at import or build_graph() time, so this needs no credentials and no
mocking. It exists to catch what it already caught once while writing it: a
module imported by the graph (memory_writer.py -> boto3,
langgraph.checkpoint.sqlite) that isn't declared in requirements.txt, so
`pip install -r requirements.txt` succeeds but `build_graph()` dies with
ModuleNotFoundError the moment anyone actually runs the app.
"""

import src.agent.graph as graph_module

# The stable core, independent of in-flight feature branches (e.g. the
# vision node): every node here must exist, but extra nodes are fine.
CORE_NODES = {
    "build_context_node",
    "summarize_memory_node",
    "planner",
    "general",
    "oos",
    "orchestrator",
    "run_step",
    "synthesizer",
    "suggester",
}


def test_build_graph_compiles():
    app = graph_module.build_graph()
    assert app is not None


def test_core_nodes_are_present():
    app = graph_module.build_graph()
    nodes = set(app.get_graph().nodes)
    missing = CORE_NODES - nodes
    assert not missing, f"graph is missing expected nodes: {missing}"


def test_module_level_graph_instance_builds():
    """`graph` (src/agent/graph.py:graph = build_graph()) is what app.py
    actually imports and runs — this is the one that has to work."""
    assert graph_module.graph is not None
