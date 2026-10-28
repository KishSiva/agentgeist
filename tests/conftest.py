import socket

import pytest


@pytest.fixture(autouse=True)
def block_unmocked_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Unit tests must mock network requests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
