"""Local compatibility for yt-dlp issue #17894; retire after the upstream fix.

Uses the public metadata endpoint already queried by the upstream extractor.
Reference: https://github.com/yt-dlp/yt-dlp/pull/17898
"""

from yt_dlp.extractor.bilibili import BiliBiliIE as _BiliBiliIE
from yt_dlp.extractor.bilibili import BiliBiliBangumiIE
from yt_dlp.utils import ExtractorError, traverse_obj, url_or_none


class BiliBiliIE(_BiliBiliIE, plugin_name="breeze_compat"):
    _VALID_URL = _BiliBiliIE._VALID_URL.replace(
        "bilibili\\.com/", "bilibili\\.com/(?:[a-z]{2}(?:-Hans)?/)?", 1
    )

    def _real_extract(self, url):
        video_id, prefix = self._match_valid_url(url).group("id", "prefix")
        self._breeze_video_query = {"platform": "web"}
        if prefix.upper() == "BV":
            self._breeze_video_query["bvid"] = "BV" + video_id
        else:
            self._breeze_video_query["aid"] = video_id
        return super()._real_extract(url)

    def _search_json(self, start_pattern, string, name, video_id, **kwargs):
        result = super()._search_json(start_pattern, string, name, video_id, **kwargs)
        if name != "initial state" or result:
            return result
        risk = super()._search_json(
            r"\bwindow\._riskdata_\s*=", string, "risk", video_id, default={}
        )
        if isinstance(risk, dict) and risk.get("v_voucher"):
            raise ExtractorError(
                "You have exceeded the rate limit. Try again later", expected=True
            )
        headers = {
            **self.geo_verification_headers(),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        detail = self._download_json(
            "https://api.bilibili.com/x/web-interface/wbi/view/detail",
            video_id,
            note="Downloading video details (Breeze compatibility)",
            query=self._sign_wbi(dict(self._breeze_video_query), video_id),
            headers=headers,
        )
        if not isinstance(detail, dict) or detail.get("code") != 0:
            code = detail.get("code") if isinstance(detail, dict) else "unknown"
            message = detail.get("message", "") if isinstance(detail, dict) else ""
            raise ExtractorError(
                f"Bilibili video metadata unavailable (code {code}): {message}",
                expected=True,
            )
        view = traverse_obj(detail, ("data", "View", {dict}))
        if not view:
            raise ExtractorError("Bilibili video metadata is missing", expected=True)
        redirect = url_or_none(view.get("redirect_url"))
        if redirect and BiliBiliBangumiIE.suitable(redirect):
            # Keep upstream's Bangumi redirect handling.
            return result
        for key in ("bvid", "aid"):
            requested = self._breeze_video_query.get(key)
            if requested is not None and str(view.get(key)) != str(requested):
                raise ExtractorError("Bilibili returned a different video", expected=True)
        if not view.get("bvid") or not view.get("cid") or not view.get("title"):
            raise ExtractorError("Bilibili video metadata is incomplete", expected=True)
        # Reuse upstream's parts, formats, login and playback restriction checks.
        return {
            "videoData": view,
            "upData": view.get("owner") or {},
            "tags": traverse_obj(detail, ("data", "Tags", {list})) or [],
        }
