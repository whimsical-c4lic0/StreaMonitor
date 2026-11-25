# Code quality, naming, and organization

## Steps

- [ ] Configure reproducible linting, formatting, and type checking.
- [ ] Apply mechanical cleanup separately from behavior changes.
- [ ] Standardize names and remove ambiguous module organization.
- [ ] Improve entry points and imports with compatibility preserved.
- [ ] Enforce conventions in CI and contributor documentation.

## Priority and scope

P2. Improve `pyproject.toml`, Python modules, entry points, and template conventions without a large rename/refactor mixed into correctness fixes.

## Implementation details

### Tooling baseline

- Add explicit Ruff settings, including supported Python target, line length, exclusions, lint rules, and formatting policy. Pin/manage tool versions in development dependencies and the lockfile.
- Start with import ordering, unused imports, syntax/error checks, and bare-except cleanup. Review automatic changes rather than applying unsafe fixes blindly.
- The review's isolated E4/E7/E9/F/I check found 86 issues, mainly 40 import-order and 34 unused-import findings. Re-establish counts using the chosen version/configuration.
- Introduce one type checker, initially at configuration, service, backend, and snapshot boundaries. Avoid mass Any annotations or blanket ignores solely to pass CI.
- Select lightweight template lint/format tooling suitable for Flask/Jinja/HTMX. Do not add a frontend build pipeline without a concrete need.

### Naming and layout

- Prefer snake_case modules/functions consistently, including cli_manager, zmq_manager, and disk_space_monitor where renaming is justified.
- Review the empty `streamonitor/managers/httpmanager.py` beside the `httpmanager/` package; remove ambiguity after checking imports and external usage.
- Keep mechanical renames isolated and update imports, docs, tests, and container entry points together.
- Preserve Downloader.py/Controller.py compatibility wrappers if introducing lowercase modules or console scripts.
- Consider installable packaging and explicit CLI entry points to remove path hacks. A src layout is optional and must not delay higher-priority work.

### Script behavior

- Ensure executable modules have main guards and imports do not start services.
- Use argparse with useful no-argument/help behavior, clear exit codes, and no accidental conflict between help and host options.
- Keep settings independent of the caller's working directory where practical.

## Acceptance criteria

- Fresh frozen development installs run identical lint/format/type commands locally and in CI.
- Mechanical cleanup introduces no intentional behavior change and passes the offline suite.
- Renamed entry points/imports retain documented compatibility or have an explicit migration path.
- No redundant same-name module/package ambiguity remains after compatibility review.
- New boundary code is type-checked under a documented incremental policy.

## Dependencies and rollout

Coordinate entry points with [05](05-architecture-and-service-boundaries.md), enforcement with [06](06-ci-cd.md), and docs with [10](10-documentation-and-observability.md). Prefer separate commits for tooling setup, formatting, unused-code cleanup, naming, and functional changes.
