import pytest


def pytest_collection_modifyitems(items):
    """Ensure the interval UI test can consume its async database fixture."""
    for item in items:
        if item.name == "test_timeline_template_is_dependency_free":
            item.add_marker(pytest.mark.anyio)
