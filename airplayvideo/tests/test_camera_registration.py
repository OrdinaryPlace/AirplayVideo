"""Generic camera API plumbing; encoded/native HA validation is separate."""
import json
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
import pytest_asyncio

from service.camera_registration import CameraRegistration, ENTITY_ID, TITLE
from service.model import Store, private_json


STREAM = "http://172.30.32.1:12345/private-bearer/stream.m3u8"
SNAPSHOT = "http://172.30.32.1:12345/private-bearer/snapshot.png"


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-supervisor-token")
    state = SimpleNamespace(entries=[], entities=[], commands=[], requests=[],
                            created=0, updated=0, fail_validation=False,
                            fail_confirmation=False, disabled=False, taken=False)

    async def api(request):
        assert request.headers["Authorization"] == "Bearer private-supervisor-token"
        path, method = request.path, request.method
        payload = await request.json() if method == "POST" else None
        state.requests.append((method, path, payload))
        if path == "/api/config/config_entries/entry":
            return web.json_response(state.entries)
        if method == "DELETE":
            return web.json_response({})
        if path.endswith("/options/flow"):
            assert payload["handler"] == "fixture-owned-camera"
            return web.json_response({"type": "form", "step_id": "init", "flow_id": "options-flow"})
        if path.endswith("/flow"):
            assert payload == {"handler": "generic"}
            return web.json_response({"type": "form", "step_id": "user", "flow_id": "camera-flow"})
        if "stream_source" in payload:
            assert payload["advanced"]["framerate"] == 2
            assert payload["still_image_url"].endswith("/snapshot.png")
            return web.json_response({"type": "form", "step_id": "user_confirm",
                                      "flow_id": path.rsplit("/", 1)[1],
                                      "errors": {"stream_source": STREAM} if state.fail_validation else {}})
        assert payload == {"confirmed_ok": True}
        if state.fail_confirmation:
            return web.Response(status=502, text=STREAM)
        if "options/flow" in path:
            state.updated += 1
            return web.json_response({"type": "create_entry", "data": {}})
        state.created += 1
        state.entries.append({"entry_id": "fixture-owned-camera", "domain": "generic", "disabled_by": None})
        state.entities.append({"entity_id": "camera.172_30_32_1", "config_entry_id": "fixture-owned-camera",
                               "device_id": "fixture-device"})
        return web.json_response({"type": "create_entry", "result": {"entry_id": "fixture-owned-camera"}})

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        assert await ws.receive_json() == {"type": "auth", "access_token": "private-supervisor-token"}
        await ws.send_json({"type": "auth_ok"})
        async for message in ws:
            command = json.loads(message.data)
            state.commands.append(command)
            result = {}
            if command["type"] == "config/entity_registry/list":
                result = state.entities
            elif command["type"] == "config/entity_registry/update":
                entity = next(item for item in state.entities if item["entity_id"] == command["entity_id"])
                entity.update({"entity_id": command.get("new_entity_id", entity["entity_id"]), "name": command["name"]})
                result = {"entity_entry": entity}
            elif command["type"] == "config_entries/update":
                assert command["entry_id"] == "fixture-owned-camera" and command["title"] == TITLE
            else:
                assert command["type"] == "config/device_registry/update"
                assert command["device_id"] == "fixture-device"
            await ws.send_json({"type": "result", "id": command["id"], "success": True, "result": result})
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", websocket)
    app.router.add_route("*", "/api/config/{path:.*}", api)
    store = Store(tmp_path)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        registration = CameraRegistration(store, session, core_url=str(server.make_url("/")))
        yield SimpleNamespace(state=state, registration=registration, store=store, session=session, server=server)


@pytest.mark.asyncio
async def test_creates_native_stream_camera_once_and_preserves_customizations(core):
    x = core
    assert await x.registration.ensure(STREAM, SNAPSHOT) == {"status": "ready", "entity_id": ENTITY_ID, "error": ""}
    assert x.state.created == 1
    assert not any("mqtt" in path for _, path, _ in x.state.requests)
    saved = private_json(x.registration.path)
    assert saved["entry_id"] == "fixture-owned-camera" and saved["named"]
    assert STREAM not in json.dumps(saved) and "private-supervisor-token" not in json.dumps(saved)
    x.state.entities[0]["entity_id"] = "camera.custom_video"
    x.state.commands.clear()
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["entity_id"] == "camera.custom_video"
    assert x.state.created == 1 and x.state.updated == 0
    assert not any(command["type"] == "config/entity_registry/update" for command in x.state.commands)


@pytest.mark.asyncio
async def test_endpoint_change_updates_only_owned_camera(core):
    x = core
    await x.registration.ensure(STREAM, SNAPSHOT)
    x.state.entries.append({"entry_id": "unrelated-camera", "domain": "generic"})
    result = await x.registration.ensure(STREAM.replace("12345", "23456"), SNAPSHOT.replace("12345", "23456"))
    assert result["status"] == "ready" and x.state.created == 1 and x.state.updated == 1
    assert x.state.entries[-1]["entry_id"] == "unrelated-camera"


@pytest.mark.asyncio
async def test_disabled_camera_is_not_enabled_or_replaced(core):
    x = core
    await x.registration.ensure(STREAM, SNAPSHOT)
    x.state.entries[0]["disabled_by"] = "user"
    x.state.requests.clear()
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["status"] == "disabled"
    assert x.state.created == 1 and x.state.updated == 0
    assert all(method == "GET" for method, _, _ in x.state.requests)


@pytest.mark.asyncio
async def test_failed_validation_is_sanitized_and_releases_preview(core):
    x = core
    x.state.fail_validation = True
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result["status"] == "pending" and x.state.created == 0
    assert "private-bearer" not in json.dumps(result)
    assert x.state.requests[-1][:2] == ("DELETE", "/api/config/config_entries/flow/camera-flow")
    assert not private_json(x.registration.path).get("uncertain_creation")


@pytest.mark.asyncio
async def test_uncertain_confirmation_prevents_duplicate_creation(core):
    x = core
    x.state.fail_confirmation = True
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result["status"] == "pending" and "private-bearer" not in json.dumps(result)
    assert private_json(x.registration.path)["uncertain_creation"] is True
    x.state.requests.clear()
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result["status"] == "pending" and "duplicate" in result["error"]
    assert not x.state.requests


@pytest.mark.asyncio
async def test_does_not_take_existing_entity_id(core):
    x = core
    x.state.entities.append({"entity_id": ENTITY_ID, "config_entry_id": "unrelated-camera"})
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result["status"] == "ready" and result["entity_id"] == "camera.172_30_32_1"
    assert x.state.entities[0] == {"entity_id": ENTITY_ID, "config_entry_id": "unrelated-camera"}


@pytest.mark.asyncio
async def test_cannot_reconfigure_an_unrelated_domain_using_saved_id(core):
    x = core
    x.registration.save(entry_id="fixture-owned-camera")
    x.state.entries.append({"entry_id": "fixture-owned-camera", "domain": "unifiprotect"})
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result["status"] == "pending" and "preserved" in result["error"]
    assert x.state.created == 0
    assert all(method == "GET" for method, _, _ in x.state.requests)


@pytest.mark.asyncio
async def test_missing_supervisor_and_disabled_setup_do_not_call_core(core, monkeypatch):
    x = core
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["status"] == "unavailable"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-supervisor-token")
    x.store.data["setup"]["home_assistant"]["enabled"] = False
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["status"] == "disabled"
    assert not x.state.requests
