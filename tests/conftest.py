import pytest

from korean_parser import SpacingFixer


@pytest.fixture(scope="session")
def fixer():
    return SpacingFixer()
