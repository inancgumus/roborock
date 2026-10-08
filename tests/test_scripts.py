#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["python-roborock", "keyring", "platformdirs", "pytest", "pytest-xdist"]
# ///
"""Runs every script in testdata/scripts against the real server and commands,
with fake Roborock servers that listen on real sockets. See testscript.py."""

import sys
from pathlib import Path

import pytest
from testscript import Script

SCRIPTS = sorted((Path(__file__).resolve().parent / "testdata" / "scripts").glob("*.txtar"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_script(script, tmp_path):
    Script(script, tmp_path).run()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-n", "auto", *sys.argv[1:]]))
