import re
import tomllib
import unittest
from pathlib import Path

import jq_tushare_sdk
from scripts.bump_version import _resolve_next_version


ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class VersioningTest(unittest.TestCase):
    def test_version_metadata_is_consistent(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertRegex(version, SEMVER_RE)
        self.assertEqual(version, jq_tushare_sdk.__version__)
        self.assertEqual(version, pyproject["project"]["version"])
        self.assertIn(f"当前版本：`v{version}`", readme)
        self.assertIn(f"## [{version}]", changelog)

    def test_semver_bump_rules(self):
        self.assertEqual("0.1.1", _resolve_next_version("0.1.0", "patch"))
        self.assertEqual("0.2.0", _resolve_next_version("0.1.0", "minor"))
        self.assertEqual("1.0.0", _resolve_next_version("0.1.0", "major"))


if __name__ == "__main__":
    unittest.main()
