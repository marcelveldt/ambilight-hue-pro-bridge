# AGENTS.md

A lightweight always-on service that presents a virtual Philips Hue bridge on the LAN so
older Philips Ambilight+Hue TVs can connect to it, and forwards their light updates to a
real Hue bridge (V2 or Pro) over the low-latency Entertainment API. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the protocol notes, module map, and the
milestone plan.

## Behaviour

- NEVER automatically reply on GitHub (PRs or Discussions) without explicit consent from the developer.

## Development Commands

- `scripts/setup.sh` - Initial setup (venv, dependencies, pre-commit hooks). Also installs the
  sibling `../hue-entertainment` library editable for parallel local development.
- `pytest` - Run all tests
- `pre-commit run --all-files` - Run all pre-commit hooks
- `python -m ambilight_hue_bridge --log-level debug` - Run the service locally
- Requires Python 3.13+.

Always run `pre-commit run --all-files` after a code change to ensure it adheres to standards.

## Related projects

- [`hue-entertainment`](https://github.com/music-assistant/hue-entertainment) - the outbound
  Hue Entertainment streaming client (shared with Music Assistant). Checked out as a sibling
  directory during development.
- [`aiohue`](https://github.com/home-assistant-libs/aiohue) - reference CLIP v2 models (a pure
  API client; it does not do entertainment streaming).

## Code Style

### Comments

Only use comments to explain complex, multi-line blocks of code. Do not comment obvious
operations. Inline comments explain code that needs explaining; respect existing comments
from authors — they had a reason to write them, don't remove them unless needed.

### Docstring Format

Use Sphinx-style docstrings with `:param:` syntax. For simple functions, a single-line
docstring is fine. Don't explain inner workings in docstrings (use inline comments for
that); the docstring provides clarity to the caller, not a technical explanation. Use the
multi-line form where the summary starts on the next line:

```python
def my_function(param1: str, param2: int, param3: bool = False) -> str:
    """
    Brief one-line description of the function.

    :param param1: Description of what param1 is used for.
    :param param2: Description of what param2 is used for.
    :param param3: Description of what param3 is used for.
    """
```

Do **not** use Google-style (`Args:`) or bullet-style (`- param:`) docstrings.

### File structure

- Private methods at the bottom of the file/class, public at the top.
- Split into multiple controllers/modules where it improves clarity (see the module map in
  docs/ARCHITECTURE.md).
- Prefer dataclasses (and `mashumaro` for (de)serialization) for data models and config.
- No blocking I/O on the asyncio event loop.

## Branching and PRs

- PR titles are a functional description of the change — no conventional-commit prefixes
  (`feat:`, `fix:`, `chore:`, ...). Labels categorize PRs, not the title.
