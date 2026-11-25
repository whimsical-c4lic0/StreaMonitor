# Contributing

## Setup

Install the locked development environment with [uv](https://docs.astral.sh/uv/):

```sh
uv sync --frozen
```

`--frozen` uses the committed `uv.lock` without modifying it. Drop it only when
you intentionally change dependencies, then commit both `pyproject.toml` and
`uv.lock`.

## Checks

Run the same commands locally that CI runs:

```sh
./scripts/check.sh
```

That script runs, in order:

```sh
uv run --frozen --no-sync ruff check .
uv run --frozen --no-sync ruff format --check .
uv run --frozen --no-sync mypy
uv run --frozen --no-sync python scripts/check_templates.py
```

Apply fixes with:

```sh
uv run --frozen --no-sync ruff check --fix .
uv run --frozen --no-sync ruff format .
```

Run the offline test suite (the live-site tests are opt-in):

```sh
uv run --frozen --no-sync pytest -m "not e2e"
```

CI runs the same locked install and commands, plus the container smoke test and
the offline suite on the oldest and newest supported Python versions.

## Code conventions

* Use `snake_case` for modules and functions and `PascalCase` for classes.
* Keep imports at module top level. Importing a module must not start services,
  open sockets, or read configuration beyond module-level constants.
* Put executable behavior behind a `main(argv=None)` function and an
  `if __name__ == "__main__":` guard. Use `argparse`; keep `-h`/`--help` free
  and return meaningful exit codes.
* Resolve paths relative to the checkout root, not the caller's working
  directory. Use the helpers in `parameters.py` and the settings documented in
  `README.md`.
* Do not add a frontend build pipeline. Templates are plain Jinja/HTMX; the
  lightweight check in `scripts/check_templates.py` compiles every template and
  resolves `extends`/`include` targets, and the offline HTTP UI tests exercise
  the rendered pages.

### Type checking policy

Type checking is incremental. `pyproject.toml` checks the configuration,
manager/service, HTTP backend, model, and utility boundaries. Add annotations to
new code in those areas. Prefer a precise annotation over `Any`, and never add a
blanket `type: ignore`; use `# type: ignore[code]` with a short reason only for a
real third-party stub problem. The checked set should grow over time, not shrink.

## Commits

Keep mechanical work separate: one commit for tooling/configuration, one for
formatter/import/unused-code cleanup, one for renames, and one for behavior
changes such as argument parsing. This keeps review diffs attributable and makes
regressions easier to bisect.
