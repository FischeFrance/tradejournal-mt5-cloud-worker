import os
import sys

import pytest

REPOSITORY_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPOSITORY_ROOT)

# A few pure-domain tests import the shared worker modules by their historical top-level names.
WORKER_DIR = os.path.join(REPOSITORY_ROOT, "worker")
sys.path.insert(0, WORKER_DIR)

WINDOWS_TEST_ROOT = os.path.join(REPOSITORY_ROOT, "tests", "windows")


def pytest_collection_modifyitems(config, items):
    del config
    windows_skip = pytest.mark.skip(
        reason="Windows test suite runs in the dedicated Windows CI job."
    )
    for item in items:
        source = os.path.abspath(str(item.path))
        if source.startswith(WINDOWS_TEST_ROOT + os.sep):
            item.add_marker(pytest.mark.windows)
            if os.name != "nt":
                item.add_marker(windows_skip)
