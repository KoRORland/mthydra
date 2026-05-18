# mthydra

Resilient Telegram access controller. See `doc/design.md` for the architecture, `doc/build-plan.md` for the artifact decomposition, and `doc/specs/` and `doc/plans/` for individual artifact specs and implementation plans.

## Development

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```
