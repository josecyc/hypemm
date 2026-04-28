**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## Repo-Specific Orientation

Before changing this repository, read:

1. `docs/CONVENTIONS.md` — directory layout, config naming, the
   config-path-to-run-dir mapping, notebook rules
2. `docs/RUNBOOK.md` — how to fetch data, run backtests, launch instances,
   migrate state, recover from breakage
3. `docs/CURRENT_STATE.md` — what's running on the server right now
4. `README.md`

Hard rules in this repo:

- Configs live under `configs/<mode>/<stem>.toml` where mode is one of
  `backtest`, `paper`, `testnet`, `live`. Nothing belongs at `configs/` root.
- `data_dir` and `run_dir` are NOT TOML fields. Output paths are derived from
  the config path. Setting them in TOML is a hard error.
- Every run lands in `data/runs/<mode>/<stem>/`. Every market input lives in
  `data/market/...`. Inputs and outputs never share a directory.
- Notebooks consume committed artifacts; they do not invent parallel data
  layouts.

Before reporting a task done:

- `uv run pytest` passes (this includes structural invariants in
  `tests/test_repo_structure.py` and `tests/test_paired_configs.py`).
- If you changed what's deployed on the server: update `docs/CURRENT_STATE.md`.
- If you made a research claim: add a dated report under `docs/research/`
  naming the config, commit hash, and output dir.
- If you added a config: it loads cleanly, and if it's a live config, it has a
  matching paper twin at `configs/paper/<stem>.toml`.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

# Python Code Standards

## Style
- **PEP 8**: Use `black` for formatting (run before commits)
- **Naming**: `snake_case` functions/variables, `PascalCase` classes, `UPPER_CASE` constants
- **Type hints required**: `def process(data: list[str]) -> dict:`

## Code Organization

### Functions
- One function = one purpose
- ~20-30 lines, 50 max
- Early returns over nested ifs
```python
def validate_user(user: dict) -> bool:
    if not user.get("email"):
        return False
    if user.get("age", 0) < 18:
        return False
    return True
```

### Files
- Max ~300 lines per file
- Group related code in modules
- One class per file if >100 lines

### Imports
```python
# Standard → Third-party → Local
import os
import pandas as pd
from src.core import process_data
```

## Key Patterns

**Dataclasses for data:**
```python
from dataclasses import dataclass

@dataclass
class User:
    name: str
    email: str
```

**No mutable defaults:**
```python
def add_item(item, items=None):
    items = items or []
    items.append(item)
```

**Specific exceptions:**
```python
try:
    data = load_file(path)
except FileNotFoundError:
    raise ValueError(f"File not found: {path}")
```

## Documentation
- Docstrings on public functions (what, params, returns)
- Comments explain *why*, not *what*
```python
def calculate_average(numbers: list[float]) -> float:
    """Return arithmetic mean of numbers."""
    return sum(numbers) / len(numbers)
```

## Testing
- **Required for all new functionality**
- Aim for one test per logical behavior/edge case
- Name tests descriptively: `test_<function>_<scenario>`
```python
def test_calculate_average_returns_mean():
    assert calculate_average([1, 2, 3]) == 2.0
```

## Tools (uv)

- Always use uv (no pip, pipenv etc.)

**Setup:**
```bash
uv add --dev black ruff mypy pytest
```

**Run before commit:**
```bash
uv run black .
uv run ruff check .
uv run mypy .
uv run pytest
```

## Rules
1. No commented-out code
2. No `print()` statements (use logging or delete)
3. No bare `except:`
4. Type hints on all functions
5. **New functionality requires tests** - write tests before or alongside new code
6. **Never add Co-Authored-By Claude** to commit messages
7. Use fallbacks only if it is absolutely necessary! The program should not run if there is something wrong.
