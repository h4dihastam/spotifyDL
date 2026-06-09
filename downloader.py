"""
downloader.py
─────────────
استراتژی دانلود (سریع‌ترین اول):
  1. SoundCloud  — بدون fragmentation، single HTTP stream، سریع
  2. YouTube     — fallback اگه SoundCloud نداد (فقط tv_embedded)
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

_SAFE_FORMAT = "bestaudio/best"

# ── کوکی یوتیوب ───────────────────────────────────────────────────────────────
_YT_COOKIE_FILE: Optional[str] = None

def _init_yt_cookies() -> None:
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
        logger.info(f"YouTube cookies loaded → {_YT_COOKIE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to init YT cookies: {e}")

_init_yt_cookies()


def _yt_opts_base() -> dict:
    """گزینه‌های پایه yt-dlp برای همه دانلودها"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if _YT_COOKIE_FILE:
        opts["cookiefile"] = _YT_COOKIE_FILE
    return opts


def _yt_opts_youtube() -> dict:
    """گزینه‌های اضافه برای YouTube — فقط tv_embedded (کمترین fragmentation)"""
    return {
        **_yt_opts_base(),
        "extractor_args": {
            "youtube": {"player_client": ["tv_embedded"]}
        },
        "concurrent_fragment_downloads": 5,
    }


# ── اسپاتیفای anonymous token ─────────────────────────────────────────────────
_sp_token: Optional[str] = None
_sp_token_expiry: float = 0.0

def _get_spotify_token() -> Optional[str]:
    """
    توکن anonymous اسپاتیفای (بدون نیاز به API Key).
    کش می‌شه تا expire بشه.
    """
    import time
    import requests as req_lib
    global _sp_token, _sp_token_expiry

    if _sp_token and time.time() < _sp_token_expiry - 30:
        return _sp_token

    try:
        r = req_lib.get(
            "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        _sp_token = data.get("accessToken")
        expiry_ms = data.get("accessTokenExpirationTimestampMs", 0)
        _sp_token_expiry = expiry_ms / 1000 if expiry_ms else time.time() + 3600
        return _sp_token
    except Exception as e:
        logger.warning(f"Spotify token fetch failed: {e}")
        return None


def _embed_track_metadata(spotify_url: str) -> dict:
    """متادیتای آهنگ از embed page اسپاتیفای — بدون API key"""
    try:
        import requests as req_lib
        m = re.search(r'spotify\.com/track/([A-Za-z0-9]+)', spotify_url)
        if not m:
            return {}
        track_id = m.group(1)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = req_lib.get(f"https://open.spotify.com/embed/track/{track_id}", headers=headers, timeout=10)
        r.raise_for_status()
        nm = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if not nm:
            return {}
        entity = json.loads(nm.group(1))["props"]["pageProps"]["state"]["data"]["entity"]

        title = entity.get("title", "")
        artists = entity.get("artists") or []
        artist = ", ".join(a["name"] for a in artists if a.get("name"))

        cover_url = ""
        vi = entity.get("visualIdentity") or {}
        imgs = vi.get("image") or []
        if imgs:
            best = max(imgs, key=lambda x: x.get("maxWidth") or 0)
            cover_url = best.get("url", "")

        duration_ms = entity.get("duration", 0) or 0
        duration_sec = int(duration_ms / 1000) if duration_ms else 0

        return {"title": title, "artist": artist, "album": "", "cover_url": cover_url, "lyrics": "", "duration_sec": duration_sec}
    except Exception as e:
        logger.warning(f"embed track metadata failed: {e}")
        return {}


class Downloader:

    # ── جستجو ────────────────────────────────────────────────────────────────
    async def search(self, query: str, limit: int = 5) -> list:
        loop = asyncio.get_event_loop()

        def _do():
            opts = {**_yt_opts_base(), "extract_flat": True, "skip_download": True}
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
        metadata = prefetch if prefetch else _embed_track_metadata(spotify_url)
        if not metadata.get("title"):
            m = re.search(r"/track/([A-Za-z0-9]+)", spotify_url)
            metadata = {"title": m.group(1) if m else "track", "artist": "", "album": "", "cover_url": "", "lyrics": ""}

        search_q = f"{metadata['title']} {metadata.get('artist', '')}".strip()
        logger.info(f"Downloading: {search_q}")
        return self._search_and_download(search_q, quality, metadata)

    def _search_and_download(self, query: str, quality: str, metadata: dict) -> dict:
        """
        استراتژی دانلود:
          1. SoundCloud  — سریع، single stream، بدون fragmentation
          2. YouTube     — fallback با tv_embedded
        """
        expected_duration = metadata.get("duration_sec", 0) or 0

        def _is_preview(dur: float) -> bool:
            if dur <= 0:
                return False
            if dur < 60:
                return True
            if expected_duration > 0 and dur < expected_duration * 0.7:
                return True
            return False

        # ── مرحله ۱: SoundCloud ──────────────────────────────────────────────
        logger.info(f"SoundCloud search: {query}")
        sc_flat_opts = {**_yt_opts_base(), "extract_flat": True, "skip_download": True}
        sc_entries = []
        try:
            with yt_dlp.YoutubeDL(sc_flat_opts) as ydl:
                info = ydl.extract_info(f"scsearch5:{query}", download=False)
                sc_entries = info.get("entries") or []
        except Exception as e:
            logger.warning(f"SoundCloud search failed: {e}")

        for entry in sc_entries:
            sc_url = entry.get("url") or entry.get("webpage_url", "")
            if not sc_url:
                continue
            dur = entry.get("duration") or 0
            if _is_preview(dur):
                logger.warning(f"SC preview skipped ({dur}s)")
                continue
            result = self._try_download_url(sc_url, quality, metadata)
            if result["success"]:
                file_size = Path(result["path"]).stat().st_size if result.get("path") else 0
                if expected_duration > 60 and file_size < 500_000:
                    logger.warning(f"SC file too small ({file_size}B) — preview?")
                    try: Path(result["path"]).unlink(missing_ok=True)
                    except: pass
                    continue
                logger.info("SoundCloud download OK")
                return result

        # ── مرحله ۲: YouTube fallback ────────────────────────────────────────
        logger.info(f"YouTube fallback: {query}")
        yt_flat_opts = {**_yt_opts_youtube(), "extract_flat": True, "skip_download": True}
        yt_entries = []
        try:
            with yt_dlp.YoutubeDL(yt_flat_opts) as ydl:
                info = ydl.extract_info(f"ytsearch3:{query}", download=False)
                yt_entries = info.get("entries") or []
        except Exception as e:
            logger.warning(f"YouTube search failed: {e}")

        last_err = "هیچ منبعی در دسترس نیست"
        for entry in yt_entries:
            vid_id = entry.get("id", "")
            if not vid_id:
                continue
            result = self._try_download_url(
                f"https://www.youtube.com/watch?v={vid_id}", quality, metadata
            )
            if result["success"]:
                logger.info("YouTube download OK")
                return result
            last_err = result.get("error", last_err)
            logger.warning(f"YT {vid_id} failed: {last_err}")

        return {"success": False, "error": f"دانلود ناموفق: {last_err}"}

    def _try_download_url(self, url: str, quality: str, metadata: dict) -> dict:
        audio_quality = "320" if quality == "320" else "128"
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", metadata.get("title") or "audio")
        out_tmpl = str(DOWNLOAD_DIR / f"{safe_title}_%(id)s.%(ext)s")

        is_youtube = "youtube.com" in url or "youtu.be" in url
        base_opts = _yt_opts_youtube() if is_youtube else _yt_opts_base()

        ydl_opts = {
            **base_opts,
            "format": _SAFE_FORMAT,
            "outtmpl": out_tmpl,
            "noplaylist": True,
            "ignoreerrors": False,
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

                downloaded_path = self._find_mp3(ydl, info, safe_title)
                if not downloaded_path:
                    return {"success": False, "error": "فایل MP3 ساخته نشد"}

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
            return {"success": False, "error": err[:200]}
        except Exception as e:
            return {"success": False, "error": str(e)[:200]}

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
                    return re.sub(r'\[\d+:\d+\.\d+\]', '', lrc).strip()[:4000]
            except Exception as e:
                logger.warning(f"Lyrics fetch failed: {e}")
            return ""

        return await loop.run_in_executor(None, _do)

    # ── آلبوم / پلی‌لیست ─────────────────────────────────────────────────────
    async def get_collection_tracks(self, url: str, kind: str) -> list[dict]:
        loop = asyncio.get_event_loop()
        sp_id = re.search(r'spotify\.com/(?:album|playlist)/([A-Za-z0-9]+)', url)
        if not sp_id:
            return []
        sp_id = sp_id.group(1)

        def _do():
            import requests as req_lib
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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

                logger.info(f"offset={offset}: {new_this_page} new → total {len(tracks)}")
                if new_this_page == 0:
                    break
                offset += 50

            return tracks

        return await loop.run_in_executor(None, _do)

    # ── تمام آهنگ‌های یک خواننده ─────────────────────────────────────────────
    async def get_artist_tracks(self, artist_url: str) -> list[dict]:
        """
        همه آهنگ‌های یک خواننده از Spotify API (anonymous token).
        شامل همه آلبوم‌ها، سینگل‌ها و EP ها — بدون محدودیت ۵۰ تایی.
        """
        loop = asyncio.get_event_loop()
        m = re.search(r'spotify\.com/artist/([A-Za-z0-9]+)', artist_url)
        if not m:
            return []
        artist_id = m.group(1)

        def _do():
            import requests as req_lib
            token = _get_spotify_token()
            if not token:
                logger.error("Could not get Spotify token for artist fetch")
                return []

            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0",
            }

            # ── مرحله ۱: همه آلبوم‌ها/سینگل‌ها ──────────────────────────────
            albums = []
            url = (
                f"https://api.spotify.com/v1/artists/{artist_id}/albums"
                f"?include_groups=album,single&limit=50&market=US"
            )
            while url:
                try:
                    r = req_lib.get(url, headers=headers, timeout=12)
                    r.raise_for_status()
                    data = r.json()
                    albums.extend(data.get("items", []))
                    url = data.get("next")
                except Exception as e:
                    logger.warning(f"Albums fetch error: {e}")
                    break

            logger.info(f"Artist {artist_id}: {len(albums)} albums/singles found")

            # ── مرحله ۲: همه آهنگ‌های هر آلبوم ──────────────────────────────
            tracks = []
            seen_ids = set()

            for album in albums:
                album_id = album.get("id", "")
                album_name = album.get("name", "")
                cover_url = ""
                if album.get("images"):
                    cover_url = album["images"][0].get("url", "")

                alb_url = (
                    f"https://api.spotify.com/v1/albums/{album_id}/tracks"
                    f"?limit=50&market=US"
                )
                while alb_url:
                    try:
                        r = req_lib.get(alb_url, headers=headers, timeout=12)
                        r.raise_for_status()
                        data = r.json()
                        for t in data.get("items", []):
                            tid = t.get("id", "")
                            if not tid or tid in seen_ids:
                                continue
                            seen_ids.add(tid)
                            artist_names = ", ".join(
                                a["name"] for a in (t.get("artists") or []) if a.get("name")
                            )
                            tracks.append({
                                "url": f"https://open.spotify.com/track/{tid}",
                                "title": t.get("name", ""),
                                "artist": artist_names,
                                "album": album_name,
                                "cover_url": cover_url,
                                "lyrics": "",
                            })
                        alb_url = data.get("next")
                    except Exception as e:
                        logger.warning(f"Album {album_id} tracks error: {e}")
                        break

            logger.info(f"Artist {artist_id}: total {len(tracks)} unique tracks")
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
