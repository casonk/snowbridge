from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "render_filebrowser_theme",
    Path(__file__).parents[1] / "scripts/render_filebrowser_theme.py",
)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class RenderFileBrowserThemeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.themes = self.root / "themes"
        self.themes.mkdir()
        (self.themes / "theme.css").write_text(":root { --bg: #000000; }\n", encoding="utf-8")
        (self.themes / "theme.js").write_text("window.PortfolioTheme = {};\n", encoding="utf-8")

    def test_render_writes_css_and_js(self) -> None:
        out = self.root / "branding"
        written = module.render(out, self.themes)
        self.assertEqual({p.name for p in written}, {"custom.css", "theme.js"})
        css = (out / "custom.css").read_text(encoding="utf-8")
        self.assertIn("--bg: #000000", css)
        self.assertIn("--background: var(--bg)", css)
        self.assertIn("html:root:root", css)
        js = (out / "theme.js").read_text(encoding="utf-8")
        self.assertIn("window.PortfolioTheme", js)
        self.assertIn("snowbridge-theme-cycle", js)

    def test_vendored_file_names_are_accepted(self) -> None:
        vendored = self.root / "vendored"
        vendored.mkdir()
        (vendored / "portfolio-theme.css").write_text("a{}", encoding="utf-8")
        (vendored / "portfolio-theme.js").write_text("b", encoding="utf-8")
        css, js = module.find_sources(vendored)
        self.assertEqual((css.name, js.name), ("portfolio-theme.css", "portfolio-theme.js"))

    def test_missing_sources_fail(self) -> None:
        with self.assertRaises(module.ThemeSourceError):
            module.find_sources(self.root / "nowhere")

    def test_env_dir_is_used_and_skips_defaults(self) -> None:
        with patch.dict("os.environ", {module.ENV_THEMES_DIR: str(self.themes)}):
            css, _ = module.find_sources(None)
        self.assertEqual(css.parent, self.themes)

    def test_main_reports_error_without_sources(self) -> None:
        code = module.main(["--out", str(self.root / "o"), "--themes-dir", str(self.root / "none")])
        self.assertEqual(code, 1)

    def test_rerender_replaces_files(self) -> None:
        out = self.root / "branding"
        module.render(out, self.themes)
        (self.themes / "theme.css").write_text(":root { --bg: #ffffff; }\n", encoding="utf-8")
        module.render(out, self.themes)
        self.assertIn("#ffffff", (out / "custom.css").read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["custom.css", "theme.js"])


if __name__ == "__main__":
    unittest.main()
