# Known failing tests (as of 2026-10-02)

67 of 529 tests fail on `main` as committed, independent of any work-in-progress
branch changes (verified by stashing the uncommitted vision-feature diff and
re-running — the baseline still fails 65 of them; 2 more are specific to code
not yet committed). CI is intentionally turned on while these are still red so
regressions stop piling up; it was not possible to run `pytest` in any
environment before this, so these had been silently accumulating.

None of these were touched as part of the CI/test infra setup — they involve
synthesizer safety/compliance phrasing and other product behavior that needs a
product-owner call, not a drive-by fix.

## By area (full list in CI output)

- **`test_visible_tier.py` / `TestRenderActions`, `TestRenderSafety`** — the
  visible action panel no longer caps at 4 bullets or drops over-length items,
  and the safety line no longer asserts "published range, not a code limit"
  phrasing. Likely lost during the `perf(agent): cut turn latency and cost by
  ~2x` / `Update de code para reducir tokens y latencia` passes.
- **`test_published_bands.py`** — band degradation, ceiling/floor phrasing in
  both languages, deterministic status panel wiring.
- **`test_archetypes.py` / `TestElSynthesizerHonraLosStatus`,
  `TestConflictoDeRestriccion`** — synthesizer not honoring computed status
  (ceiling/compliance wording, target/cause ordering).
- **`test_visible_readings.py`** — second-person voice rules, Spanish
  phrasing, retry-on-contract-violation.
- **`test_planner_contract.py`** — planner prompt missing/duplicating rules
  the test asserts must be present (splitting cost, dependency discipline).
- **`test_nodes.py` / `TestBuildContextNode`, `TestPlanner`** — per-turn
  channel reset, language fallback, token-budget routing.
- **`test_state.py`** — required state keys no longer match the channels that
  should survive a turn reset.
- **`test_llm.py`** — specialist "thinking budget" no longer bounded as
  expected.
- **`test_middleware.py` / `TestRetiradaDelSchema`** — tool schema not
  retiring from the model after its budget is exhausted.
- **`test_graph.py` / `test_agent.py`** — node destination wiring and
  retrieval-tool assignment no longer match the declared contract.

## Suggested next step

Triage one area at a time against the actual synthesizer/safety behavior you
want today, decide per case whether the test or the code is stale, then fix
and delete from this file. Do not bulk-fix without reading each test's intent
— several encode safety/compliance requirements specific to pool chemistry.
