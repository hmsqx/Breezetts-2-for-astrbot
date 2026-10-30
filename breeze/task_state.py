"""Shared song-task progress and temporary TTS pause state."""

import asyncio
import time

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

from . import settings as plugin_settings


class SongTaskMixin:
    """Keep task ownership and progress on the plugin and its retained context."""

    def _task_snapshot(self) -> dict:
        state = dict(
            getattr(self, "_task_progress", None)
            or {
                "active": False,
                "state": "idle",
                "name": "",
                "kind": "",
                "stage": "idle",
                "label": "暂无歌曲任务",
                "detail": "",
                "started_at": None,
                "updated_at": None,
                "elapsed_seconds": 0,
                "progress": None,
                "sent_parts": 0,
                "total_parts": None,
                "error": None,
            }
        )
        started = state.pop("_started_mono", None)
        if state["active"] and started is not None:
            state["elapsed_seconds"] = round(max(0.0, time.monotonic() - started), 1)
        return state

    def _task_begin(self, name: str, kind: str) -> None:
        now = time.time()
        self._task_progress = {
            "active": True,
            "state": "running",
            "name": name,
            "kind": kind,
            "stage": "preparing",
            "label": "正在准备",
            "detail": "",
            "started_at": now,
            "updated_at": now,
            "elapsed_seconds": 0,
            "progress": None,
            "sent_parts": 0,
            "total_parts": None,
            "error": None,
            "fallback_reason": "",
            "_started_mono": time.monotonic(),
        }
        self.context._breeze_song_progress = self._task_progress

    def _task_update(
        self, stage: str, label: str, detail: str = "", progress=None, **updates
    ) -> None:
        if getattr(self, "_task_progress", None) is None:
            self._task_begin("", "")
        self._task_progress.update(
            stage=stage,
            label=label,
            detail=str(detail)[:2000],
            progress=progress,
            updated_at=time.time(),
            **updates,
        )
        logger.info(f"[BreezeTTS] Song task {stage}: {label}; {str(detail)[:2000]}")

    def _task_finish(
        self, state: str, label: str, detail: str = "", error=None
    ) -> None:
        elapsed = self._task_snapshot()["elapsed_seconds"]
        self._task_update(
            state, label, detail, progress=1.0 if state == "completed" else None
        )
        self._task_progress.update(
            active=False,
            state=state,
            elapsed_seconds=elapsed,
            error=str(error)[:2000] if error is not None else None,
        )

    async def task_status_web(self):
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        return json_response(self._task_snapshot())

    def _song_task_done(self, task: asyncio.Task) -> None:
        if self._song_task is not task:
            return
        self._set_cover_busy(False)
        error = None if task.cancelled() else task.exception()
        if self._task_snapshot()["active"]:
            if task.cancelled():
                self._task_finish("cancelled", "歌曲任务已取消")
            else:
                detail = (
                    str(error)
                    if error is not None
                    else "任务已结束，但未确认发送结果。"
                )
                self._task_finish("failed", "歌曲任务未完成", detail, error=detail)
        if error is not None:
            logger.error(f"[BreezeTTS] Song task ended unexpectedly: {error}")

    def _set_cover_busy(self, busy: bool) -> None:
        """Share the temporary pause with live Breeze provider instances.

        Args:
            busy: Whether a cover is using or releasing GPU resources.
        """
        if not hasattr(self, "_cover_state"):
            self._cover_state = {"busy": False}
        self._cover_state["busy"] = busy
        for provider in getattr(self.context, "get_all_providers", lambda: [])():
            if getattr(provider, "provider_config", {}).get("type") == "breeze_tts":
                provider._breeze_cover_state = self._cover_state

    def _singing_settings(self, *, require_enabled: bool = True) -> tuple[str, float]:
        """Read current singing settings from the official plugin configuration.

        Returns:
            Cover backend base URL and request timeout.

        Raises:
            ValueError: Singing is disabled or its settings are invalid.
        """
        settings = plugin_settings._load_plugin_config().get("singing", {})
        if not isinstance(settings, dict):
            raise ValueError("翻唱配置无效，请重新保存插件配置")
        if require_enabled and not settings.get("enabled", True):
            raise ValueError("翻唱功能未启用，请在插件配置中启用")
        api_base = (
            str(settings.get("api_base") or "http://127.0.0.1:7861").strip().rstrip("/")
        )
        if not api_base.startswith(("http://", "https://")):
            raise ValueError("翻唱服务地址必须以 http:// 或 https:// 开头")
        return api_base, plugin_settings._as_float(settings.get("timeout"), 1800.0)
