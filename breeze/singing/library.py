"""Persistent song-library operations shared by tool and dashboard entry points."""

import asyncio
import json
import math
import time
import uuid
from pathlib import Path

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

from ..settings import MAX_CONFIG_BODY_BYTES
from .backend_client import _backend_error_detail

# The dashboard's native multipart limit is 128 MiB; leave room for framing.
MAX_SONG_UPLOAD_BYTES = 120 * 1024 * 1024
SONG_UPLOAD_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".flac",
    ".webm",
}


class SongLibraryMixin:
    """Use the plugin context and SongTaskMixin's shared task/progress ownership."""

    async def _library_items(self) -> list[dict]:
        api_base, _ = self._singing_settings(require_enabled=False)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            async with client.stream("GET", f"{api_base}/songs") as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > 1024 * 1024:
                        raise ValueError("歌曲库列表过大，无法显示。")
                    data.extend(chunk)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"读取歌曲库失败：{_backend_error_detail(bytes(data).decode('utf-8', 'replace'))}"
                    )
        songs = json.loads(data)
        if not isinstance(songs, list):
            raise ValueError("歌曲库返回格式无效。")
        items = []
        for song in songs:
            if not isinstance(song, dict):
                continue
            song_id, name, duration = (
                song.get("id"),
                song.get("name"),
                song.get("duration"),
            )
            if (
                not isinstance(song_id, str)
                or len(song_id) != 32
                or any(char not in "0123456789abcdef" for char in song_id)
                or not isinstance(name, str)
                or not name.strip()
                or len(name) > 160
                or type(duration) not in (int, float)
                or not math.isfinite(duration)
                or not 0 < duration <= 600
            ):
                continue
            source_type = song.get("source_type")
            if source_type not in ("url", "upload", "local"):
                source_type = "url" if song.get("source_url") else "local"
            item = {
                "id": song_id,
                "name": name,
                "duration": duration,
                "persistent": True,
                "source_type": source_type,
            }
            for key in ("artist", "language", "source_url"):
                if isinstance(song.get(key), str):
                    item[key] = song[key][:2048]
            items.append(item)
        return items

    async def library_songs_web(self):
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        try:
            return json_response(
                {"songs": await self._library_items(), "persistent": True}
            )
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            return error_response(
                f"无法读取歌曲库，请确认翻唱服务已启动：{exc}", status_code=503
            )

    async def library_upload_prepare_web(self):
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        try:
            if self._song_task and not self._song_task.done():
                return error_response(
                    "歌曲任务正在进行，请完成后再上传。", status_code=409
                )
            body = await request.body()
            if len(body) > MAX_CONFIG_BODY_BYTES:
                return error_response("上传信息过大。", status_code=413)
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {
                "name",
                "artist",
                "language",
                "file_name",
                "size",
            }:
                raise ValueError("上传信息不完整，请重新选择文件。")
            for key, limit in (("name", 160), ("artist", 160), ("file_name", 255)):
                value = data[key]
                if (
                    not isinstance(value, str)
                    or len(value) > limit
                    or any(ord(char) < 32 for char in value)
                ):
                    raise ValueError("歌曲名、歌手或文件名格式无效。")
            name, artist, file_name = (
                data["name"].strip(),
                data["artist"].strip(),
                data["file_name"],
            )
            if not name or not file_name or any(char in file_name for char in "/\\:"):
                raise ValueError("请填写歌名并选择有效的本地音频。")
            extension = Path(file_name).suffix.lower()
            if extension not in SONG_UPLOAD_EXTENSIONS:
                raise ValueError(
                    "不支持这个文件扩展名，请选择 WAV、MP3、M4A、FLAC 等音频。"
                )
            size, language = data["size"], data["language"]
            if type(size) is not int or not 0 < size <= MAX_SONG_UPLOAD_BYTES:
                raise ValueError("请选择非空且不超过 120 MiB 的音频文件。")
            if not isinstance(language, str) or language not in (
                "",
                "zh",
                "en",
                "ja",
                "ko",
            ):
                raise ValueError("歌曲语言无效。")
            songs = await self._library_items()
            if any(song["name"].casefold() == name.casefold() for song in songs):
                return error_response(
                    "已存在同名歌曲，请换一个名称；不会覆盖已有音源。", status_code=409
                )
            if self._song_task and not self._song_task.done():
                return error_response(
                    "歌曲任务正在进行，请完成后再上传。", status_code=409
                )
            now = time.monotonic()
            self._upload_drafts = {
                key: value
                for key, value in self._upload_drafts.items()
                if value["expires"] > now
            }
            if len(self._upload_drafts) >= 8:
                return error_response(
                    "待上传任务过多，请稍后重新选择文件。", status_code=409
                )
            upload_id = uuid.uuid4().hex
            self._upload_drafts[upload_id] = {
                "username": request.username,
                "expires": now + 1200,
                "name": name,
                "artist": artist,
                "language": language,
                "file_name": file_name,
                "extension": extension,
                "size": size,
            }
            return json_response({"upload_id": upload_id})
        except (ValueError, TypeError, OSError) as exc:
            return error_response(str(exc), status_code=400)
        except (httpx.HTTPError, RuntimeError) as exc:
            return error_response(f"请先启动翻唱服务：{exc}", status_code=503)

    async def library_upload_web(self, upload_id: str):
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        draft = self._upload_drafts.get(upload_id)
        if (
            draft is None
            or draft["expires"] <= time.monotonic()
            or draft["username"] != request.username
        ):
            return error_response(
                "上传请求已过期或无效，请重新选择文件。", status_code=400
            )
        if self._song_task and not self._song_task.done():
            return error_response("歌曲任务正在进行，请完成后再上传。", status_code=409)
        del self._upload_drafts[upload_id]
        self._task_begin(draft["name"], "learn")
        self._song_task = asyncio.create_task(self._stage_library_upload(draft))
        self._song_task.add_done_callback(self._song_task_done)
        return await self._song_task

    async def _stage_library_upload(self, draft: dict):
        staged = (
            Path(get_astrbot_temp_path())
            / f"breeze_upload_{uuid.uuid4().hex}{draft['extension']}"
        )
        handed_off = False
        uploads = []
        try:
            declared = request.headers.get("content-length")
            if (
                declared is not None
                and int(declared) > MAX_SONG_UPLOAD_BYTES + 1024 * 1024
            ):
                raise ValueError("音频上传超过 120 MiB 限制。")
            self._task_update("uploading", "正在接收手动上传的音源")
            files = await request.files()
            uploads = [upload for key in files.keys() for upload in files.getlist(key)]
            if set(files.keys()) != {"file"} or len(files.getlist("file")) != 1:
                raise ValueError("一次只能上传一个音频文件。")
            audio = files["file"]
            if Path(str(audio.filename or "")).suffix.lower() != draft["extension"]:
                raise ValueError("上传文件类型已变化，请重新选择文件。")
            size = 0
            with staged.open("xb") as stream:
                while chunk := await audio.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_SONG_UPLOAD_BYTES:
                        raise ValueError("音频上传超过 120 MiB 限制。")
                    stream.write(chunk)
            if size != draft["size"]:
                raise ValueError("音频上传不完整，请重新上传。")
            api_base, timeout = self._singing_settings(require_enabled=False)
            self._song_task = asyncio.create_task(
                self._perform_library_upload(draft, staged, api_base, timeout)
            )
            self._song_task.add_done_callback(self._song_task_done)
            self._song_task.add_done_callback(
                lambda task: self._cleanup_library_upload(staged)
            )
            handed_off = True
            return json_response({"started": True, "name": draft["name"]})
        except asyncio.CancelledError:
            self._task_finish("cancelled", "上传已取消")
            raise
        except Exception as exc:
            self._task_finish("failed", "上传失败", str(exc), error=exc)
            return error_response(str(exc), status_code=400)
        finally:
            try:
                for upload in {id(item): item for item in uploads}.values():
                    try:
                        await upload.close()
                    except Exception as exc:
                        logger.warning(
                            f"[BreezeTTS] Could not close an uploaded file: {exc}"
                        )
            finally:
                if not handed_off:
                    self._cleanup_library_upload(staged)

    def _cleanup_library_upload(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"[BreezeTTS] Could not remove staged song upload: {exc}")

    async def _perform_library_upload(
        self, draft: dict, staged: Path, api_base: str, timeout: float
    ) -> None:
        try:
            self._task_update(
                "normalizing",
                "正在保存音源到长期歌库",
                "正在校验音频并转换为标准格式；不需要联网或训练模型。",
            )
            with staged.open("rb") as audio:
                async with httpx.AsyncClient(
                    timeout=min(timeout, 900), trust_env=False
                ) as client:
                    response = await client.post(
                        f"{api_base}/songs/upload",
                        data={
                            "name": draft["name"],
                            "artist": draft["artist"],
                            "language": draft["language"],
                        },
                        files={
                            "audio": (
                                draft["file_name"],
                                audio,
                                "application/octet-stream",
                            )
                        },
                    )
            if response.status_code == 404:
                raise RuntimeError(
                    "翻唱后台尚未加载上传接口，请关闭并重新启动翻唱服务后重试。"
                )
            if response.status_code != 200:
                raise RuntimeError(_backend_error_detail(response.text))
            song = response.json()
            song_id = song.get("id") if isinstance(song, dict) else None
            duration = song.get("duration") if isinstance(song, dict) else None
            if (
                not isinstance(song, dict)
                or song.get("name") != draft["name"]
                or not isinstance(song_id, str)
                or len(song_id) != 32
                or any(char not in "0123456789abcdef" for char in song_id)
                or type(duration) not in (int, float)
                or not math.isfinite(duration)
                or not 0 < duration <= 600
            ):
                raise ValueError("翻唱后台没有返回有效的收录结果。")
            self._task_finish(
                "completed",
                "音源已永久保存",
                f"《{draft['name']}》已加入歌曲库；可让Bot用当前音色翻唱。",
            )
        except asyncio.CancelledError:
            self._task_finish("cancelled", "手动收录已取消")
            raise
        except Exception as exc:
            self._task_finish("failed", "手动收录失败", str(exc), error=exc)
        finally:
            self._cleanup_library_upload(staged)

    async def _delete_library_song(self, song_id: str, name: str) -> dict:
        if self._song_task and not self._song_task.done():
            raise RuntimeError("歌曲任务正在进行，请完成后再删除音源。")
        if (
            not isinstance(song_id, str)
            or len(song_id) != 32
            or any(char not in "0123456789abcdef" for char in song_id)
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 160
        ):
            raise ValueError("歌曲ID或歌名无效，请刷新歌曲库。")
        api_base, timeout = self._singing_settings(require_enabled=False)
        self._song_task = asyncio.create_task(
            self._delete_library_entry(api_base, timeout, song_id, name)
        )
        self._song_task.add_done_callback(self._song_task_done)
        return await self._song_task

    async def _delete_library_entry(
        self, api_base: str, timeout: float, song_id: str, name: str
    ) -> dict:
        async with httpx.AsyncClient(
            timeout=min(timeout, 30), trust_env=False
        ) as client:
            response = await client.request(
                "DELETE", f"{api_base}/songs/{song_id}", json={"expected_name": name}
            )
        if response.status_code in (404, 405):
            raise RuntimeError(
                "歌曲已移除或翻唱后台尚未加载删除接口，请刷新歌库或重新启动翻唱服务。"
            )
        if response.status_code != 200:
            raise RuntimeError(_backend_error_detail(response.text))
        result = response.json()
        if (
            not isinstance(result, dict)
            or result.get("deleted") is not True
            or result.get("id") != song_id
            or result.get("name") != name
        ):
            raise ValueError("删除结果未得到确认，请刷新歌曲库核对。")
        return result

    async def library_delete_web(self):
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("请先登录 AstrBot 管理台。", status_code=403)
        try:
            body = await request.body()
            if len(body) > 4096:
                return error_response("删除请求过大。", status_code=413)
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {"id", "name"}:
                raise ValueError("请明确要删除的歌曲。")
            if self._song_task and not self._song_task.done():
                return error_response(
                    "歌曲任务正在进行，请完成后再删除音源。", status_code=409
                )
            return json_response(
                await self._delete_library_song(data["id"], data["name"])
            )
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return error_response(str(exc), status_code=409)

    async def _learn_song(
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
        if self._song_task and not self._song_task.done():
            return "已有歌曲任务正在进行，请等它完成。"
        name = (name or "").strip()
        url = (url or "").strip()
        if not name or not url.startswith(("http://", "https://")):
            return "请输入歌曲名称和公开可访问的音频链接。"
        if (
            not isinstance(artist, str)
            or len(artist) > 160
            or any(ord(c) < 32 for c in artist)
        ):
            return "歌手信息须为不超过160个字符的文本。"
        artist = artist.strip()
        try:
            api_base, timeout = self._singing_settings()
            self._task_begin(name, "learn")
            self._song_task = asyncio.create_task(
                self._perform_learn(
                    event.unified_msg_origin,
                    name,
                    url,
                    api_base,
                    timeout,
                    artist=artist,
                )
            )
            self._song_task.add_done_callback(self._song_task_done)
            return f"正在收录《{name}》，完成后会通知当前对话，请稍等。"
        except Exception as e:
            return f"收录歌曲准备失败：{e}"

    async def _perform_learn(
        self,
        origin: str,
        name: str,
        url: str,
        api_base: str,
        timeout: float,
        *,
        artist: str = "",
    ) -> None:
        """Download one song and report the result to its originating conversation.

        Args:
            origin: Original conversation identifier.
            name: Song title.
            url: Public song URL.
            api_base: Cover service base URL.
            timeout: Maximum request time in seconds.
            artist: Confirmed performer, kept for later lyric matching.
        """
        if not self._task_snapshot()["active"]:
            self._task_begin(name, "learn")
        self._task_update(
            "downloading", "正在收录音频", "正在获取来源音频并写入歌曲库。"
        )
        succeeded = False
        try:
            request_timeout = min(timeout, 1200.0)
            song_request = {"name": name, "url": url}
            if artist:
                song_request["artist"] = artist
            async with httpx.AsyncClient(
                timeout=request_timeout, trust_env=False
            ) as client:
                response = await asyncio.wait_for(
                    client.post(f"{api_base}/songs", json=song_request),
                    timeout=request_timeout,
                )
            response.raise_for_status()
            song = response.json()
            if not isinstance(song, dict) or not song.get("id") or not song.get("name"):
                raise ValueError("翻唱服务返回了无效的歌曲信息")
            duration = song.get("duration")
            length = (
                f"{duration:.1f} 秒"
                if isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(duration)
                and duration > 0
                else "时长未知"
            )
            message = f"已收录来源音频：{song['name']}（{length}）。"
            if song.get("reused") is True:
                message = f"已存在同名音源，复用已收录的《{song['name']}》（{length}），未修改旧条目。"
            succeeded = True
            self._task_update(
                "notifying", "收录完成，正在发送通知", f"来源音频时长：{length}。"
            )
        except asyncio.CancelledError:
            self._task_finish("cancelled", "收录已取消")
            raise
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"[BreezeTTS] Song import HTTP {e.response.status_code}: {e.response.text[:4000]}"
            )
            detail = f"HTTP {e.response.status_code}：{_backend_error_detail(e.response.text)}"
            self._task_finish("failed", "收录失败", detail, error=detail)
            message = f"收录《{name}》失败，请稍后重试。"
        except (httpx.TimeoutException, asyncio.TimeoutError):
            self._task_finish("failed", "收录超时", error="收录歌曲请求超时。")
            message = f"收录《{name}》超时，请稍后重试。"
        except Exception as e:
            logger.error(f"[BreezeTTS] Song download failed: {e}")
            self._task_finish("failed", "收录失败", str(e), error=e)
            message = f"收录《{name}》失败，请稍后重试。"
        try:
            sent = await self.context.send_message(
                origin, MessageChain([Plain(message)])
            )
            if not sent:
                logger.error(
                    "[BreezeTTS] Failed to report song result: platform not found"
                )
                if succeeded:
                    self._task_finish(
                        "failed",
                        "音频已收录，通知发送失败",
                        error="找不到发起请求的平台。",
                    )
            elif succeeded:
                self._task_finish("completed", "收录完成，已发送通知")
        except asyncio.CancelledError:
            if succeeded:
                self._task_finish("cancelled", "音频已收录，通知已取消")
            raise
        except Exception as e:
            logger.error(f"[BreezeTTS] Failed to report song result: {e}")
            if succeeded:
                self._task_finish("failed", "音频已收录，通知发送失败", str(e), error=e)

    async def _list_learned_songs(self, event: AstrMessageEvent) -> str:
        """列出已经收录、可以翻唱的歌曲。

        Returns:
            已收录歌曲列表或错误信息。
        """
        try:
            api_base, timeout = self._singing_settings()
            async with httpx.AsyncClient(
                timeout=min(timeout, 15.0), trust_env=False
            ) as client:
                response = await client.get(f"{api_base}/songs")
            response.raise_for_status()
            songs = response.json()
            if not isinstance(songs, list):
                raise ValueError("翻唱服务返回了无效的歌曲列表")
            names = []
            for song in songs:
                if (
                    not isinstance(song, dict)
                    or not song.get("name")
                    or not song.get("id")
                ):
                    continue
                duration = song.get("duration")
                length = (
                    f"{duration:.1f} 秒"
                    if isinstance(duration, (int, float))
                    and not isinstance(duration, bool)
                    and math.isfinite(duration)
                    and duration > 0
                    else "时长未知"
                )
                names.append(f"{song['name']}（{length}）")
            return (
                "已收录来源音频（可能包含试听片段）：\n" + "\n".join(names)
                if names
                else "还没有收录歌曲。"
            )
        except httpx.HTTPStatusError as e:
            return f"获取歌曲列表失败（HTTP {e.response.status_code}）：{e.response.text[:300]}"
        except Exception as e:
            return f"获取歌曲列表失败：{e}"

    async def _delete_song(self, event: AstrMessageEvent, name: str) -> str:
        """管理员明确要求删除某首已收录歌曲时，删除其原始音源和对应时间轴缓存。

        只在用户本次明确要求删除这首歌时调用；不能因匹配失败、重新收录、空间不足或清理任务擅自删除。
        歌曲任务进行中禁止删除；不会删除其他歌曲或用户电脑上的上传原文件。

        Args:
            name(string): 用户明确要求删除的已收录歌曲完整名称。

        Returns:
            已删除的歌名或具体失败原因。
        """
        if not event.is_admin():
            return "只有 AstrBot 管理员可以删除歌曲音源。"
        if not isinstance(name, str) or not name.strip():
            return "请明确要删除的歌曲名称。"
        try:
            if self._song_task and not self._song_task.done():
                return "歌曲任务正在进行，请完成后再删除音源。"
            matches = [
                song
                for song in await self._library_items()
                if song["name"].strip().casefold() == name.strip().casefold()
            ]
            if not matches:
                return f"歌曲库里没有《{name.strip()}》。"
            if len(matches) != 1:
                return "存在多个同名条目，请在管理页按具体条目删除。"
            await self._delete_library_song(matches[0]["id"], matches[0]["name"])
            return f"已删除《{matches[0]['name']}》的原始音源和对应时间轴缓存。"
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            return f"删除歌曲失败：{exc}"
