import sys

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if sys.platform == "linux":
        return
    skip = pytest.mark.skip(
        reason="needs Linux symlinks/inotify: docker compose -f compose.test.yaml run --rm test"
    )
    for item in items:
        if "linux" in item.keywords:
            item.add_marker(skip)
