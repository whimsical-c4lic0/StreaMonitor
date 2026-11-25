"""Compile every Jinja template used by the web skins.

This is the project's lightweight template check: it catches syntax errors and
missing extends/include targets without adding a JavaScript build step or a
second formatting toolchain. Run it through ``scripts/check.sh``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKINS_DIR = PROJECT_ROOT / "streamonitor" / "managers" / "httpmanager" / "skins"

# Registered by HTTPManager on the Flask app. The check only needs their names
# to compile templates; rendering behavior is covered by the HTTP UI tests.
CUSTOM_FILTERS = ("abstime", "reltime", "status_icon_class", "status_text", "tohumanfilesize")


def main() -> int:
    failures = 0
    checked = 0

    for templates in sorted(SKINS_DIR.glob("*/templates")):
        environment = Environment(loader=FileSystemLoader(str(templates)), autoescape=True)
        for filter_name in CUSTOM_FILTERS:
            environment.filters[filter_name] = lambda value: value
        for path in sorted(templates.rglob("*.jinja")):
            name = path.relative_to(templates).as_posix()
            checked += 1
            try:
                environment.get_template(name)
            except TemplateError as exc:
                print(f"{path.relative_to(PROJECT_ROOT)}: {exc}", file=sys.stderr)
                failures += 1

    if failures:
        print(f"template check failed: {failures} of {checked} templates", file=sys.stderr)
        return 1

    print(f"template check passed: {checked} templates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
