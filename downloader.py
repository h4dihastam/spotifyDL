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
import tempfile
import logging
from pathlib import Path
from typing import Optional

import yt_dlp
from spotdl.types.song import Song
from spotdl.utils.spotify import SpotifyClient

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "tgbot_music"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── spotdl بدون API رسمی اسپاتیفای ──────────────────────────────────────────
def _init_spotdl():
    SpotifyClient.init(
        client_id="",
        client_secret="",
        use_official_api=False,
    )

_init_spotdl()


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
        loop = asyncio.get_event_loop()
        if is_spotify:
            return await loop.run_in_executor(None, self._spotdl_download, url, quality)
        else:
            return await loop.run_in_executor(None, self._ytdlp_download, url, quality, {})

    def _spotdl_download(self, spotify_url: str, quality: str) -> dict:
        try:
            song = Song.from_url(spotify_url)
            metadata = {
                "title": song.name,
                "artist": song.artist,
                "album": song.album_name or "",
                "cover_url": song.cover_url,
                "lyrics": song.lyrics or "",
            }
            search_q = f"{song.name} {song.artist} audio"
            result = self._ytdlp_download(f"ytsearch1:{search_q}", quality, metadata)
            # اگه لیریک از spotdl گرفتیم، توی نتیجه بذار
            if not result.get("lyrics") and metadata.get("lyrics"):
                result["lyrics"] = metadata["lyrics"]
            return result
        except Exception as e:
            logger.error(f"spotdl error: {e}")
            return self._ytdlp_download(spotify_url, quality, {})

    def _ytdlp_download(self, url: str, quality: str, metadata: dict) -> dict:
        if quality == "320":
            audio_quality = "320"
        elif quality == "128":
            audio_quality = "128"
        else:
            audio_quality = "192"

        safe = re.sub(r'[\\/*?:"<>|]', "_", metadata.get("title", "audio"))
        out_tmpl = str(DOWNLOAD_DIR / f"{safe}_%(id)s.%(ext)s")

        ydl_opts = {
            "format": "bestaudio[acodec!=opus]/bestaudio/best",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }],
        }

        downloaded_path = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    return {"success": False, "error": "اطلاعاتی دریافت نشد (احتمالاً DRM)"}
                if "entries" in info:
                    info = info["entries"][0]

                base = Path(ydl.prepare_filename(info)).stem
                for f in DOWNLOAD_DIR.glob(f"{base}*.mp3"):
                    downloaded_path = str(f)
                    break
                if not downloaded_path:
                    vid_id = info.get("id", "")
                    for f in DOWNLOAD_DIR.glob(f"*{vid_id}*.mp3"):
                        downloaded_path = str(f)
                        break

                if not downloaded_path:
                    return {"success": False, "error": "فایل MP3 پیدا نشد"}

                if not metadata.get("title"):
                    title = info.get("title", "Unknown")
                    parts = title.split(" - ", 1)
                    metadata = {
                        "title": parts[1].strip() if len(parts) == 2 else title,
                        "artist": parts[0].strip() if len(parts) == 2 else info.get("uploader", ""),
                        "album": "",
                        "cover_url": info.get("thumbnail"),
                        "lyrics": "",
                    }

        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            if "DRM" in err or "drm" in err.lower():
                logger.warning(f"DRM detected, retrying with different search: {url}")
                # تلاش دوباره با جستجوی مستقیم
                title_q = metadata.get("title", "")
                artist_q = metadata.get("artist", "")
                if title_q:
                    fallback_q = f"ytsearch1:{title_q} {artist_q} lyrics audio"
                    ydl_opts2 = dict(ydl_opts)
                    ydl_opts2["format"] = "bestaudio/best"
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts2) as ydl2:
                            info2 = ydl2.extract_info(fallback_q, download=True)
                            if info2 and "entries" in info2:
                                info2 = info2["entries"][0]
                            if info2:
                                base2 = Path(ydl2.prepare_filename(info2)).stem
                                for f in DOWNLOAD_DIR.glob(f"{base2}*.mp3"):
                                    downloaded_path = str(f)
                                    break
                                if not downloaded_path:
                                    vid_id2 = info2.get("id", "")
                                    for f in DOWNLOAD_DIR.glob(f"*{vid_id2}*.mp3"):
                                        downloaded_path = str(f)
                                        break
                    except Exception as e2:
                        logger.error(f"DRM fallback also failed: {e2}")
                        return {"success": False, "error": "محتوا DRM داره و دانلود ممکن نیست"}
                    if not downloaded_path:
                        return {"success": False, "error": "فایل MP3 پیدا نشد (بعد از DRM retry)"}
                else:
                    return {"success": False, "error": "محتوا DRM داره و دانلود ممکن نیست"}
            else:
                return {"success": False, "error": str(e)[:300]}

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
            "lyrics": metadata.get("lyrics", ""),
        }

    # ── لیریک ────────────────────────────────────────────────────────────────
    async def fetch_lyrics(self, title: str, artist: str) -> str:
        """جستجوی متن آهنگ با syncedlyrics"""
        loop = asyncio.get_event_loop()

        def _do():
            try:
                import syncedlyrics
                query = f"{title} {artist}".strip()
                lrc = syncedlyrics.search(query, plain_only=True)
                if lrc:
                    # حذف تایم‌استمپ‌های [mm:ss.xx]
                    clean = re.sub(r'\[\d+:\d+\.\d+\]', '', lrc).strip()
                    return clean[:4000]
            except Exception as e:
                logger.warning(f"Lyrics fetch failed: {e}")
            return ""

        return await loop.run_in_executor(None, _do)

    # ── آلبوم / پلی‌لیست ─────────────────────────────────────────────────────
    async def get_collection_tracks(self, url: str, kind: str) -> list[str]:
        loop = asyncio.get_event_loop()

        def _do():
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
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT
            audio = MP3(mp3_path, ID3=ID3)
            try: audio.add_tags()
            except: pass
            t = audio.tags
            if meta.get("title"):  t.add(TIT2(encoding=3, text=meta["title"]))
            if meta.get("artist"): t.add(TPE1(encoding=3, text=meta["artist"]))
            if meta.get("album"):  t.add(TALB(encoding=3, text=meta["album"]))
            if meta.get("lyrics"): t.add(USLT(encoding=3, lang="eng", desc="", text=meta["lyrics"]))
            if thumb_path and Path(thumb_path).exists():
                with open(thumb_path, "rb") as img:
                    t.add(APIC(encoding=3, mime="image/jpeg", type=3,
                               desc="Cover", data=img.read()))
            audio.save()
        except Exception as e:
            logger.warning(f"Tag embed failed: {e}")
