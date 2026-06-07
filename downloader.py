"""
downloader.py
─────────────
بدون نیاز به Spotify API.
- لینک اسپاتیفای  →  spotdl  (متادیتا + دانلود از YouTube Music)
- جستجو با اسم    →  yt-dlp ytsearch  (مستقیم از یوتیوب)
"""

import os
import re
import asyncio
import aiohttp
import tempfile
import logging
from pathlib import Path
from typing import Optional

import yt_dlp
from spotdl import Spotdl
from spotdl.types.song import Song
from spotdl.utils.spotify import SpotifyClient

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "tgbot_music"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── spotdl بدون API رسمی اسپاتیفای ──────────────────────────────────────────
def _init_spotdl():
    """
    spotdl از spotipyfree/spotapi استفاده می‌کنه که نیازی به
    client_id/secret نداره — use_official_api=False
    """
    SpotifyClient.init(
        client_id="",
        client_secret="",
        use_official_api=False,
    )

_init_spotdl()


class Downloader:

    # ── جستجو ────────────────────────────────────────────────────────────────
    async def search(self, query: str, limit: int = 5) -> list:
        """جستجو با yt-dlp (بدون هیچ API)"""
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
                entries = info.get("entries", [])
                results = []
                for e in entries:
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
    async def download_one(self, url: str, quality: str, is_spotify: bool) -> dict:
        """
        is_spotify=True  → ابتدا با spotdl متادیتا می‌گیریم، بعد دانلود
        is_spotify=False → مستقیم با yt-dlp از یوتیوب
        """
        loop = asyncio.get_event_loop()

        if is_spotify:
            return await loop.run_in_executor(None, self._spotdl_download, url, quality)
        else:
            return await loop.run_in_executor(None, self._ytdlp_download, url, quality, {})

    def _spotdl_download(self, spotify_url: str, quality: str) -> dict:
        """دانلود با spotdl (متادیتا از اسپاتیفای بدون API رسمی + دانلود از YT Music)"""
        try:
            # دریافت اطلاعات آهنگ
            song = Song.from_url(spotify_url)
            metadata = {
                "title": song.name,
                "artist": song.artist,
                "album": song.album_name or "",
                "cover_url": song.cover_url,
            }
            # جستجو در یوتیوب با اسم آهنگ
            search_q = f"{song.name} {song.artist} audio"
            return self._ytdlp_download(
                f"ytsearch1:{search_q}", quality, metadata
            )
        except Exception as e:
            logger.error(f"spotdl error: {e}")
            # fallback: بدون متادیتا
            return self._ytdlp_download(spotify_url, quality, {})

    def _ytdlp_download(self, url: str, quality: str, metadata: dict) -> dict:
        """دانلود با yt-dlp"""
        audio_quality = "320" if quality == "320" else "0"
        safe = re.sub(r'[\\/*?:"<>|]', "_", metadata.get("title", "audio"))
        out_tmpl = str(DOWNLOAD_DIR / f"{safe}_%(id)s.%(ext)s")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }],
        }

        downloaded_path = None
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                info = info["entries"][0]

            # پیدا کردن فایل MP3
            base = Path(ydl.prepare_filename(info)).stem
            for f in DOWNLOAD_DIR.glob(f"{base}*.mp3"):
                downloaded_path = str(f)
                break
            if not downloaded_path:
                # fallback search
                vid_id = info.get("id", "")
                for f in DOWNLOAD_DIR.glob(f"*{vid_id}*.mp3"):
                    downloaded_path = str(f)
                    break

            if not downloaded_path:
                return {"success": False, "error": "فایل MP3 پیدا نشد"}

            # اگه متادیتا نداشتیم از yt-dlp می‌گیریم
            if not metadata.get("title"):
                title = info.get("title", "Unknown")
                parts = title.split(" - ", 1)
                metadata = {
                    "title": parts[1].strip() if len(parts) == 2 else title,
                    "artist": parts[0].strip() if len(parts) == 2 else info.get("uploader", ""),
                    "album": "",
                    "cover_url": info.get("thumbnail"),
                }

        # اضافه کردن متادیتا به MP3
        thumb_path = None
        if metadata.get("cover_url"):
            thumb_path = self._download_cover_sync(metadata["cover_url"], safe)
        self._embed_tags(downloaded_path, metadata, thumb_path)

        return {
            "success": True,
            "path": downloaded_path,
            "thumb": thumb_path,
            "title": metadata.get("title", ""),
            "artist": metadata.get("artist", ""),
            "album": metadata.get("album", ""),
        }

    # ── آلبوم / پلی‌لیست ─────────────────────────────────────────────────────
    async def get_collection_tracks(self, url: str, kind: str) -> list[str]:
        """لیست URL آهنگ‌های یک آلبوم یا پلی‌لیست اسپاتیفای"""
        loop = asyncio.get_event_loop()

        def _do():
            if kind == "album":
                songs = Song.list_from_url(url)   # spotdl
            else:
                songs = Song.list_from_url(url)
            return [s.url for s in songs]

        return await loop.run_in_executor(None, _do)

    # ── ابزارها ──────────────────────────────────────────────────────────────
    def _download_cover_sync(self, cover_url: str, safe_name: str) -> Optional[str]:
        import urllib.request
        try:
            path = str(DOWNLOAD_DIR / f"{safe_name}_cover.jpg")
            urllib.request.urlretrieve(cover_url, path)
            return path
        except Exception as e:
            logger.warning(f"Cover download failed: {e}")
            return None

    def _embed_tags(self, mp3_path: str, meta: dict, thumb_path: Optional[str]):
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC
            audio = MP3(mp3_path, ID3=ID3)
            try: audio.add_tags()
            except: pass
            t = audio.tags
            if meta.get("title"):  t.add(TIT2(encoding=3, text=meta["title"]))
            if meta.get("artist"): t.add(TPE1(encoding=3, text=meta["artist"]))
            if meta.get("album"):  t.add(TALB(encoding=3, text=meta["album"]))
            if thumb_path and Path(thumb_path).exists():
                with open(thumb_path, "rb") as img:
                    t.add(APIC(encoding=3, mime="image/jpeg", type=3,
                               desc="Cover", data=img.read()))
            audio.save()
        except Exception as e:
            logger.warning(f"Tag embed failed: {e}")
