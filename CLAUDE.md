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
