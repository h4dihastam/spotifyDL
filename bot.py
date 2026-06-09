import os
import re
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from downloader import Downloader
from keep_alive import keep_alive

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
dl = Downloader()


def _extract_spotify_id(url: str) -> str:
    m = re.search(r'spotify\.com/(?:track|album|playlist)/([A-Za-z0-9]+)', url)
    return m.group(1) if m else ""


def _bar(current: int, total: int, width: int = 10) -> str:
    filled = int(width * current / total) if total else 0
    pct = int(100 * current / total) if total else 0
    return f"{'▓' * filled}{'░' * (width - filled)} {pct}%"


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *ربات دانلودر موزیک*\n\n"
        "می‌تونی:\n"
        "🔗 لینک اسپاتیفای بفرستی (آهنگ / آلبوم / پلی‌لیست)\n"
        "🔍 اسم آهنگ یا آرتیست بنویسی\n\n"
        "مثال:\n"
        "`https://open.spotify.com/track/...`\n"
        "`Blinding Lights The Weeknd`\n\n"
        "/help — راهنما",
        parse_mode="Markdown"
    )


# ── /help ─────────────────────────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *راهنما*\n\n"
        "*ورودی‌های مجاز:*\n"
        "• لینک آهنگ اسپاتیفای (دانلود فوری ۳۲۰kbps)\n"
        "• لینک آلبوم اسپاتیفای\n"
        "• لینک پلی‌لیست اسپاتیفای\n"
        "• اسم آهنگ (فارسی یا انگلیسی)\n\n"
        "*کیفیت:*\n"
        "🔹 آهنگ: ۳۲۰kbps خودکار\n"
        "🔹 آلبوم/پلی‌لیست: ۳۲۰ یا ۱۲۸ انتخابی\n\n"
        "⚠️ پلی‌لیست‌های بزرگ زمان بیشتری می‌برن.",
        parse_mode="Markdown"
    )


# ── پیام متنی ─────────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "spotify.com" in text:
        if "/track/" in text:
            sp_id = _extract_spotify_id(text)
            if not sp_id:
                await update.message.reply_text("❌ نتونستم ID اسپاتیفای رو پیدا کنم.")
                return
            url = f"https://open.spotify.com/track/{sp_id}"
            # آهنگ single: دانلود فوری بدون نیاز به دکمه
            msg = await update.message.reply_text("⏳ دانلود آهنگ با کیفیت 320kbps...")
            try:
                result = await dl.download_one(url, "320", True)
                await send_audio(context.bot, update.message.chat_id, result)
            except Exception as e:
                logger.error(f"Track download error: {e}")
                await context.bot.send_message(update.message.chat_id, f"❌ خطا:\n{str(e)[:300]}")
            finally:
                try:
                    await msg.delete()
                except Exception:
                    pass
            return

        elif "/album/" in text:
            kind = "album"; emoji = "💿"; label = "آلبوم"
        elif "/playlist/" in text:
            kind = "playlist"; emoji = "📋"; label = "پلی‌لیست"
        else:
            await update.message.reply_text("❌ لینک اسپاتیفای معتبر نیست.")
            return

        sp_id = _extract_spotify_id(text)
        if not sp_id:
            await update.message.reply_text("❌ نتونستم ID اسپاتیفای رو پیدا کنم.")
            return

        keyboard = [[
            InlineKeyboardButton("🔹 320kbps", callback_data=f"dl|320|{kind}|{sp_id}"),
            InlineKeyboardButton("🔸 128kbps", callback_data=f"dl|128|{kind}|{sp_id}"),
        ]]
        await update.message.reply_text(
            f"{emoji} *{label}* شناسایی شد.\nکیفیت دانلود رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        msg = await update.message.reply_text(f"🔍 جستجو: *{text}*...", parse_mode="Markdown")
        try:
            results = await dl.search(text, limit=5)
            if not results:
                await msg.edit_text("❌ نتیجه‌ای پیدا نشد.")
                return

            key = f"sr_{update.effective_chat.id}_{msg.message_id}"
            context.bot_data[key] = results

            keyboard = [
                [InlineKeyboardButton(
                    f"🎵 {r['title'][:45]}",
                    callback_data=f"pick|{i}|{key}"
                )]
                for i, r in enumerate(results)
            ]
            await msg.edit_text(
                f"نتایج جستجو برای *{text}*:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(e)
            await msg.edit_text("❌ خطا در جستجو.")


# ── Callback ──────────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("pick|"):
        _, idx, key = data.split("|", 2)
        results = context.bot_data.get(key)
        if not results:
            await query.edit_message_text("❌ نتایج منقضی شدن. دوباره جستجو کن.")
            return
        r = results[int(idx)]
        title = r["title"]

        yt_key = f"yt_{key}_{idx}"
        context.bot_data[yt_key] = {"url": r["url"]}

        keyboard = [[
            InlineKeyboardButton("🔹 320kbps", callback_data=f"ytdl|320|{yt_key}"),
            InlineKeyboardButton("🔸 128kbps", callback_data=f"ytdl|128|{yt_key}"),
        ]]
        await query.edit_message_text(
            f"🎵 *{title}*\n\nکیفیت رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data.startswith("dl|"):
        parts = data.split("|", 3)
        if len(parts) != 4:
            await query.edit_message_text("❌ داده نامعتبر.")
            return
        quality, kind, sp_id = parts[1], parts[2], parts[3]
        url = f"https://open.spotify.com/{kind}/{sp_id}"
        await run_download(update, context, url=url, kind=kind, quality=quality, is_spotify=True)

    elif data.startswith("ytdl|"):
        parts = data.split("|", 2)
        quality, yt_key = parts[1], parts[2]
        stored = context.bot_data.get(yt_key)
        if not stored:
            await query.edit_message_text("❌ نتایج منقضی شدن. دوباره جستجو کن.")
            return
        url = stored["url"]
        await run_download(update, context, url=url, kind="track", quality=quality, is_spotify=False)


async def run_download(update, context, url, kind, quality, is_spotify):
    query = update.callback_query
    chat_id = query.message.chat_id
    q_label = "320kbps" if quality == "320" else "128kbps"

    await query.edit_message_text(f"⏳ دانلود آهنگ با کیفیت {q_label}...")

    try:
        if kind == "track" or not is_spotify:
            result = await dl.download_one(url, quality, is_spotify)
            await send_audio(context.bot, chat_id, result)
            try:
                await query.message.delete()
            except Exception:
                pass
        else:
            await query.edit_message_text("⏳ در حال دریافت لیست آهنگ‌ها...")
            tracks = await dl.get_collection_tracks(url, kind)
            total = len(tracks)
            if not total:
                await context.bot.send_message(chat_id, "❌ نتونستم آهنگ‌های پلی‌لیست/آلبوم رو بگیرم.")
                return

            type_label = "آلبوم" if kind == "album" else "پلی‌لیست"
            progress_msg = await context.bot.send_message(
                chat_id,
                f"📦 {total} آهنگ پیدا شد — شروع دانلود...\n{_bar(0, total)}"
            )
            try:
                await query.message.delete()
            except Exception:
                pass

            ok, fail = 0, 0
            for i, track in enumerate(tracks, 1):
                title_short = (track.get("title") or "")[:35]
                try:
                    await progress_msg.edit_text(
                        f"⏳ {type_label} — {i}/{total}\n"
                        f"🎵 {title_short}\n"
                        f"{_bar(i - 1, total)}"
                    )
                except Exception:
                    pass

                try:
                    prefetch = {
                        "title": track.get("title", ""),
                        "artist": track.get("artist", ""),
                        "album": track.get("album", ""),
                        "cover_url": track.get("cover_url", ""),
                        "lyrics": "",
                    }
                    result = await dl.download_one(track["url"], quality, True, prefetch=prefetch)
                    await send_audio(context.bot, chat_id, result)
                    ok += 1
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.error(f"Track {i} failed: {e}")
                    fail += 1

            try:
                await progress_msg.edit_text(
                    f"✅ {type_label} تموم شد!\n"
                    f"{_bar(total, total)}\n"
                    f"✔️ موفق: {ok}   ❌ ناموفق: {fail}"
                )
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Download error: {e}")
        await context.bot.send_message(chat_id, f"❌ خطا:\n{str(e)[:300]}")


async def send_audio(bot, chat_id, result):
    if not result["success"]:
        await bot.send_message(chat_id, f"❌ {result.get('error', 'خطای ناشناخته')}")
        return

    title = result.get("title", "")
    artist = result.get("artist", "")
    album = result.get("album", "")

    caption = f"🎵 {title}"
    if artist:
        caption += f"\n👤 {artist}"
    if album:
        caption += f"\n💿 {album}"

    thumb = open(result["thumb"], "rb") if result.get("thumb") else None
    try:
        with open(result["path"], "rb") as f:
            await bot.send_audio(
                chat_id, audio=f, caption=caption,
                title=title, performer=artist,
                thumbnail=thumb
            )
    finally:
        if thumb:
            thumb.close()
        for p in [result.get("path"), result.get("thumb")]:
            if p:
                try: os.unlink(p)
                except: pass

    lyrics = result.get("lyrics", "").strip()
    if not lyrics and title:
        lyrics = await dl.fetch_lyrics(title, artist)

    if lyrics:
        header = f"📝 متن آهنگ — {title}\n{'─'*30}\n"
        full = header + lyrics
        for i in range(0, len(full), 4000):
            try:
                await bot.send_message(chat_id, full[i:i+4000])
            except Exception as e:
                logger.warning(f"Lyrics send failed: {e}")
                break


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    keep_alive()

    if os.environ.get("REPL_ID"):
        logger.info("Running on Replit — Flask only (bot polling disabled to avoid Conflict with Render)")
        import time
        while True:
            time.sleep(60)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
