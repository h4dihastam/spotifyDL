import os
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "ربات اسپاتیفای زنده است! 🎵"

@app.route('/health')
def health():
    return "ok", 200

def run():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
