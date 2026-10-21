# Notes for coding agents (Codex, Claude Code, …)

## Environment

- Use the project venv: `.venv/bin/python`, `.venv/bin/mpb`. System `python3` has no deps.
- Tests: `.venv/bin/python -m pytest -q tests` (19 tests, <1 s).
- Never install new runtime dependencies: the package is stdlib-only on purpose and the
  merger under `motion_photo_bridge/_vendor/` is third-party code — do not edit it.

## Do not

- Do not `git commit`, `git push`, tag or publish. The parent agent reviews your diff and commits.
- Do not touch real media: no `/mnt/*`, no Immich database, no `IMMICH_*` paths. Use
  `docs/sample/` and `tmp_path` fixtures instead.
- Do not add personal paths, emails or hostnames as defaults.

## What good looks like

- `mpb convert` must stay safe on a messy, read-only-ish folder: skip junk, never leave a
  half-written output, never overwrite an existing file without `--overwrite`.
- Functions that touch Immich must validate UUIDs/emails (`motion_photo_bridge/sql.py`) and
  quote literals; no f-string interpolation of user input.
- New behaviour comes with a test in `tests/` that fails before the change.
