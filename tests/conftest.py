# -*- coding: utf-8 -*-
import pytest
from core import store


@pytest.fixture(autouse=True)
def _close_store_conns():
    yield
    store.close_all()
