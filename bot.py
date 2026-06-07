import os
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
        "🔸 بهترین کیفیت موجود\n\n"
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

        key = f"sp_{update.effective_chat.id}_{update.message.message_id}"
        context.bot_data[key] = {"url": text, "kind": kind}

        keyboard = [[
            InlineKeyboardButton("🔹 320kbps", callback_data=f"dl|320|{key}"),
            InlineKeyboardButton("🔸 بهترین کیفیت", callback_data=f"dl|best|{key}"),
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
            InlineKeyboardButton("🔸 بهترین کیفیت", callback_data=f"ytdl|best|{yt_key}"),
        ]]
        await query.edit_message_text(
            f"🎵 *{title}*\n\nکیفیت رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data.startswith("dl|"):
        # dl|quality|sp_key
        parts = data.split("|", 2)
        quality, sp_key = parts[1], parts[2]
        stored = context.bot_data.get(sp_key)
        if not stored:
            await query.edit_message_text("❌ لینک منقضی شده. دوباره بفرست.")
            return
        url, kind = stored["url"], stored["kind"]
        await run_download(update, context, url=url, kind=kind, quality=quality, is_spotify=True)

    elif data.startswith("ytdl|"):
        # ytdl|quality|yt_key
        parts = data.split("|", 2)
        quality, yt_key = parts[1], parts[2]
        stored = context.bot_data.get(yt_key)
        if not stored:
            await query.edit_message_text("❌ لینک منقضی شده. دوباره جستجو کن.")
            return
        url = stored["url"]
        await run_download(update, context, url=url, kind="track", quality=quality, is_spotify=False)


async def run_download(update, context, url, kind, quality, is_spotify):
    query = update.callback_query
    chat_id = query.message.chat_id
    q_label = "320kbps" if quality == "320" else "بهترین کیفیت"
    type_label = {"track": "آهنگ", "album": "آلبوم", "playlist": "پلی‌لیست"}.get(kind, kind)

    await query.edit_message_text(f"⏳ دانلود {type_label} با کیفیت {q_label}...")

    try:
        if kind == "track" or not is_spotify:
            result = await dl.download_one(url, quality, is_spotify)
            await send_audio(context.bot, chat_id, result)
        else:
            # آلبوم یا پلی‌لیست
            tracks = await dl.get_collection_tracks(url, kind)
            total = len(tracks)
            await context.bot.send_message(chat_id, f"📦 {total} آهنگ پیدا شد. شروع دانلود...")

            ok, fail = 0, 0
            for i, track_url in enumerate(tracks, 1):
                try:
                    await context.bot.send_message(chat_id, f"⏳ {i}/{total}...")
                    result = await dl.download_one(track_url, quality, True)
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
        await context.bot.send_message(chat_id, f"❌ خطا:\n`{str(e)[:300]}`", parse_mode="Markdown")


async def send_audio(bot, chat_id, result):
    if not result["success"]:
        await bot.send_message(chat_id, f"❌ {result.get('error', 'خطای ناشناخته')}")
        return

    caption = f"🎵 *{result['title']}*"
    if result.get("artist"):
        caption += f"\n👤 {result['artist']}"
    if result.get("album"):
        caption += f"\n💿 {result['album']}"

    thumb = open(result["thumb"], "rb") if result.get("thumb") else None
    try:
        with open(result["path"], "rb") as f:
            await bot.send_audio(
                chat_id, audio=f, caption=caption, parse_mode="Markdown",
                title=result.get("title"), performer=result.get("artist"),
                thumbnail=thumb
            )
    finally:
        if thumb:
            thumb.close()
        # cleanup
        for p in [result.get("path"), result.get("thumb")]:
            if p:
                try: os.unlink(p)
                except: pass


# ── Main ──────────────────────────────────────────────────────────────────────
import asyncio
# ... بقیه importها

def main():
    keep_alive()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started...")
    asyncio.run(app.run_polling(allowed_updates=Update.ALL_TYPES))

if __name__ == "__main__":
    main()
