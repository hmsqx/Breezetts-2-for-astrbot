"""Authenticated dashboard configuration and local service controls."""

import asyncio
import json

import httpx
import psutil
from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

from . import settings as plugin_settings
from . import tts_provider
from .settings import MAX_CONFIG_BODY_BYTES


class DashboardMixin:
    """Use the plugin's service controllers, config and shared song-task slot."""

    async def _install_web(self, kind, *, start=False, cancel=False):
        """Expose local installation only to a logged-in dashboard user.

        Args:
            kind: The fixed tts or cover service key.
            start: Whether to queue an explicit installation request.
            cancel: Whether to stop this service's active installer.

        Returns:
            The current progress snapshot or an authentication/busy error.
        """
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        if cancel:
            if (
                self._installer.snapshot(kind)["active"]
                and self._song_task
                and not self._song_task.done()
            ):
                self._song_task.cancel()
                try:
                    await self._song_task
                except asyncio.CancelledError:
                    pass
                if self._installer.states[kind].get("active"):
                    self._installer.states[kind].update(
                        active=False,
                        state="cancelled",
                        stage="安装已停止",
                        progress=None,
                        error="安装已停止，已完成文件保留，可点击继续安装。",
                    )
            return json_response(self._installer.snapshot(kind))
        if not start:
            return json_response(self._installer.snapshot(kind))
        if (
            self._installer.busy
            or self._cover_state["busy"]
            or (self._song_task and not self._song_task.done())
        ):
            return error_response(
                "安装或歌曲任务正在进行，请完成后再安装。", status_code=409
            )
        from .services.service_control import validate_api_base

        try:
            cfg = plugin_settings._load_plugin_config()
            api_base = (
                cfg.get("api_base")
                if kind == "tts"
                else cfg.get("singing", {}).get("api_base")
            )
            validate_api_base(
                api_base or f"http://127.0.0.1:{7860 if kind == 'tts' else 7861}",
                7860 if kind == "tts" else 7861,
            )
        except (ValueError, AttributeError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        blockers = self._installer.snapshot(kind).get("blockers", [])
        if blockers:
            return error_response("；".join(blockers), status_code=400)
        from .services.installer import normalize_sources

        try:
            body = await request.body()
            if len(body) > 4096:
                return error_response("安装请求过大。", status_code=413)
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict) or set(payload) - {"sources"}:
                raise ValueError("安装请求无效，请重新打开页面后选择下载源。")
            sources = normalize_sources(payload.get("sources"))
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        self._installer.begin(kind, sources)
        # Reuse the existing task slot so singing, uploads and configuration cannot race installation.
        self._song_task = asyncio.create_task(self._installer.run(kind))
        return json_response(self._installer.snapshot(kind))

    async def tts_install_web(self):
        return await self._install_web("tts", start=True)

    async def cover_install_web(self):
        return await self._install_web("cover", start=True)

    async def tts_install_cancel_web(self):
        return await self._install_web("tts", cancel=True)

    async def cover_install_cancel_web(self):
        return await self._install_web("cover", cancel=True)

    async def tts_install_status_web(self):
        return await self._install_web("tts")

    async def cover_install_status_web(self):
        return await self._install_web("cover")

    async def config_get_web(self):
        """Read complete editable settings without writing defaults or starting services."""
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        try:
            cfg = plugin_settings._complete_plugin_config(
                plugin_settings._load_plugin_config()
            )
            return json_response(
                {"config": cfg, "revision": plugin_settings._config_revision(cfg)}
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return error_response(f"读取插件配置失败：{exc}", status_code=500)

    async def config_save_web(self):
        """Validate and persist a complete form with a fresh revision check."""
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        try:
            declared = request.headers.get("content-length")
            if declared is not None and int(declared) > MAX_CONFIG_BODY_BYTES:
                return error_response("配置内容超过 64 KiB 限制。", status_code=413)
            body = await request.body()
            if len(body) > MAX_CONFIG_BODY_BYTES:
                return error_response("配置内容超过 64 KiB 限制。", status_code=413)
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {"config", "revision"}:
                raise ValueError("保存请求须包含完整配置和读取时的版本。")
            revision = data["revision"]
            if (
                not isinstance(revision, str)
                or len(revision) != 64
                or any(char not in "0123456789abcdef" for char in revision)
            ):
                raise ValueError("配置版本无效，请重新读取配置。")
            cfg = plugin_settings._validate_plugin_config(data["config"])
            current = plugin_settings._load_plugin_config()
            if (
                plugin_settings._config_revision(
                    plugin_settings._complete_plugin_config(current)
                )
                != revision
            ):
                return error_response(
                    "配置已变化，请重新读取后再保存。", status_code=409
                )
            if self._cover_state["busy"] or (
                self._song_task and not self._song_task.done()
            ):
                return error_response(
                    "歌曲任务正在进行，请完成后再保存配置。", status_code=409
                )
            saved = dict(cfg)
            if isinstance(current.get("singing"), dict):
                saved["singing"] = {**current["singing"], **cfg["singing"]}
            plugin_settings._save_plugin_config(saved, self.config)
        except (ValueError, TypeError, KeyError) as exc:
            return error_response(str(exc), status_code=400)
        except (OSError, RuntimeError) as exc:
            return error_response(f"保存插件配置失败：{exc}", status_code=500)
        try:
            for provider in getattr(self.context, "get_all_providers", lambda: [])():
                if getattr(provider, "provider_config", {}).get("type") == "breeze_tts":
                    tts_provider._sync_provider_from_config(provider, cfg)
        except Exception as exc:
            logger.warning(
                f"[BreezeTTS] Config saved; live provider refresh deferred: {exc}"
            )
        return json_response(
            {"config": cfg, "revision": plugin_settings._config_revision(cfg)}
        )

    async def tts_status_web(self):
        """Read ordinary TTS readiness from the dashboard.

        Returns:
            Service state, including the intentional cover pause.
        """
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        if self._cover_state["busy"]:
            return json_response(
                {
                    "running": False,
                    "ready": False,
                    "starting": False,
                    "task_running": True,
                    "message": "翻唱中，普通语音暂时暂停，完成后自动恢复。",
                }
            )
        try:
            api_base = str(
                plugin_settings._load_plugin_config().get("api_base")
                or "http://127.0.0.1:7860"
            )
            return json_response(await self._tts_service.status(api_base=api_base))
        except (ValueError, OSError, RuntimeError) as exc:
            return error_response(str(exc))

    async def tts_start_web(self):
        """Start ordinary TTS without competing with an active cover.

        Returns:
            Existing or starting service state, or a dashboard error.
        """
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        if getattr(self, "_installer", None) and self._installer.busy:
            return error_response("正在安装服务，请完成后再启动。", status_code=409)
        if self._cover_state["busy"]:
            return error_response(
                "翻唱正在使用显存，完成后会自动恢复普通语音。", status_code=409
            )
        try:
            api_base = str(
                plugin_settings._load_plugin_config().get("api_base")
                or "http://127.0.0.1:7860"
            )
            return json_response(await self._tts_service.start(api_base=api_base))
        except (ValueError, OSError, RuntimeError) as exc:
            return error_response(str(exc))

    async def tts_stop_web(self):
        """Stop ordinary TTS on an explicit dashboard request.

        Returns:
            Service state, or an authorization/busy error.
        """
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        try:
            api_base = str(
                plugin_settings._load_plugin_config().get("api_base")
                or "http://127.0.0.1:7860"
            )
            state = await self._tts_service.stop(
                api_base=api_base,
                can_stop=lambda: (
                    not self._cover_state["busy"]
                    and not (self._song_task and not self._song_task.done())
                ),
            )
            return json_response(state)
        except (
            ValueError,
            OSError,
            RuntimeError,
            KeyError,
            httpx.HTTPError,
            psutil.Error,
        ) as exc:
            return error_response(str(exc))

    async def cover_stop_web(self):
        """Stop Cover while preserving the independently running TTS service.

        Returns:
            Service state, or an authorization/busy error.
        """
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        try:
            settings = plugin_settings._load_plugin_config().get("singing", {})
            if not isinstance(settings, dict):
                raise ValueError("翻唱配置无效，请重新保存插件配置。")
            api_base = str(settings.get("api_base") or "http://127.0.0.1:7861")
            state = await self._cover_service.stop(
                api_base=api_base,
                can_stop=lambda: (
                    not self._cover_state["busy"]
                    and not (self._song_task and not self._song_task.done())
                ),
            )
            return json_response(state)
        except (
            ValueError,
            OSError,
            RuntimeError,
            KeyError,
            httpx.HTTPError,
            psutil.Error,
        ) as exc:
            return error_response(str(exc))

    async def cover_status_web(self):
        """Read local service state for the authenticated dashboard user.

        Returns:
            Service and model readiness, or an authentication/configuration error.
        """
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        try:
            settings = plugin_settings._load_plugin_config().get("singing", {})
            if not isinstance(settings, dict):
                raise ValueError("翻唱配置无效，请重新保存插件配置。")
            api_base = (
                str(settings.get("api_base") or "http://127.0.0.1:7861")
                .strip()
                .rstrip("/")
            )
            state = await self._cover_service.status(api_base=api_base)
            state["task_running"] = bool(self._song_task and not self._song_task.done())
            state["enabled"] = bool(settings.get("enabled", True))
            if not state["enabled"]:
                state["message"] = "翻唱功能已禁用，已运行服务仍可手动关闭。"
            return json_response(state)
        except (ValueError, OSError, RuntimeError) as exc:
            return error_response(str(exc))

    async def cover_start_web(self):
        """Start the configured local service from the authenticated dashboard.

        Returns:
            The existing or newly started service state, or a startup error.
        """
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        if self._song_task and not self._song_task.done():
            return error_response("歌曲任务正在进行，请等任务完成。", status_code=409)
        try:
            api_base, _ = self._singing_settings()
            return json_response(await self._cover_service.start(api_base=api_base))
        except (ValueError, OSError, RuntimeError) as exc:
            logger.error(f"[BreezeTTS] Cover service startup failed: {exc}")
            return error_response(str(exc))
