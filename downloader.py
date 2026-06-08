"""
downloader.py
─────────────
بدون نیاز به Spotify API.
- لینک اسپاتیفای  →  oEmbed (عنوان) + ytsearch5 در یوتیوب
- جستجو با اسم    →  yt-dlp ytsearch مستقیم
"""

import os
import re
import asyncio
import tempfile
import logging
import urllib.request
import urllib.parse
import json
from pathlib import Path
from typing import Optional

import yt_dlp

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "tgbot_music"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# فرمت صوتی — استریم‌های HTTP بدون DRM اولویت دارن
_SAFE_FORMAT = (
    "bestaudio[protocol^=http][vcodec=none]"
    "/bestaudio[protocol^=https][vcodec=none]"
    "/bestaudio[ext=webm]"
    "/bestaudio[ext=m4a]"
    "/bestaudio/best"
)

# ── کوکی یوتیوب (برای سرورهای cloud که IP‌شون بلاکه) ─────────────────────────
_YT_COOKIE_FILE: Optional[str] = None

def _init_yt_cookies() -> None:
    """اگه YT_COOKIES_B64 ست شده، کوکی‌ها رو به فایل موقت decode می‌کنه"""
    global _YT_COOKIE_FILE
    import base64
    raw = os.environ.get("YT_COOKIES_B64", "").strip()
    if not raw:
        return
    try:
        content = base64.b64decode(raw).decode("utf-8")
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f.write(content)
        f.close()
        _YT_COOKIE_FILE = f.name
        logger.info(f"YouTube cookies loaded from env → {_YT_COOKIE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to init YT cookies: {e}")

_init_yt_cookies()


def _yt_base_opts() -> dict:
    """گزینه‌های پایه یوتیوب: چند player client + کوکی اگه داریم"""
    opts: dict = {
        "extractor_args": {
            # android_music روی cloud IP استریم HTTP قابل دانلود برمی‌گردونه
            # tv_embedded و mweb fallback های قابل اعتماد هستن
            "youtube": {"player_client": ["android_music", "tv_embedded", "mweb"]}
        },
    }
    if _YT_COOKIE_FILE:
        opts["cookiefile"] = _YT_COOKIE_FILE
    return opts


def _embed_track_metadata(spotify_url: str) -> dict:
    """
    گرفتن متادیتای دقیق آهنگ از صفحه embed اسپاتیفای.
    عنوان، اسم واقعی خواننده، و تصویر کاور.
    """
    try:
        import requests as req_lib
        m = re.search(r'spotify\.com/track/([A-Za-z0-9]+)', spotify_url)
        if not m:
            return {}
        track_id = m.group(1)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        r = req_lib.get(f"https://open.spotify.com/embed/track/{track_id}", headers=headers, timeout=10)
        r.raise_for_status()
        next_m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if not next_m:
            return {}
        entity = json.loads(next_m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]

        title = entity.get("title", "")
        artists = entity.get("artists") or []
        artist = ", ".join(a["name"] for a in artists if a.get("name"))
        cover_url = ""
        vi = entity.get("visualIdentity") or {}
        images = vi.get("image") or []
        if images:
            # بزرگترین تصویر
            best = max(images, key=lambda x: x.get("maxWidth") or 0)
            cover_url = best.get("url", "")

        return {"title": title, "artist": artist, "album": "", "cover_url": cover_url, "lyrics": ""}
    except Exception as e:
        logger.warning(f"embed track metadata failed: {e}")
        return {}


class Downloader:

    # ── جستجو ────────────────────────────────────────────────────────────────
    async def search(self, query: str, limit: int = 5) -> list:
        loop = asyncio.get_event_loop()

        def _do():
            opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                results = []
                for e in (info.get("entries") or []):
                    url = e.get("url") or e.get("webpage_url") or ""
                    if not url.startswith("http"):
                        url = f"https://www.youtube.com/watch?v={e.get('id','')}"
                    results.append({
                        "title": e.get("title", query),
                        "uploader": e.get("uploader", ""),
                        "url": url,
                        "duration": e.get("duration", 0),
                    })
                return results

        return await loop.run_in_executor(None, _do)

    # ── دانلود یک آهنگ ───────────────────────────────────────────────────────
    async def download_one(self, url: str, quality: str, is_spotify: bool, prefetch: dict = None) -> dict:
        loop = asyncio.get_event_loop()
        if is_spotify:
            return await loop.run_in_executor(None, self._spotify_download, url, quality, prefetch)
        else:
            return await loop.run_in_executor(None, self._try_download_url, url, quality, prefetch or {})

    def _spotify_download(self, spotify_url: str, quality: str, prefetch: dict = None) -> dict:
        """متادیتا از embed page اسپاتیفای، دانلود از یوتیوب"""
        metadata = prefetch if prefetch else _embed_track_metadata(spotify_url)

        if not metadata.get("title"):
            # fallback: فقط ID
            m = re.search(r"/track/([A-Za-z0-9]+)", spotify_url)
            metadata = {"title": m.group(1) if m else "track", "artist": "", "album": "", "cover_url": "", "lyrics": ""}

        title = metadata['title']
        artist = metadata.get('artist', '')

        # چند حالت جستجو: با audio، بدون audio، فقط title
        queries = []
        if artist:
            queries.append(f"{title} {artist} audio")
            queries.append(f"{title} {artist}")
            queries.append(f"{title} official audio")
        else:
            queries.append(f"{title} audio")
            queries.append(title)

        for q in queries:
            logger.info(f"Searching YouTube for: {q}")
            result = self._search_and_download(q, quality, metadata)
            if result["success"]:
                return result
            # اگه فقط نتیجه‌ای پیدا نشد (نه خطای دانلود) query بعدی رو امتحان کن
            if "نتیجه‌ای پیدا نشد" in result.get("error", "") or "یوتیوب و ساندکلاد" in result.get("error", ""):
                logger.info(f"No results for '{q}', trying next query...")
                continue
            return result

        return {"success": False, "error": "با همه روش‌های جستجو نتیجه‌ای پیدا نشد"}

    def _search_and_download(self, query: str, quality: str, metadata: dict) -> dict:
        """جستجوی ytsearch5 و تلاش روی هر نتیجه — اگه YouTube بلاک بود SoundCloud fallback"""
        # ── جستجوی YouTube ────────────────────────────────────────────────────
        flat_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            **_yt_base_opts(),
        }
        entries = []
        yt_blocked = False
        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(f"ytsearch5:{query}", download=False)
                entries = info.get("entries") or []
        except Exception as e:
            logger.warning(f"YouTube search failed: {e}")
            yt_blocked = True

        if not yt_blocked and entries:
            last_err = "خطای ناشناخته"
            all_blocked = True
            for entry in entries:
                vid_id = entry.get("id", "")
                if not vid_id:
                    continue
                yt_url = f"https://www.youtube.com/watch?v={vid_id}"
                result = self._try_download_url(yt_url, quality, metadata)
                if result["success"]:
                    return result
                last_err = result.get("error", last_err)
                logger.warning(f"Skipping {vid_id}: {last_err}")
                # این خطاها نشونه‌ی بلاک شدن IP روی cloud هستن — باید به SoundCloud fallback بریم
                _block_signs = ("sign in", "bot", "format is not available", "requested format", "not available")
                if not any(kw in last_err.lower() for kw in _block_signs):
                    all_blocked = False

            if not all_blocked:
                return {"success": False, "error": f"همه نتایج ناموفق بودن: {last_err}"}
            logger.warning("YouTube blocked on this IP — trying SoundCloud fallback")

        # ── SoundCloud fallback ───────────────────────────────────────────────
        logger.info(f"Trying SoundCloud for: {query}")
        sc_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        try:
            with yt_dlp.YoutubeDL(sc_opts) as ydl:
                sc_info = ydl.extract_info(f"scsearch5:{query}", download=False)
                sc_entries = sc_info.get("entries") or []
        except Exception as e:
            logger.error(f"SoundCloud search failed: {e}")
            return {"success": False, "error": "یوتیوب و ساندکلاد هر دو در دسترس نیستن"}

        if not sc_entries:
            return {"success": False, "error": "نتیجه‌ای پیدا نشد (نه YouTube، نه SoundCloud)"}

        last_err = "خطای ناشناخته"
        for entry in sc_entries:
            sc_url = entry.get("url") or entry.get("webpage_url", "")
            if not sc_url:
                continue
            result = self._try_download_url(sc_url, quality, metadata)
            if result["success"]:
                logger.info("SoundCloud download succeeded")
                return result
            last_err = result.get("error", last_err)

        # ── YouTube Music fallback ────────────────────────────────────────────
        logger.info(f"Trying YouTube Music for: {query}")
        ytm_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            **_yt_base_opts(),
        }
        try:
            with yt_dlp.YoutubeDL(ytm_opts) as ydl:
                ytm_info = ydl.extract_info(f"https://music.youtube.com/search?q={urllib.parse.quote(query)}", download=False)
                ytm_entries = ytm_info.get("entries") or [] if ytm_info else []
        except Exception as e:
            logger.warning(f"YouTube Music search failed: {e}")
            ytm_entries = []

        if not ytm_entries:
            try:
                with yt_dlp.YoutubeDL(ytm_opts) as ydl:
                    ytm_info = ydl.extract_info(f"ytmsearch5:{query}", download=False)
                    ytm_entries = ytm_info.get("entries") or [] if ytm_info else []
            except Exception as e:
                logger.warning(f"YouTube Music ytmsearch failed: {e}")
                ytm_entries = []

        for entry in ytm_entries[:5]:
            vid_id = entry.get("id") or entry.get("videoId", "")
            if not vid_id:
                continue
            yt_url = f"https://www.youtube.com/watch?v={vid_id}"
            result = self._try_download_url(yt_url, quality, metadata)
            if result["success"]:
                logger.info("YouTube Music download succeeded")
                return result

        # ── RadioJavan fallback (موزیک فارسی) ────────────────────────────────
        logger.info(f"Trying RadioJavan for: {query}")
        rj_result = self._radiojavan_download(query, quality, metadata)
        if rj_result["success"]:
            return rj_result

        return {"success": False, "error": "نتیجه‌ای پیدا نشد (YouTube، SoundCloud، YouTube Music، RadioJavan)"}

    def _radiojavan_download(self, query: str, quality: str, metadata: dict) -> dict:
        """جستجو در RadioJavan و دانلود مستقیم از لینک MP3"""
        import requests as req_lib
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.radiojavan.com/",
        }
        try:
            r = req_lib.get(
                f"https://www.radiojavan.com/search?query={urllib.parse.quote(query)}&type=mp3",
                headers=headers, timeout=12
            )
            r.raise_for_status()
            # استخراج لینک‌های آهنگ از HTML
            song_slugs = re.findall(r'href=["\'](?:/mp3s/mp3/|/song/)([^"\'?]+)["\']', r.text)
            if not song_slugs:
                logger.info("RadioJavan: no songs found in search results")
                return {"success": False, "error": "RadioJavan: نتیجه‌ای پیدا نشد"}
        except Exception as e:
            logger.warning(f"RadioJavan search request failed: {e}")
            return {"success": False, "error": f"RadioJavan: خطا در جستجو"}

        seen = set()
        for slug in song_slugs:
            if slug in seen:
                continue
            seen.add(slug)
            rj_url = f"https://www.radiojavan.com/mp3s/mp3/{slug}"
            logger.info(f"RadioJavan: trying {rj_url}")
            result = self._try_download_url(rj_url, quality, metadata)
            if result["success"]:
                logger.info(f"RadioJavan download succeeded: {slug}")
                return result
            if len(seen) >= 3:
                break

        return {"success": False, "error": "RadioJavan: دانلود ناموفق بود"}

    def _try_download_url(self, url: str, quality: str, metadata: dict) -> dict:
        """دانلود یک URL مشخص با فرمت امن (بدون DRM)"""
        audio_quality = "320" if quality == "320" else "128"
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", metadata.get("title") or "audio")
        out_tmpl = str(DOWNLOAD_DIR / f"{safe_title}_%(id)s.%(ext)s")

        ydl_opts = {
            "format": _SAFE_FORMAT,
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": False,
            **_yt_base_opts(),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }],
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    return {"success": False, "error": "اطلاعاتی دریافت نشد"}
                if "entries" in info:
                    info = info["entries"][0]

                # پیدا کردن فایل MP3
                downloaded_path = self._find_mp3(ydl, info, safe_title)
                if not downloaded_path:
                    return {"success": False, "error": "فایل MP3 ساخته نشد"}

                # اگه متادیتا نداشتیم از yt-dlp بگیر
                if not metadata.get("title"):
                    title = info.get("title", "Unknown")
                    parts = title.split(" - ", 1)
                    metadata = {
                        "title": parts[1].strip() if len(parts) == 2 else title,
                        "artist": parts[0].strip() if len(parts) == 2 else info.get("uploader", ""),
                        "album": "",
                        "cover_url": info.get("thumbnail", ""),
                        "lyrics": "",
                    }

        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            if "DRM" in err:
                return {"success": False, "error": "DRM"}
            return {"success": False, "error": err[:200]}
        except Exception as e:
            return {"success": False, "error": str(e)[:200]}

        # تگ + کاور
        thumb_path = None
        if metadata.get("cover_url"):
            thumb_path = self._download_cover(metadata["cover_url"], safe_title)
        self._embed_tags(downloaded_path, metadata, thumb_path)

        return {
            "success": True,
            "path": downloaded_path,
            "thumb": thumb_path,
            "title": metadata.get("title", ""),
            "artist": metadata.get("artist", ""),
            "album": metadata.get("album", ""),
            "lyrics": metadata.get("lyrics", ""),
        }

    def _find_mp3(self, ydl, info: dict, safe_title: str) -> Optional[str]:
        base = Path(ydl.prepare_filename(info)).stem
        for f in DOWNLOAD_DIR.glob(f"{base}*.mp3"):
            return str(f)
        vid_id = info.get("id", "")
        for f in DOWNLOAD_DIR.glob(f"*{vid_id}*.mp3"):
            return str(f)
        return None

    # ── لیریک ────────────────────────────────────────────────────────────────
    async def fetch_lyrics(self, title: str, artist: str) -> str:
        loop = asyncio.get_event_loop()

        def _do():
            try:
                import syncedlyrics
                lrc = syncedlyrics.search(f"{title} {artist}".strip(), plain_only=True)
                if lrc:
                    clean = re.sub(r'\[\d+:\d+\.\d+\]', '', lrc).strip()
                    return clean[:4000]
            except Exception as e:
                logger.warning(f"Lyrics fetch failed: {e}")
            return ""

        return await loop.run_in_executor(None, _do)

    # ── آلبوم / پلی‌لیست ─────────────────────────────────────────────────────
    async def get_collection_tracks(self, url: str, kind: str) -> list[dict]:
        """
        لیست کامل آهنگ‌های پلی‌لیست یا آلبوم از صفحه embed اسپاتیفای.
        از offset پیجیناسیون می‌کنه تا همه آهنگ‌ها رو بگیره.
        هر آیتم: {url, title, artist, cover_url}
        """
        loop = asyncio.get_event_loop()
        sp_id = re.search(r'spotify\.com/(?:album|playlist)/([A-Za-z0-9]+)', url)
        if not sp_id:
            return []
        sp_id = sp_id.group(1)

        def _do():
            import requests as req_lib
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
            tracks = []
            seen_uris = set()
            offset = 0

            while True:
                embed_url = f"https://open.spotify.com/embed/{kind}/{sp_id}?offset={offset}"
                r = req_lib.get(embed_url, headers=headers, timeout=12)
                r.raise_for_status()

                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
                if not m:
                    break

                data = json.loads(m.group(1))
                track_list = data["props"]["pageProps"]["state"]["data"]["entity"].get("trackList", [])

                new_this_page = 0
                for t in track_list:
                    uri = t.get("uri", "")
                    parts = uri.split(":")
                    if len(parts) != 3 or parts[1] != "track":
                        continue
                    if uri in seen_uris:
                        continue
                    seen_uris.add(uri)
                    new_this_page += 1

                    track_id = parts[2]
                    artist = t.get("subtitle", "")
                    cover_url = ""
                    vi = t.get("visualIdentity") or {}
                    imgs = vi.get("image") or []
                    if imgs:
                        best = max(imgs, key=lambda x: x.get("maxWidth") or 0)
                        cover_url = best.get("url", "")

                    tracks.append({
                        "url": f"https://open.spotify.com/track/{track_id}",
                        "title": t.get("title", ""),
                        "artist": artist,
                        "cover_url": cover_url,
                        "album": "",
                        "lyrics": "",
                    })

                logger.info(f"offset={offset}: got {len(track_list)} items, {new_this_page} new — total {len(tracks)}")

                # اگه هیچ آهنگ جدیدی نداشت یعنی به آخر رسیدیم
                if new_this_page == 0:
                    break

                offset += 50

            logger.info(f"Total tracks fetched from {kind} {sp_id}: {len(tracks)}")
            return tracks

        return await loop.run_in_executor(None, _do)

    # ── ابزارها ──────────────────────────────────────────────────────────────
    def _download_cover(self, cover_url: str, safe_name: str) -> Optional[str]:
        try:
            path = str(DOWNLOAD_DIR / f"{safe_name}_cover.jpg")
            req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r, open(path, "wb") as f:
                f.write(r.read())
            return path
        except Exception as e:
            logger.warning(f"Cover download failed: {e}")
            return None

    def _embed_tags(self, mp3_path: str, meta: dict, thumb_path: Optional[str]):
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT
            audio = MP3(mp3_path, ID3=ID3)
            try:
                audio.add_tags()
            except Exception:
                pass
            t = audio.tags
            if meta.get("title"):  t.add(TIT2(encoding=3, text=meta["title"]))
            if meta.get("artist"): t.add(TPE1(encoding=3, text=meta["artist"]))
            if meta.get("album"):  t.add(TALB(encoding=3, text=meta["album"]))
            if meta.get("lyrics"): t.add(USLT(encoding=3, lang="eng", desc="", text=meta["lyrics"]))
            if thumb_path and Path(thumb_path).exists():
                with open(thumb_path, "rb") as img:
                    t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img.read()))
            audio.save()
        except Exception as e:
            logger.warning(f"Tag embed failed: {e}")
