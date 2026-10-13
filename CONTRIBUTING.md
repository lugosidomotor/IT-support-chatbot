# Contributing

Thanks for your interest! This is a compact proof of concept, so please keep changes small and focused.

## Development setup

```bash
uv sync                     # installs Python 3.12 + dependencies (dev group included)
cp .env.example .env        # add OPENROUTER_API_KEY to chat for real
uv run uvicorn app.main:app --reload
```

## Before opening a pull request

```bash
uv run pytest                                  # must pass: unit, API and offline end-to-end tests
uv run python scripts/build_demo.py            # if you touched app/static or demo/
```

- Keep the agent **read-only**: never add write tools, and keep the deny-by-default layers intact
  (`app/agent/permissions.py`, the Atlassian allowlist, the GitHub repository allowlist).
- New tools need tests, including a negative test showing that they cannot escape the allowlist.
- Do not add infrastructure (databases, brokers, vector stores) without discussing it in an issue first.
- Never commit credentials. Tests must not require real credentials; live tests go in
  `tests/test_integration.py` and must skip cleanly.
