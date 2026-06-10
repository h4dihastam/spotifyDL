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
    """گزینه‌های اضافه برای YouTube — چند client به ترتیب اولویت"""
    return {
        **_yt_opts_base(),
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "tv_embedded", "android_music", "mweb", "web"]
            }
        },
        "concurrent_fragment_downloads": 5,
    }


# ── Spotify Client Credentials token (اگه env var تنظیم شده) ─────────────────
_sp_cc_token: Optional[str] = None
_sp_cc_expiry: float = 0.0

def _get_spotify_cc_token() -> Optional[str]:
    """
    توکن Spotify با Client Credentials flow.
    نیاز به SPOTIFY_CLIENT_ID و SPOTIFY_CLIENT_SECRET در env vars دارد.
    از هر IP کار می‌کنه — بلاک نمی‌شه.
    """
    import time
    import base64
    import requests as req_lib
    global _sp_cc_token, _sp_cc_expiry

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None

    if _sp_cc_token and time.time() < _sp_cc_expiry - 30:
        return _sp_cc_token

    try:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        r = req_lib.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        _sp_cc_token = data.get("access_token")
        _sp_cc_expiry = time.time() + data.get("expires_in", 3600)
        logger.info("Spotify CC token refreshed ✓")
        return _sp_cc_token
    except Exception as e:
        logger.warning(f"Spotify CC token failed: {e}")
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
                info = ydl.extract_info(f"ytsearch5:{query}", download=False)
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

        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": audio_quality,
        }]

        # فرمت‌ها به ترتیب اولویت — اگه اولی fail شد، بعدی امتحان می‌شه
        format_attempts = [
            _SAFE_FORMAT,                          # bestaudio/best
            "bestaudio[ext=m4a]/bestaudio/best",   # m4a اول
            "best",                                # هر چی هست
        ]

        info = None
        last_err = "اطلاعاتی دریافت نشد"
        last_ydl = None

        for fmt in format_attempts:
            ydl_opts = {
                **base_opts,
                "format": fmt,
                "outtmpl": out_tmpl,
                "noplaylist": True,
                "ignoreerrors": False,
                "postprocessors": postprocessors,
            }
            try:
                ydl_inst = yt_dlp.YoutubeDL(ydl_opts)
                with ydl_inst:
                    info = ydl_inst.extract_info(url, download=True)
                    if info is None:
                        last_err = "اطلاعاتی دریافت نشد"
                        continue
                    if "entries" in info:
                        info = info["entries"][0]
                    last_ydl = ydl_inst
                break  # موفق شد
            except yt_dlp.utils.DownloadError as e:
                last_err = str(e)[:200]
                if "Requested format is not available" in last_err or "No video formats found" in last_err:
                    logger.warning(f"Format '{fmt}' failed for {url[:60]}, trying next...")
                    continue
                return {"success": False, "error": last_err}
            except Exception as e:
                return {"success": False, "error": str(e)[:200]}

        if info is None or last_ydl is None:
            return {"success": False, "error": last_err}

        downloaded_path = self._find_mp3(last_ydl, info, safe_title)
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
        اولویت‌بندی منابع:
          1. Spotify API (Client Credentials) — اگه SPOTIFY_CLIENT_ID/SECRET تنظیم شده
          2. Deezer API                        — رایگان، بدون auth، برای خواننده‌های غربی
          3. Spotify embed top tracks          — fallback برای خواننده‌هایی که جای دیگه نیستن
        """
        m = re.search(r'spotify\.com/artist/([A-Za-z0-9]+)', artist_url)
        if not m:
            return []
        artist_id = m.group(1)

        loop = asyncio.get_event_loop()

        def _do():
            import requests as req_lib
            import time
            from difflib import SequenceMatcher

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }

            def _sim(a: str, b: str) -> float:
                return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

            # ── helpers مشترک ─────────────────────────────────────────────────
            def _parse_track_item(t: dict, alb_name: str = "", cover_fallback: str = "") -> Optional[dict]:
                """یک track dict رو به فرمت خروجی تبدیل می‌کنه."""
                title = t.get("name", "")
                if not title:
                    return None
                raw_artists = t.get("artists") or t.get("artist", {})
                if isinstance(raw_artists, list):
                    t_artist = ", ".join(
                        (a.get("profile", {}).get("name") or a.get("name", ""))
                        for a in raw_artists if a
                    )
                elif isinstance(raw_artists, dict):
                    t_artist = raw_artists.get("name", "") or artist_name
                else:
                    t_artist = artist_name
                if not t_artist:
                    t_artist = artist_name

                alb = t.get("albumOfTrack") or t.get("album") or {}
                cover = cover_fallback
                ca = alb.get("coverArt") or {}
                srcs = ca.get("sources") or []
                if srcs:
                    cover = max(srcs, key=lambda s: s.get("width", 0) or 0).get("url", "")
                a_name = alb.get("name", "") or alb_name

                dur_ms = t.get("duration", {})
                if isinstance(dur_ms, dict):
                    dur_sec = int(dur_ms.get("totalMilliseconds", 0) / 1000)
                else:
                    dur_sec = int((dur_ms or 0) / 1000)

                t_id = t.get("id") or ""
                sp_url = (
                    f"https://open.spotify.com/track/{t_id}"
                    if t_id else
                    f"https://open.spotify.com/artist/{artist_id}"
                )
                return {
                    "url": sp_url,
                    "title": title,
                    "artist": t_artist.strip(", "),
                    "album": a_name,
                    "cover_url": cover,
                    "lyrics": "",
                    "duration_sec": dur_sec,
                }

            def _fetch_album_embed_tracks(alb_id: str) -> list[dict]:
                """آهنگ‌های یک آلبوم رو از embed page اسپاتیفای می‌گیره."""
                try:
                    r = req_lib.get(
                        f"https://open.spotify.com/embed/album/{alb_id}",
                        headers=headers, timeout=12
                    )
                    r.raise_for_status()
                    nm = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
                    if not nm:
                        return []
                    ae = json.loads(nm.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
                    alb_name = ae.get("name", "")
                    imgs = ae.get("images") or (ae.get("coverArt") or {}).get("sources") or []
                    cover = max(imgs, key=lambda s: s.get("width", 0) or s.get("height", 0)).get("url", "") if imgs else ""
                    raw_tracks = ae.get("tracks") or ae.get("trackList") or {}
                    t_items = raw_tracks.get("items") if isinstance(raw_tracks, dict) else raw_tracks
                    out = []
                    for ti in (t_items or []):
                        td = ti.get("track") or ti
                        parsed = _parse_track_item(td, alb_name, cover)
                        if parsed:
                            out.append(parsed)
                    return out
                except Exception as e:
                    logger.warning(f"Album embed {alb_id} failed: {e}")
                    return []

            # ── تابع fallback: آهنگ‌ها از embed Spotify (top tracks + discography) ──
            def _top_tracks_from_embed(entity: dict) -> list[dict]:
                tracks_out = []
                seen: set[str] = set()

                def _add(t_dict: Optional[dict]):
                    if not t_dict:
                        return
                    key = f"{t_dict['title'].lower()}|{t_dict['artist'].lower()}"
                    if key in seen:
                        return
                    seen.add(key)
                    tracks_out.append(t_dict)

                # ── مرحله ۱: topTracks از entity ─────────────────────────────
                top = entity.get("topTracks") or {}
                top_items = top.get("items") if isinstance(top, dict) else (top if isinstance(top, list) else [])
                for item in (top_items or []):
                    _add(_parse_track_item(item.get("track") or item))

                if tracks_out:
                    logger.info(f"Spotify embed topTracks: {len(tracks_out)} found")
                    return tracks_out

                # ── مرحله ۲: از discography در entity آلبوم IDs رو جمع کن ──
                logger.info(f"topTracks empty — scanning discography. Entity keys: {list(entity.keys())}")
                discography = entity.get("discography") or {}
                album_ids: list[str] = []

                for disc_key in ["popularReleasesAlbums", "albums", "singles", "latest", "recentlyPlayed"]:
                    section = discography.get(disc_key) or {}
                    if isinstance(section, dict):
                        # ممکنه مستقیم items داشته باشه یا تو releases باشه
                        rel_items = (
                            section.get("items")
                            or (section.get("releases") or {}).get("items")
                            or []
                        )
                    elif isinstance(section, list):
                        rel_items = section
                    else:
                        continue
                    for rel in rel_items:
                        aid = rel.get("id") or (rel.get("releases") or {}).get("items", [{}])[0].get("id", "")
                        if aid and aid not in album_ids:
                            album_ids.append(aid)

                logger.info(f"Discography album IDs found: {len(album_ids)}")

                # ── مرحله ۳: embed هر آلبوم ───────────────────────────────────
                for alb_id in album_ids[:15]:
                    for td in _fetch_album_embed_tracks(alb_id):
                        _add(td)
                    time.sleep(0.1)

                logger.info(f"Spotify embed discography fallback: {len(tracks_out)} tracks total")
                return tracks_out

            # ── مسیر ۱ (اگه credentials داریم): Spotify API کامل ─────────────
            def _spotify_api_discography() -> Optional[list[dict]]:
                """
                با Client Credentials token همه آلبوم‌ها و آهنگ‌های خواننده رو
                از Spotify API رسمی می‌گیریم. از هر IP بلاک نمی‌شه.
                """
                token = _get_spotify_cc_token()
                if not token:
                    return None

                auth_headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                }

                # اسم خواننده (اگه 403 بود ادامه می‌دیم)
                a_name = ""
                try:
                    r = req_lib.get(
                        f"https://api.spotify.com/v1/artists/{artist_id}",
                        headers=auth_headers, timeout=10
                    )
                    r.raise_for_status()
                    a_name = r.json().get("name", "")
                except Exception as e:
                    logger.warning(f"Spotify API artist info failed: {e} — continuing with albums")

                # همه آلبوم‌ها (album + single)
                albums = []
                url = (
                    f"https://api.spotify.com/v1/artists/{artist_id}/albums"
                    "?include_groups=album,single&limit=50&market=US"
                )
                while url:
                    try:
                        r = req_lib.get(url, headers=auth_headers, timeout=10)
                        r.raise_for_status()
                        page = r.json()
                        albums.extend(page.get("items", []))
                        url = page.get("next")
                        time.sleep(0.05)
                    except Exception as e:
                        logger.warning(f"Spotify API albums page error: {e}")
                        break

                logger.info(f"Spotify API: {len(albums)} albums for '{a_name}'")

                all_tracks: list[dict] = []
                seen_keys: set[str] = set()

                for album in albums:
                    alb_id = album.get("id", "")
                    alb_name = album.get("name", "")
                    images = album.get("images") or []
                    cover = images[0].get("url", "") if images else ""

                    tracks_url = (
                        f"https://api.spotify.com/v1/albums/{alb_id}/tracks?limit=50&market=US"
                    )
                    while tracks_url:
                        try:
                            r = req_lib.get(tracks_url, headers=auth_headers, timeout=10)
                            r.raise_for_status()
                            tpage = r.json()
                            for t in tpage.get("items", []):
                                title = t.get("name", "")
                                if not title:
                                    continue
                                t_artist = ", ".join(
                                    a.get("name", "") for a in (t.get("artists") or [])
                                )
                                key = f"{title.lower()}|{t_artist.lower()}"
                                if key in seen_keys:
                                    continue
                                seen_keys.add(key)
                                dur_sec = int((t.get("duration_ms") or 0) / 1000)
                                sp_url = (
                                    (t.get("external_urls") or {}).get("spotify", "")
                                    or f"https://open.spotify.com/track/{t.get('id', '')}"
                                )
                                all_tracks.append({
                                    "url": sp_url,
                                    "title": title,
                                    "artist": t_artist,
                                    "album": alb_name,
                                    "cover_url": cover,
                                    "lyrics": "",
                                    "duration_sec": dur_sec,
                                })
                            tracks_url = tpage.get("next")
                            time.sleep(0.05)
                        except Exception as e:
                            logger.warning(f"Spotify API tracks page error: {e}")
                            break

                logger.info(f"Spotify API: {len(all_tracks)} tracks total for '{a_name}'")
                return all_tracks if all_tracks else None

            sp_api_result = _spotify_api_discography()
            if sp_api_result is not None:
                return sp_api_result

            # ── مرحله ۱: اسم خواننده + entity از embed Spotify ──────────────
            artist_name = ""
            entity: dict = {}
            try:
                r = req_lib.get(
                    f"https://open.spotify.com/embed/artist/{artist_id}",
                    headers=headers, timeout=12
                )
                r.raise_for_status()
                nm = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
                if nm:
                    data = json.loads(nm.group(1))
                    entity = data["props"]["pageProps"]["state"]["data"]["entity"]
                    artist_name = (
                        entity.get("profile", {}).get("name", "")
                        or entity.get("name", "")
                    )
            except Exception as e:
                logger.warning(f"Could not get artist name from Spotify embed: {e}")

            if not artist_name:
                logger.error(f"Could not resolve artist name for {artist_id}")
                return []

            logger.info(f"Artist name: {artist_name}")

            # ── مرحله ۲: پیدا کردن خواننده در Deezer با بررسی شباهت ─────────
            deezer_artist_id = None
            deezer_artist_name = ""
            try:
                r = req_lib.get(
                    "https://api.deezer.com/search/artist",
                    params={"q": artist_name, "limit": 10},
                    headers=headers, timeout=10
                )
                r.raise_for_status()
                results = r.json().get("data", [])
                if results:
                    exact = next(
                        (x for x in results if x.get("name", "").lower() == artist_name.lower()),
                        None
                    )
                    if exact:
                        best, sim = exact, 1.0
                    else:
                        best = max(results, key=lambda x: _sim(artist_name, x.get("name", "")))
                        sim = _sim(artist_name, best.get("name", ""))

                    # بررسی نسبت طول اسم (جلوگیری از Matzak ≈ Matarzak)
                    la, lb = len(artist_name), len(best.get("name", ""))
                    len_ratio = min(la, lb) / max(la, lb) if max(la, lb) > 0 else 0.0

                    logger.info(
                        f"Deezer best match: '{best.get('name')}' "
                        f"(sim={sim:.2f}, len_ratio={len_ratio:.2f})"
                    )

                    if sim >= 0.80 and len_ratio >= 0.80:
                        deezer_artist_id = best["id"]
                        deezer_artist_name = best.get("name", artist_name)
                    else:
                        logger.warning(
                            f"Deezer mismatch: '{artist_name}' ≠ '{best.get('name')}' "
                            f"(sim={sim:.2f}, len_ratio={len_ratio:.2f}) — falling back to Spotify top tracks"
                        )
            except Exception as e:
                logger.warning(f"Deezer artist search failed: {e}")

            # ── Deezer پیدا نشد → top tracks از Spotify embed ────────────────
            if not deezer_artist_id:
                return _top_tracks_from_embed(entity)

            # ── مرحله ۳: همه آلبوم‌ها از Deezer ─────────────────────────────
            albums = []
            next_url = f"https://api.deezer.com/artist/{deezer_artist_id}/albums?limit=50"
            while next_url:
                try:
                    r = req_lib.get(next_url, headers=headers, timeout=10)
                    r.raise_for_status()
                    data = r.json()
                    albums.extend(data.get("data", []))
                    next_url = data.get("next")
                    time.sleep(0.05)
                except Exception as e:
                    logger.warning(f"Deezer albums page error: {e}")
                    break

            logger.info(f"Deezer: {len(albums)} albums for '{deezer_artist_name}'")

            # ── مرحله ۴: همه آهنگ‌های هر آلبوم ──────────────────────────────
            all_tracks: list[dict] = []
            seen_keys: set[str] = set()

            for album in albums:
                alb_id = album.get("id")
                alb_name = album.get("title", "")
                cover = (
                    album.get("cover_xl")
                    or album.get("cover_big")
                    or album.get("cover", "")
                )
                try:
                    r = req_lib.get(
                        f"https://api.deezer.com/album/{alb_id}/tracks",
                        params={"limit": 100},
                        headers=headers, timeout=10
                    )
                    r.raise_for_status()
                    for t in r.json().get("data", []):
                        title = t.get("title") or t.get("title_short", "")
                        contributors = t.get("contributors") or []
                        t_artist = ", ".join(
                            c["name"] for c in contributors if c.get("name")
                        ) or deezer_artist_name

                        key = f"{title.lower().strip()}|{t_artist.lower().strip()}"
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)

                        all_tracks.append({
                            "url": f"https://open.spotify.com/artist/{artist_id}",
                            "title": title,
                            "artist": t_artist,
                            "album": alb_name,
                            "cover_url": cover,
                            "lyrics": "",
                            "duration_sec": t.get("duration", 0),
                        })
                    time.sleep(0.05)
                except Exception as e:
                    logger.warning(f"Deezer album {alb_id} tracks error: {e}")

            logger.info(f"Deezer: {len(all_tracks)} unique tracks for '{deezer_artist_name}'")
            return all_tracks

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
