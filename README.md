# 🎵 ربات دانلودر موزیک (بدون Spotify API)

دانلود آهنگ، آلبوم و پلی‌لیست اسپاتیفای — **بدون نیاز به Spotify Developer Account**.

## ✨ قابلیت‌ها
- 🔗 لینک آهنگ / آلبوم / پلی‌لیست اسپاتیفای
- 🔍 جستجو با اسم آهنگ یا آرتیست
- 🎚 انتخاب کیفیت: 320kbps یا بهترین
- 🎨 کاور آرت + متادیتا کامل در فایل MP3

## 🛠 نصب

### ۱. پیش‌نیازها
```bash
pip install -r requirements.txt
```

نصب ffmpeg:
- **Ubuntu/Debian:** `sudo apt install ffmpeg`
- **Windows:** دانلود از https://ffmpeg.org/download.html و اضافه به PATH

### ۲. توکن ربات
1. به @BotFather پیام بده
2. `/newbot` بزن و اسم بده
3. توکن رو کپی کن

### ۳. راه‌اندازی
```bash
cp .env.example .env
# فایل .env رو باز کن و BOT_TOKEN رو پر کن
```

### ۴. اجرا
```bash
# Linux/Mac
export BOT_TOKEN="توکن_شما"
python bot.py

# Windows (PowerShell)
$env:BOT_TOKEN="توکن_شما"
python bot.py
```

## ❓ چرا بدون Spotify API کار می‌کنه؟
- **spotdl** از روش‌های جایگزین (بدون API رسمی) متادیتا می‌گیره
- فایل صوتی از **YouTube Music** دانلود میشه
- هیچ client_id یا client_secret نیاز نیست!

## 📁 ساختار
```
spotify-bot/
├── bot.py           # هندلرهای تلگرام
├── downloader.py    # موتور دانلود
├── requirements.txt
├── .env.example
└── README.md
```

## 🐛 مشکلات رایج
| مشکل | راه‌حل |
|------|---------|
| `ffmpeg not found` | ffmpeg رو نصب کن |
| دانلود کند | نرمال است، از YouTube دانلود می‌شه |
| خطای spotdl | `pip install -U spotdl` |
