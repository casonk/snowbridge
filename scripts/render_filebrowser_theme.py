#!/usr/bin/env python3
"""Render the portfolio (clockwork) themes into File Browser's branding directory.

Writes ``custom.css`` and ``theme.js`` into the branding directory. The Snowbridge
File Browser fork serves both from ``/static/`` when ``FB_BRANDING_FILES`` points at
that directory. ``custom.css`` carries every portfolio theme plus a mapping from
File Browser's own variables to the portfolio tokens; ``theme.js`` is the portfolio
theme script plus a small floating rotator button.

Theme sources, first match wins:

1. ``--themes-dir`` / ``SNOWBRIDGE_THEMES_DIR``
2. ``~/.config/traction-control/themes/rendered`` (what clockwork's Themes page edits)
3. the vendored copy in a sibling ``traction-control/themes`` checkout
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDERED_DIR = Path.home() / ".config/traction-control/themes/rendered"
SIBLING_DIR = REPO_ROOT.parent / "traction-control" / "themes"
ENV_THEMES_DIR = "SNOWBRIDGE_THEMES_DIR"

# (published name, rendered name, vendored name)
CSS_NAMES = ("theme.css", "portfolio-theme.css")
JS_NAMES = ("theme.js", "portfolio-theme.js")

# File Browser variable -> portfolio token. ``html:root:root`` outranks File
# Browser's ``:root.dark`` rule, so the mapping wins in every theme and also
# before the script has chosen one.
MAPPING = """
html:root:root {
  --blue: var(--accent);
  --dark-blue: var(--accent);
  --dark-red: var(--red);
  --moon-grey: var(--surface2);
  --icon-red: var(--red);
  --icon-yellow: var(--yellow);
  --icon-green: var(--green);
  --icon-blue: var(--accent);
  --icon-violet: var(--purple);
  --input-red: var(--red-dim);
  --input-green: var(--green-dim);
  --item-selected: var(--accent-ink);
  --action: var(--text);
  --background: var(--bg);
  --surfacePrimary: var(--surface);
  --surfaceSecondary: var(--surface2);
  --divider: var(--border);
  --iconPrimary: var(--accent);
  --iconSecondary: var(--text);
  --iconTertiary: var(--muted);
  --textPrimary: var(--muted);
  --textSecondary: var(--text);
  --hover: var(--accent-dim);
  --borderPrimary: var(--border);
  --borderSecondary: var(--border);
  --dividerPrimary: var(--border);
  --dividerSecondary: var(--border);
}
body { font-family: var(--font, inherit); }
#snowbridge-theme-cycle {
  position: fixed;
  right: 1em;
  bottom: 1em;
  z-index: 9999;
  padding: 0.4em 0.9em;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 0.85em;
  cursor: pointer;
  box-shadow: 0 0 0 3px var(--stripe, transparent);
}
#snowbridge-theme-cycle:hover { background: var(--surface2); }
#snowbridge-theme-cycle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
"""

# Added after the portfolio script. The button is created in plain DOM because
# the File Browser frontend is a compiled Vue app that is not edited for this.
BUTTON_JS = """
;(function () {
  function add() {
    if (document.getElementById("snowbridge-theme-cycle")) return;
    var button = document.createElement("button");
    button.id = "snowbridge-theme-cycle";
    button.type = "button";
    button.setAttribute("data-theme-cycle", "");
    document.body.appendChild(button);
    if (window.PortfolioTheme) window.PortfolioTheme.bind(button);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", add);
  else add();
})();
"""


class ThemeSourceError(Exception):
    """No usable portfolio theme files were found."""


def _first_file(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def find_sources(themes_dir: Path | None) -> tuple[Path, Path]:
    """Return (css, js) from the first candidate directory that has both."""
    candidates: list[Path] = []
    if themes_dir is not None:
        candidates.append(themes_dir)
    elif os.environ.get(ENV_THEMES_DIR):
        candidates.append(Path(os.environ[ENV_THEMES_DIR]))
    if themes_dir is None and not os.environ.get(ENV_THEMES_DIR):
        candidates.extend([RENDERED_DIR, SIBLING_DIR])
    for directory in candidates:
        css = _first_file(directory, CSS_NAMES)
        js = _first_file(directory, JS_NAMES)
        if css and js:
            return css, js
    searched = ", ".join(str(path) for path in candidates)
    raise ThemeSourceError(f"no portfolio theme.css/theme.js found in: {searched}")


def build_css(theme_css: str) -> str:
    return f"{theme_css.rstrip()}\n\n/* File Browser variables mapped to the portfolio tokens (snowbridge). */{MAPPING}"


def build_js(theme_js: str) -> str:
    return f"{theme_js.rstrip()}\n{BUTTON_JS}"


def _write_atomic(path: Path, text: str) -> None:
    descriptor, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def render(out_dir: Path, themes_dir: Path | None = None) -> list[Path]:
    css_source, js_source = find_sources(themes_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in (
        ("custom.css", build_css(css_source.read_text(encoding="utf-8"))),
        ("theme.js", build_js(js_source.read_text(encoding="utf-8"))),
    ):
        target = out_dir / name
        _write_atomic(target, text)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="branding directory (the File Browser config dir's branding/ subfolder)",
    )
    parser.add_argument("--themes-dir", type=Path, help="directory holding theme.css and theme.js")
    args = parser.parse_args(argv)
    try:
        written = render(args.out, args.themes_dir)
    except (ThemeSourceError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
