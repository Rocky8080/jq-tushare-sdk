#!/usr/bin/env python3
"""Bump project version files and prepend a changelog entry."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
INIT_FILE = ROOT / "jq_tushare_sdk" / "__init__.py"
PYPROJECT_FILE = ROOT / "pyproject.toml"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"
README_FILE = ROOT / "README.md"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    current = _read_current_version()
    next_version = _resolve_next_version(current, args.part)

    if not args.yes:
        print(f"Current version: {current}")
        print(f"Next version:    {next_version}")

    _write_version_file(next_version)
    _replace_package_version(next_version)
    _replace_pyproject_version(next_version)
    _replace_readme_version(next_version)
    _prepend_changelog_entry(next_version, args.message, args.date)

    print(f"Bumped JQ Tushare SDK to {next_version}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bump JQ Tushare SDK version metadata.",
    )
    parser.add_argument(
        "part",
        choices=("major", "minor", "patch"),
        help="Semantic Versioning part to bump.",
    )
    parser.add_argument(
        "-m",
        "--message",
        default="Version update.",
        help="Changelog bullet for this version.",
    )
    parser.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="Release date in YYYY-MM-DD format. Defaults to today.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Run without the preview prompt text.",
    )
    return parser


def _read_current_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not SEMVER_RE.match(version):
        raise SystemExit(f"Invalid VERSION value: {version!r}")
    return version


def _resolve_next_version(current: str, part: str) -> str:
    major, minor, patch = [int(value) for value in current.split(".")]
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _write_version_file(version: str) -> None:
    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8")


def _replace_package_version(version: str) -> None:
    text = INIT_FILE.read_text(encoding="utf-8")
    updated = re.sub(
        r'__version__ = "[^"]+"',
        f'__version__ = "{version}"',
        text,
        count=1,
    )
    if text == updated:
        raise SystemExit(f"Could not find __version__ in {INIT_FILE}")
    INIT_FILE.write_text(updated, encoding="utf-8")


def _replace_pyproject_version(version: str) -> None:
    text = PYPROJECT_FILE.read_text(encoding="utf-8")
    updated = re.sub(
        r'(?m)^version = "[^"]+"$',
        f'version = "{version}"',
        text,
        count=1,
    )
    if text == updated:
        raise SystemExit(f"Could not find project version in {PYPROJECT_FILE}")
    PYPROJECT_FILE.write_text(updated, encoding="utf-8")


def _replace_readme_version(version: str) -> None:
    text = README_FILE.read_text(encoding="utf-8")
    updated = re.sub(
        r"当前版本：`v[^`]+`",
        f"当前版本：`v{version}`",
        text,
        count=1,
    )
    if text == updated:
        raise SystemExit(f"Could not find current version in {README_FILE}")
    README_FILE.write_text(updated, encoding="utf-8")


def _prepend_changelog_entry(version: str, message: str, date_text: str) -> None:
    text = CHANGELOG_FILE.read_text(encoding="utf-8")
    marker = "\n## ["
    if marker not in text:
        raise SystemExit("Could not find changelog insertion point")

    entry = (
        f"\n## [{version}] - {date_text}\n\n"
        "### Changed\n\n"
        f"- {message.strip()}\n"
    )
    updated = text.replace(marker, entry + marker, 1)
    CHANGELOG_FILE.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
