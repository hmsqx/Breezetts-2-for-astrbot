import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import chorus_planner as planner
import httpx

SONG = {"id": "123", "name": "Sample Song", "duration": 120}
LYRICS = "[ar:Artist]\n[offset:+500]\n[00:01.00]作词：Creator\n[00:10.00]first\n[00:20.00][00:40.00]repeat\n[00:30.00]last\n[00:50.00]next"
TRACK = {
    "id": 77,
    "trackName": "Sample Song",
    "artistName": "Artist",
    "albumName": "Album",
    "duration": 120,
    "syncedLyrics": LYRICS,
}
CHINESE_LYRICS = "[00:10.00]这是完整的第一句歌词\n[00:20.00]这是完整的第二句歌词\n[00:30.00]这是完整的第三句歌词\n[00:40.00]这是完整的第四句歌词"
ENGLISH_LYRICS = "[00:10.00]You are the light that stays with me\n[00:20.00]And we will share our dreams together\n[00:30.00]Your voice is in my heart forever\n[00:40.00]This is the song we have for you"


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.real_netease = planner._netease_candidates
        self.netease = AsyncMock(side_effect=ValueError("离线模拟没有网易云匹配结果。"))
        patcher = patch.object(planner, "_netease_candidates", self.netease)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mock_http(self, handler):
        transport = httpx.MockTransport(handler)
        cls = httpx.AsyncClient
        return patch.object(
            planner.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: cls(transport=transport, **kwargs),
        )

    async def test_verified_lines_offset_and_duplicate_timestamps(self):
        async def handler(request):
            self.assertEqual(request.url.host, "lrclib.net")
            self.assertEqual(request.url.path, "/api/search")
            self.assertEqual(request.url.params["track_name"], "Sample Song")
            self.assertEqual(request.url.params["artist_name"], "Artist")
            self.assertIn("BreezeCover/1.4", request.headers["user-agent"])
            return httpx.Response(200, json=[TRACK])

        generate = AsyncMock(
            return_value=json.dumps(
                {"start_line": 1, "end_line": 3, "reason": "重复段落"}
            )
        )
        with self.mock_http(handler):
            result = await planner.plan_chorus(SONG, "Artist", generate)
        self.assertAlmostEqual(result["start_seconds"], 20.3)
        self.assertEqual(result["end_seconds"], 50.5)
        self.assertEqual(result["phrase_ends"], [30.5, 40.5, 50.5])
        self.assertEqual(result["source_url"], "https://lrclib.net/api/get/77")
        self.assertNotIn("repeat", str(result))
        self.assertIn("未精确对齐", result["match_basis"])
        self.assertIn("外部数据", generate.await_args.args[1])

    async def test_unique_without_artist_is_clearly_qualified(self):
        with self.mock_http(lambda req: httpx.Response(200, json=[TRACK])):
            result = await planner.plan_chorus(
                SONG,
                "",
                AsyncMock(return_value='{"start_line":0,"end_line":0,"reason":"首段"}'),
            )
        self.assertIn("歌手由唯一结果推定", result["match_basis"])

    async def test_rejects_ambiguous_and_bad_matches(self):
        model = AsyncMock(return_value="{}")
        for candidates in (
            [TRACK, {**TRACK, "id": 78, "artistName": "Other"}],
            [
                TRACK,
                {
                    **TRACK,
                    "id": 78,
                    "albumName": "Live",
                    "syncedLyrics": LYRICS + "\n[01:10.00]variant",
                },
            ],
            [{**TRACK, "duration": 123}],
            [{**TRACK, "syncedLyrics": None}],
            [{**TRACK, "trackName": "Other"}],
        ):
            with (
                self.subTest(candidates=len(candidates)),
                self.mock_http(lambda req: httpx.Response(200, json=candidates)),
            ):
                with self.assertRaises(ValueError):
                    await planner.plan_chorus(SONG, "", model)
        model.assert_not_awaited()

    def test_closest_duration_precedes_farther_timeline_variants(self):
        farther = {
            **TRACK,
            "id": 78,
            "duration": 121,
            "syncedLyrics": LYRICS + "\n[01:10.00]variant",
        }
        chosen, _, _ = planner._match(SONG, "Artist", [farther, TRACK])
        self.assertEqual(chosen["id"], 77)

    def test_near_duration_uses_strong_independent_timeline_consensus(self):
        variants = [
            {**TRACK, "id": item_id, "duration": 120.02} for item_id in (77, 78)
        ]
        other = {**TRACK, "id": 79, "syncedLyrics": LYRICS + "\n[01:10.00]variant"}
        chosen, _, _ = planner._match(SONG, "Artist", [other, *variants])
        self.assertEqual(chosen["id"], 77)
        for items in (
            [other, variants[0]],
            [*variants, other, {**other, "id": 80}],
            [variants[0], variants[0], other],
            [*variants, {**other, "artistName": "Other"}],
            [*variants, {**other, "albumName": "Live"}],
        ):
            with (
                self.subTest(ids=[item["id"] for item in items]),
                self.assertRaisesRegex(ValueError, "歧义"),
            ):
                planner._match(SONG, "", items)
        with self.assertRaisesRegex(ValueError, "冲突时间轴"):
            planner._match(SONG, "Artist", [TRACK, {**other, "id": 77}])

    async def test_lrclib_transient_failure_retries_once(self):
        for failure in (503, 502, 504, httpx.ConnectError(""), httpx.ReadTimeout("")):
            calls = []

            async def handler(request):
                calls.append(request)
                if len(calls) == 1:
                    if isinstance(failure, int):
                        return httpx.Response(failure)
                    raise failure
                return httpx.Response(200, json=[TRACK])

            with (
                self.subTest(failure=str(failure)),
                self.mock_http(handler),
                patch.object(planner.asyncio, "sleep", AsyncMock()) as sleep,
            ):
                result = await planner._search("Sample Song", "Artist")
            self.assertEqual(result, [TRACK])
            self.assertEqual(len(calls), 2)
            sleep.assert_awaited_once_with(0.5)

    async def test_lrclib_retry_is_bounded_and_errors_keep_diagnostics(self):
        for failure, expected, attempts in (
            (503, "HTTP 503", 2),
            (httpx.ConnectError(""), "ConnectError", 2),
            (401, "HTTP 401", 1),
            (403, "HTTP 403", 1),
            (429, "HTTP 429", 1),
        ):
            calls = []

            async def handler(request):
                calls.append(request)
                if isinstance(failure, int):
                    return httpx.Response(failure)
                raise failure

            with (
                self.subTest(failure=str(failure)),
                self.mock_http(handler),
                patch.object(planner.asyncio, "sleep", AsyncMock()),
            ):
                with self.assertRaisesRegex(ValueError, expected):
                    await planner._search("Sample Song", "Artist")
            self.assertEqual(len(calls), attempts)

    async def test_strict_source_title_artist_hint_does_not_become_verified_artist(
        self,
    ):
        source = {**SONG, "title": "【Hi-Res】｜《Sample Song》- Artist -‘𝙈𝙑视听’"}
        query = AsyncMock(return_value=[TRACK])
        with patch.object(planner, "_search", query):
            result = await planner.plan_chorus(
                source,
                "",
                AsyncMock(return_value='{"start_line":0,"end_line":1,"reason":"副歌"}'),
            )
        query.assert_awaited_once_with("Sample Song", "Artist")
        self.assertNotIn("artist", source)
        self.assertIn("标题中的歌手线索", result["match_basis"])
        self.assertIn("不等于已核验", result["match_basis"])
        explicit = {**source, "artist": "Other"}
        query = AsyncMock(return_value=[{**TRACK, "artistName": "Other"}])
        with patch.object(planner, "_search", query):
            await planner.plan_chorus(
                explicit,
                "",
                AsyncMock(return_value='{"start_line":0,"end_line":1,"reason":"副歌"}'),
            )
        query.assert_awaited_once_with("Sample Song", "Other")

    def test_source_title_artist_hint_rejects_ambiguous_descriptions(self):
        for source in (
            {**SONG, "uploader": "Artist"},
            {**SONG, "title": "《Another Song》- Artist - MV"},
            {**SONG, "title": "《Sample Song》- Artist - MV 高音质推荐"},
            {**SONG, "title": "Artist 唱的 Sample Song MV"},
            {**SONG, "title": "《Sample Song》- Artist 翻唱 - MV"},
        ):
            with self.subTest(source=source):
                self.assertEqual(planner._title_artist_hint(source, "Sample Song"), "")

    async def test_identical_lyrics_different_album_reuses_timeline(self):
        variants = [TRACK, {**TRACK, "id": 78, "albumName": "Single"}]
        generate = AsyncMock(
            return_value='{"start_line":0,"end_line":1,"reason":"重复段"}'
        )
        with self.mock_http(lambda req: httpx.Response(200, json=variants)):
            result = await planner.plan_chorus(SONG, "Artist", generate)
        self.assertEqual(result["source_url"], "https://lrclib.net/api/get/77")
        self.assertIn("未精确对齐", result["match_basis"])
        with self.mock_http(lambda req: httpx.Response(200, json=variants)):
            chosen = await planner.plan_chorus(
                {**SONG, "album": "Single"}, "Artist", generate
            )
        self.assertEqual(chosen["source_url"], "https://lrclib.net/api/get/78")
        live_variants = [
            TRACK,
            {
                **TRACK,
                "id": 79,
                "albumName": "Live",
                "syncedLyrics": LYRICS + "\n[01:10.00]variant",
            },
        ]
        with self.mock_http(lambda req: httpx.Response(200, json=live_variants)):
            chosen_live = await planner.plan_chorus(
                {**SONG, "album": "Live"}, "Artist", generate
            )
        self.assertEqual(chosen_live["source_url"], "https://lrclib.net/api/get/79")
        with self.mock_http(lambda req: httpx.Response(200, json=variants)):
            with self.assertRaisesRegex(ValueError, "专辑"):
                await planner.plan_chorus(
                    {**SONG, "album": "Missing"}, "Artist", generate
                )

    async def test_model_and_timing_are_validated(self):
        for output in (
            "not json",
            "[]",
            '{"start_line":true,"end_line":1,"reason":"x"}',
            '{"start_line":1,"end_line":100,"reason":"x"}',
            '{"start_line":2,"end_line":1,"reason":"x"}',
            '{"start_line":0,"end_line":0,"reason":""}',
        ):
            with (
                self.subTest(output=output),
                self.mock_http(lambda req: httpx.Response(200, json=[TRACK])),
            ):
                with self.assertRaises(ValueError):
                    await planner.plan_chorus(
                        SONG, "Artist", AsyncMock(return_value=output)
                    )
        for lrc in ("[00:10.00]one\n[02:30.00]outside", "[00:75.00]invalid"):
            with self.mock_http(
                lambda req: httpx.Response(200, json=[{**TRACK, "syncedLyrics": lrc}])
            ):
                with self.assertRaises(ValueError):
                    await planner.plan_chorus(SONG, "Artist", AsyncMock())

    async def test_same_time_lines_cannot_be_split(self):
        item = {
            **TRACK,
            "syncedLyrics": "[00:10.00]first\n[00:10.00]translation\n[00:20.00]next",
        }
        with self.mock_http(lambda req: httpx.Response(200, json=[item])):
            with self.assertRaises(ValueError):
                await planner.plan_chorus(
                    SONG,
                    "Artist",
                    AsyncMock(
                        return_value='{"start_line":0,"end_line":0,"reason":"x"}'
                    ),
                )

    def test_credit_only_and_plain_text_are_not_reported_as_out_of_range_lrc(self):
        for lyrics in (
            "[00:00.00-1] 作词 : Creator\n[00:00.00-1] 作曲 : Creator\n",
            "[00:00.00]作词 : Creator\n[00:00.00]作曲 : Creator\n",
            "歌词正文没有任何时间标签",
        ):
            with (
                self.subTest(lyrics=lyrics[:30]),
                self.assertRaisesRegex(ValueError, "没有可用的带时间正文"),
            ):
                planner._match(SONG, "Artist", [{**TRACK, "syncedLyrics": lyrics}])
        for lyrics, message in (
            ("[00:75.00]invalid", "时间格式无效"),
            ("[00:10.00]first\n[02:30.00]outside", "超出当前音源范围"),
        ):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                planner._match(SONG, "Artist", [{**TRACK, "syncedLyrics": lyrics}])

    async def test_rate_limit_and_size(self):
        with self.mock_http(
            lambda req: httpx.Response(429, headers={"Retry-After": "17"})
        ):
            with self.assertRaisesRegex(ValueError, "17"):
                await planner.plan_chorus(SONG, "Artist", AsyncMock())
        with (
            patch.object(planner, "MAX_RESPONSE_BYTES", 12),
            self.mock_http(
                lambda req: httpx.Response(200, content=b"[" + b" " * 30 + b"]")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "过大"):
                await planner.plan_chorus(SONG, "Artist", AsyncMock())

    async def test_cancellation_propagates(self):
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        with self.mock_http(handler):
            task = asyncio.create_task(planner.plan_chorus(SONG, "Artist", AsyncMock()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_language_suffix_keeps_chinese_version(self):
        chinese = {**TRACK, "trackName": "Sample Song (中文)"}
        source = {**SONG, "title": "Official p01 【中】 Sample Song"}
        with self.mock_http(lambda req: httpx.Response(200, json=[TRACK, chinese])):
            result = await planner.plan_chorus(
                source,
                "",
                AsyncMock(
                    return_value='{"start_line":0,"end_line":1,"reason":"完整重复段"}'
                ),
            )
        self.assertEqual(result["track"], "Sample Song (中文)")

    async def test_local_english_alias_searches_base_title_without_losing_version(self):
        english = {
            **TRACK,
            "id": 17914639,
            "trackName": "Daisy Crown (English Ver.)",
            "artistName": "Empty old City",
            "albumName": "Daisy Crown",
            "duration": 196,
        }
        original_language = {
            **english,
            "id": 20058838,
            "trackName": "Daisy Crown",
            "duration": 196.4875,
        }
        bad_duration = {**english, "id": 35456172, "duration": 3}
        transcribe = AsyncMock()
        report = AsyncMock()

        async def handler(request):
            self.assertEqual(request.url.params["track_name"], "Daisy Crown")
            self.assertNotIn("artist_name", request.url.params)
            return httpx.Response(200, json=[original_language, bad_duration, english])

        for suffix in ("(英文版)", "（英文版）", "(English Ver.)", "(English Version)"):
            source = {
                "id": "9962445750674a95a7d019624dd2403a",
                "name": "Daisy Crown " + suffix,
                "duration": 196.48,
            }
            generate = AsyncMock(
                return_value='{"start_line":1,"end_line":3,"reason":"完整连续副歌"}'
            )
            with self.subTest(suffix=suffix), self.mock_http(handler):
                result = await planner.plan_chorus(
                    source, "", generate, transcribe=transcribe, report=report
                )
            self.assertEqual(
                result["source_url"], "https://lrclib.net/api/get/17914639"
            )
            self.assertEqual(result["timeline_source"], "lrclib")
            self.assertIn(source["name"], generate.await_args.args[0])
        transcribe.assert_not_awaited()
        self.assertEqual(
            {call.args[0] for call in report.await_args_list}, {"searching", "model"}
        )
        self.assertNotIn("first", str(report.await_args_list))

    async def test_known_language_suffix_does_not_strip_live_or_change_artist_album_checks(
        self,
    ):
        source = {**SONG, "name": "Sample Song (Live)"}
        query = AsyncMock(return_value=[TRACK])
        with patch.object(planner, "_search", query):
            with self.assertRaises(ValueError):
                await planner.plan_chorus(source, "", AsyncMock())
        query.assert_awaited_once_with("Sample Song (Live)", "")
        english = {**TRACK, "trackName": "Sample Song (English Ver.)"}
        source = {**SONG, "name": "Sample Song (英文版)", "artist": "Different singer"}
        with self.mock_http(lambda req: httpx.Response(200, json=[english])):
            with self.assertRaises(ValueError):
                await planner.plan_chorus(source, "", AsyncMock())
        source = {**source, "artist": "Artist", "album": "Missing album"}
        with self.mock_http(lambda req: httpx.Response(200, json=[english])):
            with self.assertRaisesRegex(ValueError, "专辑"):
                await planner.plan_chorus(source, "", AsyncMock())

    async def test_fallback_reports_the_original_reason_without_lyrics(self):
        reason = "同步歌词查询超时，请稍后重试或手动指定片段。"
        timeline = {
            "source": "local_whisper",
            "duration": 120,
            "segments": [
                {"start": 10, "end": 20, "text": "opening lyric line"},
                {"start": 21, "end": 30, "text": "first complete chorus line"},
                {"start": 31, "end": 45, "text": "last complete chorus line"},
                {"start": 47, "end": 55, "text": "following verse line"},
            ],
        }
        report = AsyncMock()
        generate = AsyncMock(
            return_value='{"start_line":1,"end_line":2,"reason":"完整连续副歌"}'
        )
        with patch.object(
            planner, "_search", AsyncMock(side_effect=ValueError(reason))
        ):
            result = await planner.plan_chorus(
                SONG,
                "",
                generate,
                transcribe=AsyncMock(return_value=timeline),
                report=report,
            )
        fallback = [
            call.args[1]
            for call in report.await_args_list
            if call.args[0] == "local_timeline"
        ]
        self.assertEqual(len(fallback), 1)
        self.assertIn(reason, fallback[0])
        self.assertIn("网易云", fallback[0])
        self.assertEqual(report.await_args_list[-1].args[0], "model")
        self.assertEqual(result["timeline_source"], "local_whisper")
        self.assertNotIn("opening lyric line", str(report.await_args_list))

    async def test_progress_failure_does_not_interrupt_and_cancellation_propagates(
        self,
    ):
        generate = AsyncMock(
            return_value='{"start_line":0,"end_line":1,"reason":"重复段"}'
        )
        with self.mock_http(lambda req: httpx.Response(200, json=[TRACK])):
            result = await planner.plan_chorus(
                SONG,
                "Artist",
                generate,
                report=AsyncMock(side_effect=RuntimeError("display failed")),
            )
        self.assertEqual(result["timeline_source"], "lrclib")
        query = AsyncMock()
        with patch.object(planner, "_search", query):
            with self.assertRaises(asyncio.CancelledError):
                await planner.plan_chorus(
                    SONG,
                    "",
                    AsyncMock(),
                    report=AsyncMock(side_effect=asyncio.CancelledError),
                )
        query.assert_not_awaited()

    async def test_mv_uses_recognized_audio_times_instead_of_guessed_offset(self):
        source = {
            **SONG,
            "duration": 209.408,
            "title": "p01【中】Sample Song",
            "source_url": "https://example.com/video",
        }
        transcript = {
            "source": "local_whisper",
            "duration": 209.408,
            "segments": [
                {"start": 12, "end": 20, "text": "opening lyric line"},
                {"start": 21, "end": 30, "text": "first complete chorus line"},
                {"start": 31, "end": 45, "text": "last complete chorus line"},
                {"start": 47, "end": 55, "text": "following verse line"},
            ],
        }
        transcribe = AsyncMock(return_value=transcript)
        generate = AsyncMock(
            return_value='{"start_line":1,"end_line":2,"reason":"完整连续副歌"}'
        )
        with self.mock_http(lambda req: httpx.Response(200, json=[TRACK])):
            result = await planner.plan_chorus(
                source, "", generate, transcribe=transcribe
            )
        transcribe.assert_awaited_once_with("zh")
        self.assertEqual(result["start_seconds"], 20.8)
        self.assertEqual(result["end_seconds"], 45.2)
        self.assertEqual(result["timeline_source"], "local_whisper")
        self.assertEqual(result["source_url"], source["source_url"])
        self.assertIn("本地识别", generate.await_args.args[0])

    async def test_audio_fallback_rejects_bad_or_sparse_timeline(self):
        generate = AsyncMock()
        for timeline in (
            {"duration": 120, "segments": []},
            {"duration": 121, "segments": [{}] * 4},
            {
                "duration": 120,
                "segments": [{"start": 10, "end": 20, "text": "repeated"}] * 4,
            },
        ):
            with self.mock_http(lambda req: httpx.Response(200, json=[])):
                with self.assertRaises(ValueError):
                    await planner.plan_chorus(
                        SONG, "", generate, transcribe=AsyncMock(return_value=timeline)
                    )
        generate.assert_not_awaited()

    async def test_fallback_cancellation_is_not_swallowed(self):
        transcribe = AsyncMock(side_effect=asyncio.CancelledError)
        with self.mock_http(lambda req: httpx.Response(200, json=[])):
            with self.assertRaises(asyncio.CancelledError):
                await planner.plan_chorus(SONG, "", AsyncMock(), transcribe=transcribe)

    async def test_unmarked_chinese_and_english_titles_use_lyric_language_evidence(
        self,
    ):
        for language, lyrics in (("zh", CHINESE_LYRICS), ("en", ENGLISH_LYRICS)):
            source = {**SONG, "language": language}
            item = {**TRACK, "syncedLyrics": lyrics}
            with (
                self.subTest(language=language),
                self.mock_http(lambda req: httpx.Response(200, json=[item])),
            ):
                result = await planner.plan_chorus(
                    source,
                    "Artist",
                    AsyncMock(
                        return_value='{"start_line":0,"end_line":1,"reason":"完整副歌"}'
                    ),
                )
            self.assertEqual(result["timeline_source"], "lrclib")
            self.assertEqual(result["lyric_metadata"]["language"], language)
        self.netease.assert_not_awaited()

    async def test_explicit_language_wins_but_wrong_or_uncertain_lyrics_are_rejected(
        self,
    ):
        source = {**SONG, "title": "旧标题【中】", "language": "en"}
        english = {
            **TRACK,
            "trackName": "Sample Song (English Ver.)",
            "syncedLyrics": ENGLISH_LYRICS,
        }
        with self.mock_http(lambda req: httpx.Response(200, json=[english])):
            result = await planner.plan_chorus(
                source,
                "",
                AsyncMock(return_value='{"start_line":0,"end_line":1,"reason":"副歌"}'),
            )
        self.assertEqual(result["lyric_metadata"]["language"], "en")
        for lyrics in (
            CHINESE_LYRICS,
            "[00:10.00]あなたの声が聞こえる\n[00:20.00]いつまでもここにいる",
            LYRICS,
        ):
            with (
                self.subTest(lyrics=lyrics[:15]),
                self.assertRaisesRegex(ValueError, "语言"),
            ):
                planner._match(
                    {**SONG, "language": "en"}, "", [{**TRACK, "syncedLyrics": lyrics}]
                )
        with self.assertRaisesRegex(ValueError, "语言标记"):
            planner._match({**SONG, "language": "invalid"}, "", [TRACK])

    def test_collaborator_members_and_album_labels_keep_identity_checks(self):
        joint = {
            **TRACK,
            "artistName": "Artist / Guest",
            "artistNames": ["Artist", "Guest"],
            "albumName": "Album - Single",
        }
        chosen, _, _ = planner._match(
            {**SONG, "artist": "Artist", "album": "Album - EP"}, "", [joint]
        )
        self.assertIs(chosen, joint)
        self.assertIs(planner._match(SONG, "Artist & Guest", [joint])[0], joint)
        with self.assertRaisesRegex(ValueError, "歌手"):
            planner._match(SONG, "Other singer", [joint])
        with self.assertRaisesRegex(ValueError, "歌手"):
            planner._match(SONG, "AC", [{**TRACK, "artistName": "AC/DC"}])
        with self.assertRaisesRegex(ValueError, "专辑"):
            planner._match({**SONG, "album": "Another album"}, "", [joint])

    def test_diagnostics_distinguish_missing_lyrics_duration_artist_and_ambiguity(self):
        cases = (
            ([], "没有搜到"),
            ([{**TRACK, "syncedLyrics": None}], "LRC"),
            ([{**TRACK, "duration": 130}], "时长不符"),
            ([{**TRACK, "artistName": "Other"}], "歌手"),
            ([TRACK, {**TRACK, "id": 78, "artistName": "Other"}], "歧义"),
        )
        for candidates, message in cases:
            artist = "Artist" if message == "歌手" else ""
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                planner._match(SONG, artist, candidates)
        with self.assertRaisesRegex(ValueError, "录音版本"):
            planner._match({**SONG, "title": "明确翻唱版本"}, "", [TRACK])

    async def test_netease_exact_song_url_fetches_public_metadata_and_original_lrc(
        self,
    ):
        requests = []

        async def handler(request):
            requests.append(request)
            self.assertNotIn("cookie", request.headers)
            if request.url.path == "/api/song/detail":
                self.assertEqual(request.url.params["id"], "77")
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "songs": [
                            {
                                "id": 77,
                                "name": "Sample Song",
                                "duration": 120000,
                                "artists": [{"name": "Artist"}],
                                "album": {"name": "Album"},
                            }
                        ],
                    },
                    headers={"set-cookie": "unused=must-not-be-forwarded"},
                )
            self.assertEqual(request.url.path, "/api/song/lyric")
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "lrc": {"lyric": CHINESE_LYRICS},
                    "tlyric": {"lyric": "translation must not be used"},
                },
            )

        source = {
            **SONG,
            "source_url": "https://music.163.com/#/song?id=77",
            "language": "zh",
        }
        with self.mock_http(handler):
            candidates = await self.real_netease(source, "Artist", "Sample Song")
        self.assertEqual(len(requests), 2)
        self.assertTrue(candidates[0]["exactSource"])
        self.assertEqual(candidates[0]["syncedLyrics"], CHINESE_LYRICS)
        self.assertEqual(candidates[0]["duration"], 120)

    async def test_netease_standard_cloudsearch_and_program_id_is_not_song_id(self):
        seen = []

        async def handler(request):
            seen.append(request.url.path)
            if request.url.path == "/api/cloudsearch/pc":
                self.assertEqual(request.method, "POST")
                self.assertEqual(
                    parse_qs(request.content.decode())["s"], ["Artist Sample Song"]
                )
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "result": {
                            "songs": [
                                {
                                    "id": 77,
                                    "name": "Sample Song",
                                    "dt": 120000,
                                    "ar": [{"name": "Artist"}],
                                    "al": {"name": "Album"},
                                }
                            ]
                        },
                    },
                )
            self.assertEqual(request.url.path, "/api/song/lyric")
            return httpx.Response(
                200, json={"code": 200, "lrc": {"lyric": CHINESE_LYRICS}}
            )

        source = {
            **SONG,
            "source_url": "https://music.163.com/m/program?id=909102206",
            "language": "zh",
        }
        self.assertIsNone(planner._netease_song_id(source["source_url"]))
        self.assertIsNone(planner._netease_song_id("https://music.163.com/album?id=77"))
        self.assertIsNone(
            planner._netease_song_id("https://music.163.com.evil.test/song?id=77")
        )
        with self.mock_http(handler):
            candidates = await self.real_netease(source, "Artist", "Sample Song")
        self.assertEqual(seen, ["/api/cloudsearch/pc", "/api/song/lyric"])
        self.assertFalse(candidates[0]["exactSource"])

    async def test_netease_search_uses_stored_artist_when_tool_argument_is_absent(self):
        async def handler(request):
            if request.url.path == "/api/cloudsearch/pc":
                self.assertEqual(
                    parse_qs(request.content.decode())["s"], ["Artist Sample Song"]
                )
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "result": {
                            "songs": [
                                {
                                    "id": 77,
                                    "name": "Sample Song",
                                    "dt": 120000,
                                    "ar": [{"name": "Artist"}],
                                    "al": {"name": "Album"},
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(
                200, json={"code": 200, "lrc": {"lyric": CHINESE_LYRICS}}
            )

        with self.mock_http(handler):
            candidates = await self.real_netease(
                {**SONG, "artist": "Artist"}, "", "Sample Song"
            )
        self.assertEqual(candidates[0]["artistName"], "Artist")

    async def test_netease_fallback_keeps_its_page_url_and_metadata(self):
        self.netease.side_effect = None
        self.netease.return_value = [
            {**TRACK, "lyricsSource": "netease", "artistNames": ["Artist"]}
        ]
        with self.mock_http(lambda req: httpx.Response(200, json=[])):
            result = await planner.plan_chorus(
                SONG,
                "Artist",
                AsyncMock(
                    return_value='{"start_line":0,"end_line":1,"reason":"完整副歌"}'
                ),
            )
        self.assertEqual(result["timeline_source"], "netease")
        self.assertEqual(result["source_url"], "https://music.163.com/song?id=77")
        self.assertEqual(result["lyric_metadata"]["provider"], "netease")

    async def test_netease_access_limits_stop_without_retry(self):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(429)

        with self.mock_http(handler):
            with self.assertRaisesRegex(ValueError, "访问限制"):
                await self.real_netease(SONG, "Artist", "Sample Song")
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
