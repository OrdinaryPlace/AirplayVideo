"""Host-network startup must coexist with other apps and fail closed."""
import contextlib
import shutil
import socket
from unittest.mock import AsyncMock, Mock
import aiohttp
import pytest
from service import network
from service.controller import PairingEngine
from service.model import UserError


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [0, -1, 65536, True, "8099", None])
async def test_ingress_rejects_invalid_supervisor_port(monkeypatch, port):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-only")
    monkeypatch.delenv("AIRPLAYVIDEO_STANDALONE", raising=False)
    response = AsyncMock(status=200)
    response.json.return_value = {"result": "ok", "data": {"ingress_port": port}}
    session = AsyncMock()
    session.get = Mock(return_value=contextlib.nullcontext(response))
    monkeypatch.setattr(network.aiohttp, "ClientSession", Mock(return_value=contextlib.nullcontext(session)))
    with pytest.raises(UserError):
        await network.ingress_listener()


@pytest.mark.asyncio
async def test_ingress_uses_supervisor_port_on_internal_interface(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-only")
    monkeypatch.delenv("AIRPLAYVIDEO_STANDALONE", raising=False)
    response = AsyncMock(status=200)
    response.json.return_value = {"result": "ok", "data": {"ingress_port": 45678}}
    session = AsyncMock()
    session.get = Mock(return_value=contextlib.nullcontext(response))
    monkeypatch.setattr(network.aiohttp, "ClientSession", Mock(return_value=contextlib.nullcontext(session)))
    assert await network.ingress_listener() == ("172.30.32.1", 45678)
    session.get.assert_called_once_with("http://supervisor/addons/self/info", headers={"Authorization": "Bearer test-only"})


@pytest.mark.asyncio
async def test_ingress_without_supervisor_never_falls_back_to_lan(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("AIRPLAYVIDEO_STANDALONE", raising=False)
    with pytest.raises(UserError):
        await network.ingress_listener()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("airplayvideo-engine"), reason="Run in the Linux app image")
async def test_two_real_engines_coexist_with_old_control_port_occupied(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 8098))
        occupied.listen()
        async with aiohttp.ClientSession() as session:
            engines = [PairingEngine(tmp_path / name, session) for name in ("first", "second")]
            try:
                for engine in engines:
                    engine.root.mkdir(mode=0o700)
                    await engine.start()
                    assert await engine.call("/receivers") == []
                assert len({8098, *(engine.port for engine in engines)}) == 3
            finally:
                for engine in engines:
                    await engine.close()
