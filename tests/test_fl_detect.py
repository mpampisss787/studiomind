"""Tests for FL window title parsing."""

from studiomind.fl_detect import parse_fl_title


def test_parse_saved_project():
    assert parse_fl_title("FL Studio 2025 - Project_TEST") == "Project_TEST"


def test_parse_modified_project():
    # Trailing asterisk marks unsaved changes
    assert parse_fl_title("FL Studio 2025 - Project_TEST *") == "Project_TEST"
    assert parse_fl_title("FL Studio 2025 - Project_TEST*") == "Project_TEST"


def test_parse_no_project():
    # Blank FL, no project loaded — title is just the version
    assert parse_fl_title("FL Studio 2025") is None
    assert parse_fl_title("FL Studio 21") is None


def test_parse_project_with_spaces():
    assert parse_fl_title("FL Studio 2025 - My Track v3") == "My Track v3"


def test_parse_project_with_dashes():
    # Project name itself contains a dash — regex must still split on the first " - "
    assert parse_fl_title("FL Studio 2025 - trap-beat-final") == "trap-beat-final"


def test_parse_empty():
    assert parse_fl_title("") is None


def test_parse_unrelated_title():
    assert parse_fl_title("Notepad") is None
    assert parse_fl_title("") is None
