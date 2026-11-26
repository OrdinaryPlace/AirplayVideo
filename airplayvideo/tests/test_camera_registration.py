"""Generic camera API plumbing; encoded/native HA validation is separate."""
import asyncio
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
                            fail_confirmation=False, disabled=False, taken=False,
                            chunked_entries=False, drop_entity_update_reply=False)

    async def api(request):
        assert request.headers["Authorization"] == "Bearer private-supervisor-token"
        path, method = request.path, request.method
        payload = await request.json() if method == "POST" else None
        state.requests.append((method, path, payload))
        if path == "/api/config/config_entries/entry":
            if state.chunked_entries:
                response = web.StreamResponse(headers={"Content-Type": "application/json"})
                await response.prepare(request)
                data = json.dumps(state.entries).encode()
                for offset in range(0, len(data), 16 * 1024):
                    await response.write(data[offset:offset + 16 * 1024])
                    # Force an observable partial body before the next chunk.
                    await asyncio.sleep(0.001)
                await response.write_eof()
                return response
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
                if state.drop_entity_update_reply:
                    state.drop_entity_update_reply = False
                    await ws.close()
                    return ws
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
    assert saved["entry_id"] == "fixture-owned-camera" and saved["named"] and saved["naming_version"] == 1
    assert x.state.entities[0]["name"] == ""
    assert STREAM not in json.dumps(saved) and "private-supervisor-token" not in json.dumps(saved)
    x.state.entities[0]["entity_id"] = "camera.custom_video"
    x.state.commands.clear()
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["entity_id"] == "camera.custom_video"
    assert x.state.created == 1 and x.state.updated == 0
    assert not any(command["type"].endswith("/update") for command in x.state.commands)


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
@pytest.mark.parametrize("disabled_by", ["user", "device"])
async def test_disabled_entity_or_device_is_reported_and_never_renamed(core, disabled_by):
    x = core
    await x.registration.ensure(STREAM, SNAPSHOT)
    x.state.entities[0].update(disabled_by=disabled_by, entity_id="camera.custom_disabled_video", name="My video")
    # Even if first-time naming was interrupted, never rename a disabled entity.
    x.registration.save(named=False)
    x.state.commands.clear()
    x.state.requests.clear()
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result["status"] == "disabled" and result["entity_id"] == "camera.custom_disabled_video"
    assert x.state.entities[0]["disabled_by"] == disabled_by
    assert x.state.entities[0]["name"] == "My video"
    assert not any(command["type"].endswith("/update") for command in x.state.commands)
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


@pytest.mark.asyncio
async def test_large_chunked_core_inventory_is_read_to_eof_before_registration(core):
    x = core
    x.state.entries.extend({"entry_id": "unrelated-" + str(index), "domain": "fixture",
                            "title": "Unrelated integration " + str(index)} for index in range(2000))
    assert len(json.dumps(x.state.entries).encode()) > 64 * 1024
    x.state.chunked_entries = True
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result == {"status": "ready", "entity_id": ENTITY_ID, "error": ""}
    assert x.state.created == 1 and len(x.state.entries) == 2001
    # A later read must also find the owned entry beyond the early chunks.
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["status"] == "ready"
    assert x.state.created == 1


@pytest.mark.asyncio
async def test_oversized_core_response_is_rejected_without_creating_camera(core):
    x = core
    x.state.entries.append({"entry_id": "unrelated", "domain": "fixture", "title": "x" * (2 * 1024 * 1024)})
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result["status"] == "pending" and "oversized" in result["error"]
    assert x.state.created == 0 and x.state.updated == 0


@pytest.mark.asyncio
async def test_large_home_registry_over_two_megabytes_can_register_camera(core):
    x = core
    x.state.entities.extend({"entity_id": "sensor.unrelated_" + str(index),
                             "config_entry_id": "unrelated", "name": "x" * 1000} for index in range(2500))
    size = len(json.dumps(x.state.entities).encode())
    assert 2 * 1024 * 1024 < size < 8 * 1024 * 1024
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result == {"status": "ready", "entity_id": ENTITY_ID, "error": ""}
    assert x.state.created == 1 and len(x.state.entities) == 2501


async def legacy_registration(core, *, name=TITLE, entity_id=ENTITY_ID, disabled_by=None):
    await core.registration.ensure(STREAM, SNAPSHOT)
    core.state.entities[0].update(name=name, entity_id=entity_id, disabled_by=disabled_by)
    core.registration.saved.pop("naming_version")
    core.registration.save()
    core.state.commands.clear()
    core.state.requests.clear()


@pytest.mark.asyncio
async def test_legacy_automatic_duplicate_name_is_migrated_once_without_changing_identity(core):
    x = core
    await legacy_registration(x, entity_id="camera.my_selected_video")
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result == {"status": "ready", "entity_id": "camera.my_selected_video", "error": ""}
    updates = [command for command in x.state.commands if command["type"].endswith("/update")]
    assert len(updates) == 1
    assert {key: value for key, value in updates[0].items() if key != "id"} == {
        "type": "config/entity_registry/update", "entity_id": "camera.my_selected_video", "name": ""}
    assert x.state.entities[0]["name"] == "" and x.state.entities[0]["device_id"] == "fixture-device"
    assert private_json(x.registration.path)["naming_version"] == 1
    x.state.commands.clear()
    await x.registration.ensure(STREAM, SNAPSHOT)
    assert not any(command["type"].endswith("/update") for command in x.state.commands)
    assert x.state.created == 1 and x.state.updated == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["My custom camera", "", None])
async def test_legacy_custom_name_and_entity_id_are_preserved_and_migration_marked_complete(core, name):
    x = core
    await legacy_registration(x, name=name, entity_id="camera.my_custom_video")
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["entity_id"] == "camera.my_custom_video"
    assert x.state.entities[0]["name"] == name and x.state.entities[0]["device_id"] == "fixture-device"
    assert not any(command["type"].endswith("/update") for command in x.state.commands)
    assert private_json(x.registration.path)["naming_version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_by", ["user", "device"])
async def test_disabled_legacy_camera_is_not_migrated_or_marked_complete(core, disabled_by):
    x = core
    await legacy_registration(x, disabled_by=disabled_by)
    result = await x.registration.ensure(STREAM, SNAPSHOT)
    assert result["status"] == "disabled"
    assert x.state.entities[0]["name"] == TITLE and x.state.entities[0]["disabled_by"] == disabled_by
    assert not any(command["type"].endswith("/update") for command in x.state.commands)
    assert "naming_version" not in private_json(x.registration.path)


@pytest.mark.asyncio
async def test_legacy_migration_recovers_lost_ack_without_duplicate_update(core):
    x = core
    await legacy_registration(x)
    x.state.drop_entity_update_reply = True
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["status"] == "pending"
    assert x.state.entities[0]["name"] == ""  # HA accepted the update before disconnecting.
    assert "naming_version" not in private_json(x.registration.path)
    x.state.commands.clear()
    # Recreate the registrar to prove recovery depends on persisted identity,
    # rather than an in-memory successful-operation flag.
    x.registration = CameraRegistration(x.store, x.session, core_url=str(x.server.make_url("/")))
    assert (await x.registration.ensure(STREAM, SNAPSHOT))["status"] == "ready"
    assert not any(command["type"].endswith("/update") for command in x.state.commands)
    assert private_json(x.registration.path)["naming_version"] == 1
    assert x.state.created == 1


@pytest.mark.asyncio
async def test_concurrent_legacy_migration_requests_serialize_one_update(core):
    x = core
    await legacy_registration(x)
    results = await asyncio.gather(x.registration.ensure(STREAM, SNAPSHOT), x.registration.ensure(STREAM, SNAPSHOT))
    assert all(result["status"] == "ready" for result in results)
    assert sum(command["type"] == "config/entity_registry/update" for command in x.state.commands) == 1
    assert private_json(x.registration.path)["naming_version"] == 1
