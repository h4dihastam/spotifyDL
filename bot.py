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
    """استخراج ID اسپاتیفای از URL (بدون query string)"""
    m = re.search(r'spotify\.com/(?:track|album|playlist)/([A-Za-z0-9]+)', url)
    return m.group(1) if m else ""


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
        "• لینک آهنگ اسپاتیفای\n"
        "• لینک آلبوم اسپاتیفای\n"
        "• لینک پلی‌لیست اسپاتیفای\n"
        "• اسم آهنگ (فارسی یا انگلیسی)\n\n"
        "*کیفیت:*\n"
        "🔹 320kbps MP3\n"
        "🔸 128kbps MP3\n\n"
        "⚠️ پلی‌لیست‌های بزرگ زمان بیشتری می‌برن.",
        parse_mode="Markdown"
    )


# ── پیام متنی ─────────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "spotify.com" in text:
        if "/track/" in text:
            kind = "track"; emoji = "🎵"; label = "آهنگ"
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

        # فرمت: dl|quality|kind|SPOTIFY_ID  (حدود ۳۷ بایت — زیر ۶۴)
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
        # جستجو
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
        # pick|index|sr_key
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
        # dl|quality|kind|SPOTIFY_ID
        parts = data.split("|", 3)
        if len(parts) != 4:
            await query.edit_message_text("❌ داده نامعتبر.")
            return
        quality, kind, sp_id = parts[1], parts[2], parts[3]
        url = f"https://open.spotify.com/{kind}/{sp_id}"
        await run_download(update, context, url=url, kind=kind, quality=quality, is_spotify=True)

    elif data.startswith("ytdl|"):
        # ytdl|quality|yt_key
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
    type_label = {"track": "آهنگ", "album": "آلبوم", "playlist": "پلی‌لیست"}.get(kind, kind)

    await query.edit_message_text(f"⏳ دانلود {type_label} با کیفیت {q_label}...")

    try:
        if kind == "track" or not is_spotify:
            result = await dl.download_one(url, quality, is_spotify)
            await send_audio(context.bot, chat_id, result)
        else:
            await query.edit_message_text("⏳ در حال دریافت لیست آهنگ‌ها...")
            tracks = await dl.get_collection_tracks(url, kind)
            total = len(tracks)
            if not total:
                await context.bot.send_message(chat_id, "❌ نتونستم آهنگ‌های پلی‌لیست/آلبوم رو بگیرم.")
                return
            await context.bot.send_message(chat_id, f"📦 {total} آهنگ پیدا شد. شروع دانلود...")

            ok, fail = 0, 0
            for i, track in enumerate(tracks, 1):
                try:
                    await context.bot.send_message(chat_id, f"⏳ {i}/{total} — {track.get('title', '')}")
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

            await context.bot.send_message(
                chat_id,
                f"✅ تموم شد!\n✔️ موفق: {ok}\n❌ ناموفق: {fail}"
            )
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

    # ارسال لیریک
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
