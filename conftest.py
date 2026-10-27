"""Keep test-module imports from changing process-wide logging for later tests."""

import logging

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_collection(session):
    previous_disable = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(previous_disable)
