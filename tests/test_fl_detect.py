"""Tests for FL window title parsing."""

from studiomind.fl_detect import parse_fl_title


# FL Studio 2025 — project-first, .flp extension in the title
def test_parse_fl2025_saved():
    assert parse_fl_title("Project_TEST.flp - FL Studio 2025") == "Project_TEST"


def test_parse_fl2025_modified():
    # FL 2025 puts the asterisk on the filename before the extension, not after
    assert parse_fl_title("Project_TEST.flp* - FL Studio 2025") == "Project_TEST"


def test_parse_fl2025_with_spaces():
    assert parse_fl_title("My Track v3.flp - FL Studio 2025") == "My Track v3"


def test_parse_fl2025_with_dashes_in_name():
    # Project name itself contains dashes — regex must split on " - FL Studio"
    assert parse_fl_title("trap-beat-final.flp - FL Studio 2025") == "trap-beat-final"


def test_parse_fl2025_no_project():
    # Blank FL, no project loaded — title is just the version
    assert parse_fl_title("FL Studio 2025") is None
    assert parse_fl_title("FL Studio 21") is None


# Older FL versions — version-first format (fallback support)
def test_parse_legacy_version_first():
    assert parse_fl_title("FL Studio 21 - Project_TEST") == "Project_TEST"


def test_parse_legacy_version_first_modified():
    assert parse_fl_title("FL Studio 21 - Project_TEST *") == "Project_TEST"


# Edge cases
def test_parse_empty():
    assert parse_fl_title("") is None


def test_parse_unrelated_title():
    assert parse_fl_title("Notepad") is None
    assert parse_fl_title("Chrome") is None


def test_parse_strips_flp_extension():
    assert parse_fl_title("beat.flp - FL Studio 2025") == "beat"
    # Case-insensitive match on extension
    assert parse_fl_title("beat.FLP - FL Studio 2025") == "beat"
