import pytest

import main


class FakeMimir:
    def __init__(self, connected=False):
        self.connected = connected
        self.starts = 0
        self.stops = 0

    @property
    def is_connected(self):
        return self.connected

    async def start(self):
        self.starts += 1
        self.connected = True

    async def stop(self):
        self.stops += 1
        self.connected = False


@pytest.mark.asyncio
async def test_health_reconnects_disconnected_vault(monkeypatch):
    fake = FakeMimir()
    monkeypatch.setattr(main, "mimir_client", fake)

    result = await main.health()

    assert result["status"] == "healthy"
    assert result["mimir"] == "connected"
    assert fake.stops == 1
    assert fake.starts == 1
