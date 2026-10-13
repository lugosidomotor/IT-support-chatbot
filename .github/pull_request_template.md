## Summary

## Checklist
- [ ] `uv run pytest` passes
- [ ] The agent stays strictly read-only (no new write tools, policy layers intact)
- [ ] `uv run python scripts/build_demo.py` was run if `app/static` or `demo/` changed
- [ ] No credentials or `.env` files are included
