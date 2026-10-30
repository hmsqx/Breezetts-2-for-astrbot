"""AstrBot registration and plugin lifecycle; implementations live in feature modules."""

import asyncio
import json
import os
from pathlib import Path

import psutil
from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.computer.windows_job import WindowsProcessJob
from astrbot.core.message.components import Record
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from . import settings
from . import tts_provider as speech_provider
from .cover import CoverMixin
from .dashboard import DashboardMixin
from .library import SongLibraryMixin
from .task_state import SongTaskMixin
from .tts_provider import ProviderBreezeTTS as ProviderBreezeTTS

speech_provider._register_provider()

WEB_ROUTES = (
    ("cover/status", "cover_status_web", "GET"),
    ("cover/start", "cover_start_web", "POST"),
    ("cover/stop", "cover_stop_web", "POST"),
    ("tts/status", "tts_status_web", "GET"),
    ("tts/start", "tts_start_web", "POST"),
    ("tts/stop", "tts_stop_web", "POST"),
    ("tts/install", "tts_install_web", "POST"),
    ("tts/install/status", "tts_install_status_web", "GET"),
    ("tts/install/cancel", "tts_install_cancel_web", "POST"),
    ("cover/install", "cover_install_web", "POST"),
    ("cover/install/status", "cover_install_status_web", "GET"),
    ("cover/install/cancel", "cover_install_cancel_web", "POST"),
    ("config", "config_get_web", "GET"),
    ("config", "config_save_web", "POST"),
    ("task/status", "task_status_web", "GET"),
    ("library/songs", "library_songs_web", "GET"),
    ("library/upload/prepare", "library_upload_prepare_web", "POST"),
    ("library/upload/<upload_id>", "library_upload_web", "POST"),
    ("library/delete", "library_delete_web", "POST"),
)


@register("breeze_tts", "Echok", "Breeze TTS 语音回复与歌曲翻唱", "1.9.0")
class BreezeTTSPlugin(
    DashboardMixin, SongLibraryMixin, CoverMixin, SongTaskMixin, Star
):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        from .installer import ServiceInstaller
        from .service_control import BreezeService, CoverService

        super().__init__(context)
        self.config = config
        self._song_task: asyncio.Task | None = None
        self._cover_state = {"busy": False}
        self._task_progress = getattr(context, "_breeze_song_progress", None)
        self._upload_drafts: dict[str, dict] = {}
        jobs = getattr(context, "_breeze_service_jobs", None)
        if jobs is None:
            old_job = getattr(context, "_breeze_service_job", None)
            jobs = [old_job] if old_job is not None else [WindowsProcessJob()]
            context._breeze_service_jobs = jobs
        owner_pid = os.getpid()
        owner_created = psutil.Process(owner_pid).create_time()
        self._cover_service = CoverService(
            jobs=jobs,
            job_factory=WindowsProcessJob,
            owner_pid=owner_pid,
            owner_create_time=owner_created,
        )
        self._tts_service = BreezeService(
            jobs=jobs,
            job_factory=WindowsProcessJob,
            owner_pid=owner_pid,
            owner_create_time=owner_created,
        )
        if not hasattr(context, "_breeze_install_progress"):
            context._breeze_install_progress = {}
        self._installer = ServiceInstaller(
            self._tts_service, self._cover_service, context._breeze_install_progress
        )
        for endpoint, handler, method in WEB_ROUTES:
            context.register_web_api(
                f"/astrbot_plugin_breeze_tts/{endpoint}",
                getattr(self, handler),
                [method],
                "Manage Breeze services, settings and songs",
            )

    async def initialize(self) -> None:
        """插件激活时，将插件配置同步到当前使用的 ProviderBreezeTTS 实例"""
        self._set_cover_busy(False)
        for controller in (
            getattr(self, "_cover_service", None),
            getattr(self, "_tts_service", None),
        ):
            if controller is not None:
                try:
                    controller.adopt_existing()
                except (OSError, RuntimeError, psutil.Error) as exc:
                    logger.warning(
                        f"[BreezeTTS] Failed to adopt an existing service: {exc}"
                    )
        if not self.config:
            return
        if getattr(self.config, "first_deploy", False):
            legacy = Path(__file__).parent / "data" / "cmd_config.json"
            if legacy.exists():
                with legacy.open(encoding="utf-8-sig") as stream:
                    previous = json.load(stream)
                if not isinstance(previous, dict):
                    raise TypeError("Legacy Breeze TTS configuration must be an object")
                settings._save_plugin_config(previous, self.config)
        try:
            tts_provider = speech_provider._get_breeze_provider(self.context)
            if tts_provider is None:
                return

            speech_provider._sync_provider_from_config(tts_provider, dict(self.config))

            mode = "音色克隆" if tts_provider.ref_audio_path else "Voice Design"
            logger.info(f"[BreezeTTS] 插件配置已同步到 Provider，当前模式: {mode}")
        except Exception as e:
            logger.warning(f"[BreezeTTS] 同步配置到 Provider 失败: {e}")

    @filter.on_decorating_result(priority=-1000000)
    async def cover_text_reply(self, event: AstrMessageEvent) -> None:
        """Keep responses as text while the selected Breeze service is paused.

        Args:
            event: Response event being decorated.
        """
        if not getattr(self, "_cover_state", {}).get("busy"):
            return
        provider = await self.context.get_using_tts_provider_async(
            event.unified_msg_origin
        )
        if getattr(provider, "provider_config", {}).get("type") != "breeze_tts":
            return
        result = event.get_result()
        if result is not None and result.is_llm_result():
            result.set_result_content_type(ResultContentType.GENERAL_RESULT)

    @filter.command("breeze_sample")
    async def breeze_sample(self, event):
        """生成一段语音样本并发送，用于测试当前音色"""
        if getattr(self, "_cover_state", {}).get("busy"):
            yield event.plain_result("正在翻唱，普通语音暂时暂停，完成后自动恢复。")
            return
        text = event.message_str.replace("/breeze_sample", "").strip()
        if not text:
            text = "这是一段测试语音，用来听听当前的音色效果。"

        try:
            tts_provider = speech_provider._get_breeze_provider(self.context)
            if tts_provider is None:
                yield event.plain_result("Breeze TTS Provider 不可用，请检查配置。")
                return

            audio_path = await tts_provider.get_audio(text)
            if not audio_path:
                yield event.plain_result("普通语音暂时暂停，翻唱完成后自动恢复。")
                return
            chain = MessageChain([Record(file=audio_path, text=text)])
            yield event.chain_result(chain)
        except Exception as e:
            logger.error(f"[BreezeTTS] 生成样本失败: {e}")
            yield event.plain_result(f"生成样本失败: {e}")

    @filter.command("breeze_status")
    async def breeze_status(self, event):
        """查看 Breeze TTS 当前配置状态"""
        try:
            tts_provider = speech_provider._get_breeze_provider(self.context)
            if tts_provider is None:
                yield event.plain_result("Breeze TTS Provider 不可用。")
                return

            mode = "🎙️ 音色克隆" if tts_provider.ref_audio_path else "🎨 Voice Design"
            ref = tts_provider.ref_audio_path or "（未设置）"
            lines = [
                "【Breeze TTS 状态】",
                f"模式: {mode}",
                f"服务: {tts_provider.api_base}",
                f"CFG: {tts_provider.cfg_scale if tts_provider.instruction and (not tts_provider.ref_audio_path or tts_provider.clone_use_instruction) else 1.0} | Seed: {tts_provider.seed}",
                f"参考音频: {ref}",
                f"参考文本: {tts_provider.ref_text or '（未设置）'}",
                f"操作超时: {tts_provider.timeout}s | 最多请求: {tts_provider.max_retries}次（含首次）",
                f"歌曲任务: {'进行中' if self._song_task and not self._song_task.done() else '空闲'}",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取状态失败: {e}")

    @llm_tool(name="set_voice_clone")
    async def set_voice_clone_tool(
        self, event: AstrMessageEvent, ref_audio_path: str, ref_text: str = ""
    ) -> str:
        """设置音色克隆，让 Bot 使用指定参考音频的音色说话。

        当用户提供了音频文件路径并想让 Bot 模仿这个音色时调用。
        会立即生效并持久化保存。

        Args:
            ref_audio_path(string): 参考音频文件的完整路径，例如 D:/voice/sample.wav
            ref_text(string): 参考音频中所说的原文，必填

        Returns:
            设置结果，包含当前使用的音色模式
        """
        try:
            tts_provider = speech_provider._get_breeze_provider(self.context)
            if tts_provider is None:
                return "错误：Breeze TTS Provider 不可用，请检查配置。"

            ref_audio_path = (ref_audio_path or "").strip()
            ref_text = (ref_text or "").strip()
            if not os.path.isfile(ref_audio_path):
                return f"错误：参考音频文件不存在: {ref_audio_path}"
            if not ref_text:
                return "错误：音色克隆需要提供参考音频对应的文本。"

            cfg = settings._load_plugin_config()
            cfg["ref_audio_path"] = ref_audio_path
            cfg["ref_text"] = ref_text
            settings._save_plugin_config(cfg, self.config)
            speech_provider._sync_provider_from_config(tts_provider, cfg)

            return (
                f"音色克隆已设置。\n"
                f"参考音频: {tts_provider.ref_audio_path}\n"
                f"参考文本: {tts_provider.ref_text or '（未提供）'}\n"
                f"当前模式: 🎙️ 音色克隆"
            )
        except Exception as e:
            logger.error(f"[BreezeTTS] set_voice_clone 失败: {e}")
            return f"设置音色克隆失败: {e}"

    @llm_tool(name="set_voice_design")
    async def set_voice_design_tool(
        self, event: AstrMessageEvent, instruction: str, cfg_scale: float = 4.0
    ) -> str:
        """设置音色设计，用文字描述生成 Bot 的说话音色。

        当用户想用文字描述来定义 Bot 音色时调用。会清空参考音频，切换回 Voice Design 模式。

        Args:
            instruction(string): 音色描述，例如"一位温柔的女性，语速较慢，声音柔和"
            cfg_scale(float): 指令遵循强度，范围 1.0~10.0，默认 4.0。越大越严格按描述来，但可能不自然

        Returns:
            设置结果，包含当前使用的音色模式
        """
        try:
            tts_provider = speech_provider._get_breeze_provider(self.context)
            if tts_provider is None:
                return "错误：Breeze TTS Provider 不可用，请检查配置。"

            if not instruction.strip():
                return "错误：音色描述不能为空。"
            cfg = settings._load_plugin_config()
            cfg["instruction"] = instruction.strip()
            cfg["cfg_scale"] = min(10.0, max(1.0, settings._as_float(cfg_scale, 4.0)))
            cfg["ref_audio_path"] = ""
            cfg["ref_text"] = ""
            settings._save_plugin_config(cfg, self.config)
            speech_provider._sync_provider_from_config(tts_provider, cfg)

            return (
                f"音色设计已设置。\n"
                f"描述: {tts_provider.instruction}\n"
                f"CFG: {tts_provider.cfg_scale}\n"
                f"当前模式: 🎨 Voice Design（文字描述生成音色）"
            )
        except Exception as e:
            logger.error(f"[BreezeTTS] set_voice_design 失败: {e}")
            return f"设置音色设计失败: {e}"

    @llm_tool(name="speak")
    async def speak_tool(self, event: AstrMessageEvent, text: str) -> str:
        """按照当前配置的音色，把文本转成语音并发送给用户。

        当需要让 Bot 用语音回复时调用。直接使用当前设置好的音色（克隆或设计）来合成。

        Args:
            text(string): 要合成语音的文本内容

        Returns:
            发送结果：成功返回"已发送语音"，失败返回错误原因
        """
        if getattr(self, "_cover_state", {}).get("busy"):
            return "正在翻唱，普通语音暂时暂停，请先用文字回复；翻唱结束后自动恢复。"
        try:
            tts_provider = speech_provider._get_breeze_provider(self.context)
            if tts_provider is None:
                return "Breeze TTS Provider 不可用"

            content = (text or "").strip()
            if not content:
                return "文本为空"

            audio_path = await tts_provider.get_audio(content)
            if not audio_path:
                return "普通语音暂时暂停，请先用文字回复；翻唱完成后自动恢复。"
            chain = MessageChain([Record(file=audio_path, text=content)])
            await self.context.send_message(event.unified_msg_origin, chain)
            return "已发送语音"
        except Exception as e:
            logger.error(f"[BreezeTTS] speak 失败: {e}")
            return f"语音发送失败: {e}"

    @llm_tool(name="learn_song")
    async def learn_song_tool(
        self, event: AstrMessageEvent, name: str, url: str, artist: str = ""
    ) -> str:
        """收录公开可访问的歌曲音频，供之后保留旋律、转换声线翻唱。

        收录来源实际可获取的音频，不会训练模型权重。先主动使用 web_search_deepseek 搜索歌曲，
        查找可访问的音频或受支持的歌曲页面链接，再传入歌曲名和链接，无需让用户手动提供 URL。
        搜索摘要、歌词和普通文章不是音频；不要猜测链接。若来源只提供试听片段，不能称为完整歌曲，
        请结合返回的音频时长如实告知用户；下载失败也应明确告知。
        已确认实际演唱者时同时填写artist，避免之后匹配到同名歌曲；不要把视频上传者当歌手。

        Args:
            name(string): 歌曲名称。
            url(string): 可公开获取歌曲音频的 HTTP 或 HTTPS 链接。
            artist(string): 已确认的实际演唱者；未确认时留空，不猜测。

        收录在后台进行，完成后会通知当前对话。

        Returns:
            任务开始状态或具体错误。
        """
        return await self._learn_song(event, name, url, artist=artist)

    @llm_tool(name="list_learned_songs")
    async def list_learned_songs_tool(self, event: AstrMessageEvent) -> str:
        """列出已经收录、可以翻唱的歌曲。

        Returns:
            已收录歌曲列表或错误信息。
        """
        return await self._list_learned_songs(event)

    @llm_tool(name="delete_song")
    async def delete_song_tool(self, event: AstrMessageEvent, name: str) -> str:
        """管理员明确要求删除某首已收录歌曲时，删除其原始音源和对应时间轴缓存。

        只在用户本次明确要求删除这首歌时调用；不能因匹配失败、重新收录、空间不足或清理任务擅自删除。
        歌曲任务进行中禁止删除；不会删除其他歌曲或用户电脑上的上传原文件。

        Args:
            name(string): 用户明确要求删除的已收录歌曲完整名称。

        Returns:
            已删除的歌名或具体失败原因。
        """
        return await self._delete_song(event, name)

    @llm_tool(name="sing_song")
    async def sing_song_tool(
        self, event: AstrMessageEvent, name: str, delivery: str = "voice"
    ) -> str:
        """用当前参考音色翻唱整首已收录歌曲，默认按自然停顿连续发送多条语音。

        用户明确要整首或完整翻唱时调用。只想唱一段、高潮或指定片段时用 sing_song_clip。
        未收录时先搜索并使用 learn_song，不能把试听片段称为完整歌曲。

        Args:
            name(string): 已收录歌曲的完整名称。
            delivery(string): voice按停顿连续发送语音（默认）；仅用户明确要文件时用file。

        Returns:
            整首翻唱任务状态。
        """
        return await self._sing_song(event, name, delivery=delivery)

    @llm_tool(name="sing_song_clip")
    async def sing_song_clip_tool(
        self,
        event: AstrMessageEvent,
        name: str,
        start_seconds: float = -1.0,
        duration_seconds: float = 0.0,
        end_seconds: float = -1.0,
        artist: str = "",
        delivery: str = "voice",
    ) -> str:
        """联网核对歌词或识别原音频时间轴，让模型选副歌候选并按停顿发送。

        默认不要填写固定时长或猜测时间，后台会读取同步歌词、调用当前模型判断完整副歌范围。
        找不到匹配版本时会临时在CPU识别当前音频的时间轴；识别不足时明确失败，不随机截取30秒。
        工具返回正在处理只表示任务启动，不能提前告诉用户已能翻唱或已经成功。
        用户明确指定时间时才填写起点和终点或时长；语音只在人声停顿处分段，不按15秒硬切。
        不会自动改发文件；缺少安全停顿时明确提示，只有用户明确要文件才用delivery=file。
        用户要整首时应调用 sing_song，未收录时先搜索并使用 learn_song。
        对话中已确认实际歌手时务必填写artist，包括随后只说“唱吧”的情况；不能只按同名曲目匹配。

        Args:
            name(string): 已收录歌曲的完整名称。
            start_seconds(float): 用户指定的原曲起点；默认-1由模型分析，不可猜测。
            duration_seconds(float): 用户指定的片段时长；默认0不限制副歌长度，手动范围最长600秒。
            end_seconds(float): 用户指定的原曲终点；默认-1。起点和终点决定完整片段。
            artist(string): 对话中已确认的实际歌手或录音版本；已知时填写，未知留空。
            delivery(string): voice按停顿连续发送语音（默认）；用户明确要文件时用file。

        Returns:
            片段翻唱任务状态。
        """
        return await self._sing_song_clip(
            event,
            name,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
            end_seconds=end_seconds,
            artist=artist,
            delivery=delivery,
        )

    async def terminate(self):
        if self._song_task and not self._song_task.done():
            self._song_task.cancel()
            await asyncio.gather(self._song_task, return_exceptions=True)
        self._set_cover_busy(False)
