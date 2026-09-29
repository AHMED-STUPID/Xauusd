# XAUUSD Telegram Mini App

Files:
- main.py — Telegram bot + Python backend + API + auto monitor
- web/index.html — Mini App UI
- web/style.css — UI
- web/app.js — Telegram WebApp + API frontend
- .env.example — keys/config
- requirements.txt — dependencies

Install:
```bash
pip install -r requirements.txt
```
Copy `.env.example` to `.env`, add your real keys, then:
```bash
python main.py
```
Expose the server through a public HTTPS URL. Use that URL as the Telegram Mini App URL.
Never put API keys in frontend files.
