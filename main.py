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
from telegram.ext import Application, CommandHandler

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# Load local .env first, then also support Render Secret Files.
# Render Secret Files are commonly mounted under /etc/secrets/.
# Real Render Environment Variables always remain available through os.environ.
ENV_FILES = [
    BASE / ".env",
    Path("/etc/secrets/.env"),
]
for env_file in ENV_FILES:
    if env_file.is_file():
        load_dotenv(env_file, override=False)


def env_first(*names, default=""):
    """Return the first non-empty environment variable from names."""
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


# Primary names + a few harmless aliases so an existing Render setup does not
# break just because the variable was named slightly differently.
TOKEN = env_first(
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_TOKEN",
    "BOT_TOKEN",
)
KEY = env_first(
    "MARKET_DATA_API_KEY",
    "TWELVE_DATA_API_KEY",
    "TWELVEDATA_API_KEY",
)
NEWS = env_first(
    "NEWS_API_KEY",
    "NEWSAPI_KEY",
)

SYMBOL = env_first("SYMBOL", default="XAU/USD")
INTERVAL = env_first("INTERVAL", default="5min")

try:
    PORT = int(env_first("PORT", default="10000"))
except ValueError:
    PORT = 10000

AUTO = env_first(
    "AUTO_MONITOR",
    "AUTO_SIGNAL",
    default="true",
).lower() in ("1", "true", "yes", "on")

try:
    CHECK = max(15, int(env_first(
        "CHECK_INTERVAL_SECONDS",
        "CHECK_INTERVAL",
        default="60",
    )))
except ValueError:
    CHECK = 60

try:
    INIT_DATA_MAX_AGE = max(
        60,
        int(env_first("INIT_DATA_MAX_AGE_SECONDS", default="86400")),
    )
except ValueError:
    INIT_DATA_MAX_AGE = 86400

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("xau")

subs = set()
history = []
last_key = ""
bot_app = None
monitor_task = None
http_session = None


async def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "XAUUSD-Mini-App/1.0"}
        )
    return http_session


async def close_http_session():
    global http_session
    if http_session is not None and not http_session.closed:
        await http_session.close()
    http_session = None


async def req(url, params):
    session = await get_http_session()
    try:
        async with session.get(url, params=params) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = {"error": await response.text()}
            return response.status, data
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log.warning("HTTP request failed: %s", error)
        raise


async def price():
    if not KEY:
        raise RuntimeError("MARKET_DATA_API_KEY is not configured.")

    status, data = await req(
        "https://api.twelvedata.com/price",
        {"symbol": SYMBOL, "apikey": KEY}
    )

    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"Twelve Data HTTP error: {status}")

    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "Twelve Data error."))

    if "price" not in data:
        raise RuntimeError(str(data))

    return float(data["price"])


async def candles():
    if not KEY:
        raise RuntimeError("MARKET_DATA_API_KEY is not configured.")

    status, data = await req(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "outputsize": 100,
            "apikey": KEY
        }
    )

    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"Twelve Data HTTP error: {status}")

    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "Twelve Data error."))

    values = data.get("values")
    if not values:
        raise RuntimeError(str(data))

    return [
        {
            "datetime": x["datetime"],
            "open": float(x["open"]),
            "high": float(x["high"]),
            "low": float(x["low"]),
            "close": float(x["close"])
        }
        for x in reversed(values)
    ]


def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result

    average = sum(values[:period]) / period
    result[period - 1] = average
    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):
        average = (values[i] - average) * multiplier + average
        result[i] = average

    return result


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
        result[period] = 100.0
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
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100 - 100 / (1 + rs)

    return result


def atr(candles_data, period=14):
    true_ranges = []

    for i, candle in enumerate(candles_data):
        if i == 0:
            true_range = candle["high"] - candle["low"]
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
            average * (period - 1) + true_ranges[i]
        ) / period
        result[i] = average

    return result


def analyze(candles_data):
    if len(candles_data) < 60:
        return {
            "signal": "NO TRADE",
            "reason": "Need at least 60 candles."
        }

    values = [candle["close"] for candle in candles_data]
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
        reason = "Bullish EMA structure with RSI confirmation."
        entry = current_price
        sl = current_price - atr_values[i] * 1.5
        tp1 = current_price + atr_values[i] * 1.5
        tp2 = current_price + atr_values[i] * 3
    elif sell:
        signal = "SELL"
        reason = "Bearish EMA structure with RSI confirmation."
        entry = current_price
        sl = current_price + atr_values[i] * 1.5
        tp1 = current_price - atr_values[i] * 1.5
        tp2 = current_price - atr_values[i] * 3
    else:
        signal = "NO TRADE"
        reason = "No confirmed EMA + RSI setup."
        entry = sl = tp1 = tp2 = None

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


def add_history(result):
    if not result or not result.get("time"):
        return

    key = (
        result.get("signal"),
        result.get("time"),
        result.get("price")
    )

    for old in history:
        old_key = (
            old.get("signal"),
            old.get("time"),
            old.get("price")
        )
        if old_key == key:
            return

    history.append(result)
    if len(history) > 30:
        del history[:-30]


async def headlines():
    if not NEWS:
        return []

    status, data = await req(
        "https://newsapi.org/v2/everything",
        {
            "q": "gold OR XAU OR USD OR Federal Reserve OR inflation",
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 8,
            "apiKey": NEWS
        }
    )

    if status != 200 or not isinstance(data, dict):
        return []

    if data.get("status") == "error":
        log.warning("NewsAPI error: %s", data.get("message", "Unknown"))
        return []

    return [
        {
            "title": article.get("title", ""),
            "url": article.get("url", "")
        }
        for article in data.get("articles", [])
        if article.get("title")
    ]


def user_from(request):
    raw = request.headers.get("X-Telegram-Init-Data", "")
    if not raw or not TOKEN:
        return None

    try:
        params = dict(parse_qsl(raw, keep_blank_values=True))
        received_hash = params.pop("hash", None)

        if not received_hash:
            return None

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

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(params.get("auth_date", "0"))
        if (
            auth_date
            and time.time() - auth_date > INIT_DATA_MAX_AGE
        ):
            return None

        if params.get("user"):
            return json.loads(params["user"])

        return {}

    except Exception as error:
        log.warning("Telegram init-data validation failed: %s", error)
        return None


async def api_health(request):
    return web.json_response({
        "ok": True,
        "service": "XAUUSD Mini App",
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "auto_monitor": AUTO,
        "subscribers": len(subs),
        "history_items": len(history)
    })


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


async def api_market(request):
    try:
        current_price = await price()
        return web.json_response({
            "ok": True,
            "symbol": SYMBOL,
            "price": current_price
        })
    except Exception as error:
        log.warning("Market API error: %s", error)
        return web.json_response(
            {"ok": False, "error": str(error)},
            status=502
        )


async def api_signal(request):
    try:
        result = analyze(await candles())
        add_history(result)
        return web.json_response({
            "ok": True,
            "data": result
        })
    except Exception as error:
        log.warning("Signal API error: %s", error)
        return web.json_response(
            {"ok": False, "error": str(error)},
            status=502
        )


async def api_history(request):
    return web.json_response({
        "ok": True,
        "items": history[-20:][::-1]
    })


async def api_news(request):
    try:
        return web.json_response({
            "ok": True,
            "items": await headlines()
        })
    except Exception as error:
        log.warning("News API error: %s", error)
        return web.json_response(
            {"ok": False, "error": str(error)},
            status=502
        )


async def monitor():
    global last_key

    while True:
        try:
            result = analyze(await candles())
            signal = result.get("signal")

            key = (
                f"{signal}|"
                f"{result.get('time')}|"
                f"{result.get('price')}"
            )

            if signal in ("BUY", "SELL") and key != last_key:
                last_key = key
                add_history(result)

                message = (
                    "🚨 XAUUSD AUTO SIGNAL\n\n"
                    f"{signal}\n"
                    f"Price: {result.get('price', 0):.2f}\n"
                    f"Entry: {result.get('entry', 0):.2f}\n"
                    f"SL: {result.get('sl', 0):.2f}\n"
                    f"TP1: {result.get('tp1', 0):.2f}\n"
                    f"TP2: {result.get('tp2', 0):.2f}\n\n"
                    "⚠️ Algorithmic alert only."
                )

                if bot_app is not None:
                    for chat_id in list(subs):
                        try:
                            await bot_app.bot.send_message(
                                chat_id=chat_id,
                                text=message
                            )
                        except Exception as error:
                            log.warning(
                                "Telegram notification failed for %s: %s",
                                chat_id,
                                error
                            )

        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("Monitor error: %s", error)

        await asyncio.sleep(CHECK)


async def start(app):
    global monitor_task

    if AUTO and (
        monitor_task is None or monitor_task.done()
    ):
        monitor_task = asyncio.create_task(monitor())

    log.info(
        "Auto monitor: %s | Check interval: %ss",
        "ON" if AUTO else "OFF",
        CHECK
    )


async def stop(app):
    global monitor_task

    if monitor_task is not None:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        monitor_task = None


async def cmd_start(update, context):
    if update.effective_chat:
        subs.add(update.effective_chat.id)

    if update.message:
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
        result = analyze(await candles())
        add_history(result)

        if update.message:
            await update.message.reply_text(
                json.dumps(result, indent=2)
            )
    except Exception as error:
        if update.message:
            await update.message.reply_text(
                f"Signal error: {error}"
            )


async def cmd_market(update, context):
    try:
        current_price = await price()
        if update.message:
            await update.message.reply_text(
                f"XAU/USD: {current_price:.2f}"
            )
    except Exception as error:
        if update.message:
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

    if update.message:
        await update.message.reply_text(message)


async def cmd_chatid(update, context):
    if update.message and update.effective_chat:
        await update.message.reply_text(
            str(update.effective_chat.id)
        )


async def main():
    global bot_app

    if not TOKEN:
        raise RuntimeError(
            "Telegram bot token was not found. Checked "
            "TELEGRAM_BOT_TOKEN / TELEGRAM_TOKEN / BOT_TOKEN and "
            "local + /etc/secrets/.env files. "
            "Add the token to this Render service's Environment Variables "
            "or to a Secret File named .env."
        )

    if not KEY:
        raise RuntimeError(
            "Market-data API key was not found. Checked "
            "MARKET_DATA_API_KEY / TWELVE_DATA_API_KEY / TWELVEDATA_API_KEY "
            "and local + /etc/secrets/.env files."
        )

    if not WEB.exists():
        raise RuntimeError(f"Web folder not found: {WEB}")

    if not (WEB / "index.html").exists():
        raise RuntimeError(
            f"web/index.html not found: {WEB / 'index.html'}"
        )

    log.info("Starting XAUUSD Mini App...")
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

    for command, handler in [
        ("start", cmd_start),
        ("signal", cmd_signal),
        ("market", cmd_market),
        ("news", cmd_news),
        ("chatid", cmd_chatid)
    ]:
        bot_app.add_handler(
            CommandHandler(command, handler)
        )

    webapp = web.Application()

    async def index(request):
        return web.FileResponse(WEB / "index.html")

    webapp.router.add_get("/", index)
    webapp.router.add_static("/static/", WEB)
    webapp.router.add_get("/api/health", api_health)
    webapp.router.add_get("/api/user", api_user)
    webapp.router.add_get("/api/market", api_market)
    webapp.router.add_get("/api/signal", api_signal)
    webapp.router.add_get("/api/history", api_history)
    webapp.router.add_get("/api/news", api_news)

    runner = web.AppRunner(webapp)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )
    await site.start()

    log.info("Web server started on port %s", PORT)

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    log.info("Telegram bot started successfully.")

    try:
        await asyncio.Event().wait()
    finally:
        if bot_app.updater.running:
            await bot_app.updater.stop()

        if bot_app.running:
            await bot_app.stop()

        await runner.cleanup()
        await close_http_session()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
