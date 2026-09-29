import os
import asyncio
import hmac
import hashlib
import json
import time
import logging
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler


# =========================================================
# PATHS + ENVIRONMENT
# =========================================================

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"

# =========================================================
# RENDER ENVIRONMENT VARIABLES
# =========================================================

# No Render Secret File is required.
# Render Environment Variables are used directly.

TOKEN = os.getenv("8801392935:AAHIXtFyRvWg8Go-o44vn9xakQnuYd4od2I", "").strip()
KEY = os.getenv("15b2d4c3a23143aea61106fd5c6dd27a", "").strip()
NEWS = os.getenv("d980775201f34edfab010ce8aff2c299", "").strip()

SYMBOL = os.getenv("SYMBOL", "XAU/USD")
INTERVAL = os.getenv("INTERVAL", "5min")

# Render normally provides PORT automatically.
PORT = int(os.getenv("PORT", "10000"))

AUTO = os.getenv(
    "AUTO_MONITOR",
    "true"
).lower() in ("1", "true", "yes", "on")

CHECK = max(
    15,
    int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
)


# =========================================================
# GLOBALS
# =========================================================

subs = set()
history = []
last_key = ""
bot_app = None
monitor_task = None

log = logging.getLogger("xau")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================================================
# HTTP REQUEST
# =========================================================

async def req(url, params):
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            return response.status, await response.json(
                content_type=None
            )


# =========================================================
# TWELVE DATA - PRICE
# =========================================================

async def price():

    status, data = await req(
        "https://api.twelvedata.com/price",
        {
            "symbol": SYMBOL,
            "apikey": KEY
        }
    )

    if status != 200 or "price" not in data:
        raise RuntimeError(str(data))

    return float(data["price"])


# =========================================================
# TWELVE DATA - CANDLES
# =========================================================

async def candles():

    status, data = await req(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "outputsize": 100,
            "apikey": KEY
        }
    )

    if status != 200 or not data.get("values"):
        raise RuntimeError(str(data))

    return [
        {
            "datetime": x["datetime"],
            "open": float(x["open"]),
            "high": float(x["high"]),
            "low": float(x["low"]),
            "close": float(x["close"])
        }
        for x in reversed(data["values"])
    ]


# =========================================================
# EMA
# =========================================================

def ema(values, period):

    result = [None] * len(values)

    if len(values) < period:
        return result

    average = sum(values[:period]) / period
    result[period - 1] = average

    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):
        average = (
            (values[i] - average) * multiplier
            + average
        )

        result[i] = average

    return result


# =========================================================
# RSI
# =========================================================

def rsi(values, period=14):

    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        result[period] = 100
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - 100 / (1 + rs)

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

        if avg_loss == 0:
            result[i + 1] = 100
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100 - 100 / (1 + rs)

    return result


# =========================================================
# ATR
# =========================================================

def atr(candles_data, period=14):

    true_ranges = []

    for i, candle in enumerate(candles_data):

        if i == 0:

            true_range = (
                candle["high"] - candle["low"]
            )

        else:

            previous_close = candles_data[i - 1]["close"]

            true_range = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close)
            )

        true_ranges.append(true_range)

    result = [None] * len(candles_data)

    if len(true_ranges) < period:
        return result

    average = sum(true_ranges[:period]) / period

    result[period - 1] = average

    for i in range(period, len(true_ranges)):

        average = (
            average * (period - 1)
            + true_ranges[i]
        ) / period

        result[i] = average

    return result


# =========================================================
# SIGNAL ENGINE
# =========================================================

def analyze(candles_data):

    if len(candles_data) < 60:

        return {
            "signal": "NO TRADE",
            "reason": "Need 60 candles."
        }

    values = [
        candle["close"]
        for candle in candles_data
    ]

    fast_ema = ema(values, 9)
    slow_ema = ema(values, 21)
    rsi_values = rsi(values, 14)
    atr_values = atr(candles_data, 14)

    i = len(candles_data) - 1

    if any(
        value is None
        for value in (
            fast_ema[i],
            slow_ema[i],
            rsi_values[i],
            atr_values[i]
        )
    ):

        return {
            "signal": "NO TRADE",
            "reason": "Indicators not ready."
        }

    current_price = values[i]

    buy = (
        current_price > fast_ema[i] > slow_ema[i]
        and 50 <= rsi_values[i] <= 70
    )

    sell = (
        current_price < fast_ema[i] < slow_ema[i]
        and 30 <= rsi_values[i] <= 50
    )

    if buy:

        signal = "BUY"
        reason = (
            "Bullish EMA structure "
            "with RSI confirmation."
        )

        entry = current_price
        sl = current_price - atr_values[i] * 1.5
        tp1 = current_price + atr_values[i] * 1.5
        tp2 = current_price + atr_values[i] * 3

    elif sell:

        signal = "SELL"
        reason = (
            "Bearish EMA structure "
            "with RSI confirmation."
        )

        entry = current_price
        sl = current_price + atr_values[i] * 1.5
        tp1 = current_price - atr_values[i] * 1.5
        tp2 = current_price - atr_values[i] * 3

    else:

        signal = "NO TRADE"
        reason = "No confirmed EMA + RSI setup."

        entry = None
        sl = None
        tp1 = None
        tp2 = None

    return {
        "signal": signal,
        "reason": reason,
        "time": candles_data[i]["datetime"],
        "price": current_price,
        "ema_fast": fast_ema[i],
        "ema_slow": slow_ema[i],
        "rsi": rsi_values[i],
        "atr": atr_values[i],
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2
    }


# =========================================================
# NEWS
# =========================================================

async def headlines():

    if not NEWS:
        return []

    status, data = await req(
        "https://newsapi.org/v2/everything",
        {
            "q": (
                "gold OR XAU OR USD OR "
                "Federal Reserve OR inflation"
            ),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 8,
            "apiKey": NEWS
        }
    )

    if status != 200:
        return []

    return [
        {
            "title": article.get("title", ""),
            "url": article.get("url", "")
        }
        for article in data.get("articles", [])
        if article.get("title")
    ]


# =========================================================
# TELEGRAM MINI APP USER VALIDATION
# =========================================================

def user_from(request):

    raw = request.headers.get(
        "X-Telegram-Init-Data",
        ""
    )

    if not raw:
        return None

    try:

        params = dict(
            parse_qsl(
                raw,
                keep_blank_values=True
            )
        )

        received_hash = params.pop(
            "hash",
            None
        )

        check_string = "\n".join(
            f"{key}={params[key]}"
            for key in sorted(params)
        )

        secret_key = hmac.new(
            b"WebAppData",
            TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if (
            not received_hash
            or not hmac.compare_digest(
                calculated_hash,
                received_hash
            )
        ):
            return None

        auth_date = int(
            params.get("auth_date", "0")
        )

        if (
            auth_date
            and time.time() - auth_date > 86400
        ):
            return None

        if params.get("user"):

            return json.loads(
                params["user"]
            )

        return {}

    except Exception:

        return None


# =========================================================
# API - USER
# =========================================================

async def api_user(request):

    user = user_from(request)

    if user is not None:

        return web.json_response({
            "ok": True,
            "user": user
        })

    return web.json_response(
        {
            "ok": False,
            "error": "Invalid Telegram init data"
        },
        status=401
    )


# =========================================================
# API - MARKET
# =========================================================

async def api_market(request):

    try:

        current_price = await price()

        return web.json_response({
            "ok": True,
            "symbol": SYMBOL,
            "price": current_price
        })

    except Exception as error:

        return web.json_response(
            {
                "ok": False,
                "error": str(error)
            },
            status=502
        )


# =========================================================
# API - SIGNAL
# =========================================================

async def api_signal(request):

    try:

        result = analyze(
            await candles()
        )

        history.append(result)

        del history[:-30]

        return web.json_response({
            "ok": True,
            "data": result
        })

    except Exception as error:

        return web.json_response(
            {
                "ok": False,
                "error": str(error)
            },
            status=502
        )


# =========================================================
# API - HISTORY
# =========================================================

async def api_history(request):

    return web.json_response({
        "ok": True,
        "items": history[-20:][::-1]
    })


# =========================================================
# API - NEWS
# =========================================================

async def api_news(request):

    try:

        news = await headlines()

        return web.json_response({
            "ok": True,
            "items": news
        })

    except Exception as error:

        return web.json_response(
            {
                "ok": False,
                "error": str(error)
            },
            status=502
        )


# =========================================================
# AUTO MONITOR
# =========================================================

async def monitor():

    global last_key

    while True:

        try:

            result = analyze(
                await candles()
            )

            key = (
                f"{result.get('signal')}"
                f"|{result.get('time')}"
                f"|{result.get('price')}"
            )

            if (
                result.get("signal")
                in ("BUY", "SELL")
                and key != last_key
            ):

                last_key = key

                history.append(result)

                del history[:-30]

                message = (
                    "🚨 XAUUSD AUTO SIGNAL\n\n"
                    f"{result['signal']}\n"
                    f"Price: {result.get('price', 0):.2f}\n"
                    f"Entry: {result.get('entry', 0):.2f}\n"
                    f"SL: {result.get('sl', 0):.2f}\n"
                    f"TP1: {result.get('tp1', 0):.2f}\n"
                    f"TP2: {result.get('tp2', 0):.2f}\n\n"
                    "⚠️ Algorithmic alert only."
                )

                for chat_id in list(subs):

                    try:

                        await bot_app.bot.send_message(
                            chat_id=chat_id,
                            text=message
                        )

                    except Exception:

                        pass

        except asyncio.CancelledError:

            raise

        except Exception as error:

            log.warning(
                "monitor: %s",
                error
            )

        await asyncio.sleep(CHECK)


# =========================================================
# TELEGRAM LIFECYCLE
# =========================================================

async def start(app):

    global monitor_task

    if AUTO:

        monitor_task = asyncio.create_task(
            monitor()
        )


async def stop(app):

    global monitor_task

    if monitor_task:

        monitor_task.cancel()

        try:

            await monitor_task

        except asyncio.CancelledError:

            pass


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

async def cmd_start(update, context):

    if update.effective_chat:

        subs.add(
            update.effective_chat.id
        )

    await update.message.reply_text(
        "🟡 XAUUSD Mini App Bot\n\n"
        "Open the Mini App from Telegram.\n\n"
        "/signal - analyze\n"
        "/market - price\n"
        "/news - headlines\n"
        "/chatid - chat ID"
    )


async def cmd_signal(update, context):

    try:

        result = analyze(
            await candles()
        )

        await update.message.reply_text(
            json.dumps(
                result,
                indent=2
            )
        )

    except Exception as error:

        await update.message.reply_text(
            f"Signal error: {error}"
        )


async def cmd_market(update, context):

    try:

        current_price = await price()

        await update.message.reply_text(
            f"XAU/USD: {current_price:.2f}"
        )

    except Exception as error:

        await update.message.reply_text(
            f"Market error: {error}"
        )


async def cmd_news(update, context):

    news = await headlines()

    if news:

        message = "\n".join(
            f"{i + 1}. {article['title']}"
            for i, article in enumerate(news)
        )

    else:

        message = "No news available."

    await update.message.reply_text(
        message
    )


async def cmd_chatid(update, context):

    await update.message.reply_text(
        str(update.effective_chat.id)
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    global bot_app

    # Check required environment variables.
    if not TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is missing. "
        "Add TELEGRAM_BOT_TOKEN in Render Environment Variables."
    )

if not KEY:
    raise RuntimeError(
        "MARKET_DATA_API_KEY is missing. "
        "Add MARKET_DATA_API_KEY in Render Environment Variables."
    )

    if not WEB.exists():

        raise RuntimeError(
            f"Web folder not found: {WEB}"
        )

    if not (WEB / "index.html").exists():

        raise RuntimeError(
            f"web/index.html not found: {WEB / 'index.html'}"
        )

    log.info(
        "Starting XAUUSD Mini App..."
    )

    log.info(
        "Symbol: %s | Interval: %s | Port: %s",
        SYMBOL,
        INTERVAL,
        PORT
    )

    bot_app = (
        Application.builder()
        .token(TOKEN)
        .post_init(start)
        .post_shutdown(stop)
        .build()
    )

    commands = [
        ("start", cmd_start),
        ("signal", cmd_signal),
        ("market", cmd_market),
        ("news", cmd_news),
        ("chatid", cmd_chatid)
    ]

    for command, handler in commands:

        bot_app.add_handler(
            CommandHandler(
                command,
                handler
            )
        )

    # =====================================================
    # WEB SERVER
    # =====================================================

    webapp = web.Application()

    webapp.router.add_get(
        "/",
        lambda request: web.FileResponse(
            WEB / "index.html"
        )
    )

    webapp.router.add_static(
        "/static/",
        WEB
    )

    webapp.router.add_get(
        "/api/user",
        api_user
    )

    webapp.router.add_get(
        "/api/market",
        api_market
    )

    webapp.router.add_get(
        "/api/signal",
        api_signal
    )

    webapp.router.add_get(
        "/api/history",
        api_history
    )

    webapp.router.add_get(
        "/api/news",
        api_news
    )

    runner = web.AppRunner(
        webapp
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    log.info(
        "Web server started on port %s",
        PORT
    )

    # =====================================================
    # TELEGRAM BOT
    # =====================================================

    await bot_app.initialize()

    await bot_app.start()

    await bot_app.updater.start_polling()

    log.info(
        "Telegram bot started successfully."
    )

    # Keep service alive.
    await asyncio.Event().wait()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    asyncio.run(main())
