"""Check dashboard-only service handlers with the installed AstrBot dependencies."""

import asyncio
import json
import types
from unittest.mock import AsyncMock

from test_support import load_module, load_plugin


def main():
    module = load_plugin()
    settings = load_module("settings")
    dashboard = load_module("dashboard")
    library = load_module("singing.library")
    task_state = load_module("task_state")
    tts_provider = load_module("tts_provider")

    def set_request(value):
        module.request = value
        for owner in (dashboard, library, task_state):
            owner.request = value

    from astrbot.core.provider.register import llm_tools

    names = {tool.name for tool in llm_tools.func_list}
    expected = {
        "set_voice_clone",
        "set_voice_design",
        "speak",
        "learn_song",
        "list_learned_songs",
        "sing_song",
        "sing_song_clip",
        "delete_song",
    }
    assert names & expected == expected
    assert all(
        (
            tool.handler.__module__ == module.__name__
            for tool in llm_tools.func_list
            if tool.name in expected
        )
    )
    routes = []
    context = types.SimpleNamespace(
        register_web_api=lambda *args: routes.append(args), get_all_providers=lambda: []
    )

    class Job:
        pass

    module.WindowsProcessJob = Job
    context._breeze_service_job = Job()
    plugin = module.BreezeTTSPlugin(context)
    first_job = context._breeze_service_jobs[0]
    assert first_job is context._breeze_service_job
    fallback_job = Job()
    context._breeze_service_jobs.append(fallback_job)
    reloaded = module.BreezeTTSPlugin(context)
    assert reloaded._cover_service.job is first_job
    assert reloaded._tts_service.job is first_job
    assert reloaded._cover_service.jobs is context._breeze_service_jobs
    assert reloaded._tts_service.jobs[-1] is fallback_job
    routes.clear()
    plugin = module.BreezeTTSPlugin(context)
    assert {(r[0], tuple(r[2])) for r in routes} == {
        ("/astrbot_plugin_breeze_tts/cover/status", ("GET",)),
        ("/astrbot_plugin_breeze_tts/cover/start", ("POST",)),
        ("/astrbot_plugin_breeze_tts/tts/status", ("GET",)),
        ("/astrbot_plugin_breeze_tts/tts/start", ("POST",)),
        ("/astrbot_plugin_breeze_tts/tts/stop", ("POST",)),
        ("/astrbot_plugin_breeze_tts/cover/stop", ("POST",)),
        ("/astrbot_plugin_breeze_tts/tts/install", ("POST",)),
        ("/astrbot_plugin_breeze_tts/tts/install/status", ("GET",)),
        ("/astrbot_plugin_breeze_tts/tts/install/cancel", ("POST",)),
        ("/astrbot_plugin_breeze_tts/cover/install", ("POST",)),
        ("/astrbot_plugin_breeze_tts/cover/install/status", ("GET",)),
        ("/astrbot_plugin_breeze_tts/cover/install/cancel", ("POST",)),
        ("/astrbot_plugin_breeze_tts/config", ("GET",)),
        ("/astrbot_plugin_breeze_tts/config", ("POST",)),
        ("/astrbot_plugin_breeze_tts/task/status", ("GET",)),
        ("/astrbot_plugin_breeze_tts/library/songs", ("GET",)),
        ("/astrbot_plugin_breeze_tts/library/upload/prepare", ("POST",)),
        ("/astrbot_plugin_breeze_tts/library/upload/<upload_id>", ("POST",)),
        ("/astrbot_plugin_breeze_tts/library/delete", ("POST",)),
    }
    settings._load_plugin_config = lambda: {}
    calls = []

    class Controller:
        async def status(self, *, api_base):
            calls.append(("status", api_base))
            return {"running": True, "ready": True, "message": "ready"}

        async def start(self, *, api_base):
            calls.append(("start", api_base))
            return {"running": True, "ready": True, "message": "ready"}

        async def stop(self, *, api_base, can_stop):
            assert can_stop()
            calls.append(("stop", api_base))
            return {"running": False, "ready": False, "message": "stopped"}

    plugin._cover_service = Controller()
    plugin._tts_service = Controller()

    async def check():
        for username in (None, dashboard.API_KEY_USERNAME_PREFIX + "test"):
            set_request(types.SimpleNamespace(username=username))
            assert (await plugin.cover_status_web()).status_code == 403
            assert (await plugin.cover_start_web()).status_code == 403
            assert (await plugin.tts_status_web()).status_code == 403
            assert (await plugin.tts_start_web()).status_code == 403
            assert (await plugin.tts_install_cancel_web()).status_code == 403
            assert (await plugin.cover_install_cancel_web()).status_code == 403
            assert (await plugin.config_get_web()).status_code == 403
            assert (await plugin.config_save_web()).status_code == 403
            assert (await plugin.task_status_web()).status_code == 403
            assert (await plugin.library_songs_web()).status_code == 403
            assert (await plugin.library_upload_prepare_web()).status_code == 403
            assert (await plugin.library_upload_web("a" * 32)).status_code == 403
            assert (await plugin.library_delete_web()).status_code == 403
        assert not calls
        set_request(types.SimpleNamespace(username="dashboard-user"))
        waiting = asyncio.Event()

        async def install_fixture():
            try:
                await waiting.wait()
            finally:
                plugin._installer.states["tts"].update(active=False, state="cancelled")

        plugin._installer.begin("tts")
        plugin._song_task = asyncio.create_task(install_fixture())
        await asyncio.sleep(0)
        await plugin.cover_install_cancel_web()
        assert not plugin._song_task.done(), (
            "another service cannot cancel the active installer"
        )
        await plugin.tts_install_cancel_web()
        assert plugin._song_task.cancelled() and not plugin._installer.busy
        plugin._installer.begin("tts")
        plugin._song_task = asyncio.create_task(install_fixture())
        await plugin.tts_install_cancel_web()
        assert plugin._song_task.cancelled() and not plugin._installer.busy
        plugin._song_task = None
        set_request(
            types.SimpleNamespace(
                username="dashboard-user",
                body=AsyncMock(
                    return_value=b'{"sources":{"python":"https://wrong.invalid"}}'
                ),
            )
        )
        assert (await plugin.tts_install_web()).status_code == 400
        assert plugin._song_task is None
        chosen = {"python": "aliyun", "cuda": "official", "model": "mirror"}
        set_request(
            types.SimpleNamespace(
                username="dashboard-user",
                body=AsyncMock(return_value=json.dumps({"sources": chosen}).encode()),
            )
        )
        original_run = plugin._installer.run

        async def selected_install_fixture(kind):
            plugin._installer.states[kind]["active"] = False

        plugin._installer.run = selected_install_fixture
        response = await plugin.tts_install_web()
        assert response.status_code == 200
        assert json.loads(response.body)["sources"] == chosen
        await plugin._song_task
        plugin._installer.run = original_run
        plugin._song_task = None
        set_request(types.SimpleNamespace(username="dashboard-user"))
        assert (await plugin.library_upload_web("a" * 32)).status_code == 400
        assert "管理员" in await plugin.delete_song_tool(
            types.SimpleNamespace(is_admin=lambda: False), "Selected song"
        )
        upload_id = "b" * 32
        loop = asyncio.get_running_loop()
        draft = {"username": "dashboard-user", "expires": loop.time() + 60}
        plugin._upload_drafts[upload_id] = draft
        set_request(types.SimpleNamespace(username="another-user"))
        assert (await plugin.library_upload_web(upload_id)).status_code == 400
        assert plugin._upload_drafts[upload_id] is draft
        set_request(types.SimpleNamespace(username="dashboard-user"))
        draft["expires"] = loop.time() - 1
        assert (await plugin.library_upload_web(upload_id)).status_code == 400
        assert plugin._song_task is None

        draft["expires"] = loop.time() + 60
        plugin._song_task = loop.create_future()
        assert (await plugin.library_upload_prepare_web()).status_code == 409
        assert (await plugin.library_upload_web(upload_id)).status_code == 409
        delete_body = json.dumps({"id": "a" * 32, "name": "Selected song"}).encode()
        set_request(
            types.SimpleNamespace(
                username="dashboard-user", body=AsyncMock(return_value=delete_body)
            )
        )
        original_delete = plugin._delete_library_song
        plugin._delete_library_song = AsyncMock(
            side_effect=AssertionError("An active task must block deletion")
        )
        assert (await plugin.library_delete_web()).status_code == 409
        plugin._delete_library_song.assert_not_awaited()
        plugin._delete_library_song = original_delete
        plugin._song_task.cancel()
        plugin._song_task = None
        plugin._upload_drafts.clear()
        set_request(types.SimpleNamespace(username="dashboard-user"))
        progress = json.loads((await plugin.task_status_web()).body)
        assert progress["state"] == "idle" and (not progress["active"])
        plugin._task_begin("Example song", "clip")
        plugin._task_update(
            "sending",
            "正在发送语音",
            progress=0.5,
            sent_parts=1,
            total_parts=2,
            delivery="voice",
        )
        progress = json.loads((await plugin.task_status_web()).body)
        assert progress["active"] and progress["state"] == "running"
        assert (
            progress["sent_parts"] == 1
            and progress["total_parts"] == 2
            and (progress["progress"] == 0.5)
        )
        assert "_started_mono" not in progress and progress["elapsed_seconds"] >= 0
        plugin._task_finish("failed", "发送未完成", error="private technical detail")
        progress = json.loads((await plugin.task_status_web()).body)
        assert not progress["active"] and progress["state"] == "failed"
        assert progress["sent_parts"] == 1 and progress["progress"] is None
        assert progress["error"] == "private technical detail"
        assert (
            json.loads((await plugin.task_status_web()).body)["elapsed_seconds"]
            == progress["elapsed_seconds"]
        )
        status = await plugin.cover_status_web()
        assert status.status_code == 200
        assert json.loads(status.body)["task_running"] is False
        assert (await plugin.cover_start_web()).status_code == 200
        assert calls == [
            ("status", "http://127.0.0.1:7861"),
            ("start", "http://127.0.0.1:7861"),
        ]
        assert (await plugin.tts_status_web()).status_code == 200
        assert (await plugin.tts_start_web()).status_code == 200
        assert calls[-2:] == [
            ("status", "http://127.0.0.1:7860"),
            ("start", "http://127.0.0.1:7860"),
        ]
        count = len(calls)
        plugin._set_cover_busy(True)
        assert json.loads((await plugin.tts_status_web()).body)["task_running"] is True
        assert (await plugin.tts_start_web()).status_code == 409
        assert len(calls) == count
        plugin._set_cover_busy(False)
        plugin._song_task = asyncio.get_running_loop().create_future()
        assert (await plugin.cover_start_web()).status_code == 409
        assert (
            json.loads((await plugin.cover_status_web()).body)["task_running"] is True
        )
        plugin._song_task.cancel()
        settings._load_plugin_config = lambda: {"singing": {"enabled": False}}
        assert (await plugin.cover_start_web()).status_code == 400
        disabled = json.loads((await plugin.cover_status_web()).body)
        assert disabled["enabled"] is False and disabled["running"] is True
        assert "仍可手动关闭" in disabled["message"]
        assert (await plugin.cover_stop_web()).status_code == 200
        current = {
            "instruction": "existing value",
            "singing": {"enabled": True, "legacy_option": "preserved"},
        }
        saves = []
        settings._load_plugin_config = lambda: json.loads(json.dumps(current))

        def save_config(cfg, live):
            saves.append(cfg)
            current.clear()
            current.update(json.loads(json.dumps(cfg)))

        settings._save_plugin_config = save_config
        set_request(types.SimpleNamespace(username="dashboard-user"))
        initial = json.loads((await plugin.config_get_web()).body)
        assert initial["config"]["instruction"] == "existing value"
        assert initial["config"]["seed"] == 42 and len(initial["revision"]) == 64
        assert not saves and "seed" not in current
        changed = {**initial["config"], "api_base": "https://custom.example/tts///"}

        def post_request(cfg, revision):
            body = json.dumps({"config": cfg, "revision": revision}).encode()
            return types.SimpleNamespace(
                username="dashboard-user",
                headers={"content-length": str(len(body))},
                body=AsyncMock(return_value=body),
            )

        set_request(post_request(changed, initial["revision"]))
        response = await plugin.config_save_web()
        assert response.status_code == 200
        saved = json.loads(response.body)
        assert saved["config"]["api_base"] == "https://custom.example/tts"
        assert current["singing"]["legacy_option"] == "preserved"
        assert len(saves) == 1
        set_request(post_request(changed, initial["revision"]))
        assert (await plugin.config_save_web()).status_code == 409
        assert len(saves) == 1
        set_request(post_request(saved["config"], saved["revision"]))
        old_body = module.request.body.return_value

        async def body_with_concurrent_change():
            current["seed"] = 43
            return old_body

        module.request.body = body_with_concurrent_change
        assert (await plugin.config_save_web()).status_code == 409
        assert len(saves) == 1
        set_request(types.SimpleNamespace(username="dashboard-user"))
        fresh = json.loads((await plugin.config_get_web()).body)
        plugin._song_task = asyncio.get_running_loop().create_future()
        set_request(post_request(fresh["config"], fresh["revision"]))
        assert (await plugin.config_save_web()).status_code == 409
        plugin._song_task.cancel()
        assert len(saves) == 1
        set_request(
            types.SimpleNamespace(
                username="dashboard-user",
                headers={"content-length": str(settings.MAX_CONFIG_BODY_BYTES + 1)},
                body=AsyncMock(),
            )
        )
        assert (await plugin.config_save_web()).status_code == 413
        module.request.body.assert_not_awaited()

        def fail_sync(*args):
            raise RuntimeError("old provider instance")

        context.get_all_providers = lambda: [
            types.SimpleNamespace(provider_config={"type": "breeze_tts"})
        ]
        tts_provider._sync_provider_from_config = fail_sync
        set_request(post_request(fresh["config"], fresh["revision"]))
        assert (await plugin.config_save_web()).status_code == 200
        assert len(saves) == 2
        await plugin.terminate()

    asyncio.run(check())
    print(
        "PASS: dashboard route registration, authorization, task guard and configuration"
    )


if __name__ == "__main__":
    main()
