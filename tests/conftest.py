"""Shared configuration for local benchmark tests."""
import os
import pytest

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'


def pytest_addoption(parser):
    parser.addoption('--run-baselines', action='store_true',
                     help='Run native baseline integration tests with pinned upstream checkouts.')


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker('baselines') and not config.getoption('--run-baselines'):
            item.add_marker(pytest.mark.skip(reason='Use --run-baselines with the pinned baseline dependencies'))
