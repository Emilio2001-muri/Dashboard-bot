"""
NEXUS Trading Dashboard — Data Pusher
======================================
Runs LOCALLY on your PC where MetaTrader 5 is installed.
Collects data from MT5 + free APIs and pushes to Supabase.
The Vercel frontend reads from Supabase in real-time.

Usage:
  1. Copy .env.example to .env and fill in your Supabase credentials
  2. pip install -r requirements.txt
  3. python pusher.py
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests
import feedparser
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    sys.exit(1)

# MT5 Config
MT5_SYMBOLS = ["GOLD#", "BTCUSD#"]
INITIAL_BALANCE = 1000.0
PNL_TARGETS = {"daily": 30.0, "weekly": 150.0, "monthly": 500.0}
MAX_DAILY_LOSS_PCT = 3.0

# API URLs
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
FEAR_GREED_URL = "https://api.alternative.me/fng/"
CALENDAR_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

# Refresh intervals (seconds)
INTERVALS = {
    "prices": 90,
    "mt5": 10,
    "news": 300,
    "calendar": 600,
    "whale": 60,
    "indicators": 180,
}

# News RSS feeds
NEWS_FEEDS = {
    "gold": [
        {"name": "Kitco Gold News", "url": "https://www.kitco.com/rss/gold.xml"},
        {"name": "FXStreet Gold", "url": "https://www.fxstreet.com/rss/gold-news"},
    ],
    "btc": [
        {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
        {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/feed"},
        {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss"},
    ],
    "macro": [
        {"name": "CNBC Economy", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"},
    ],
}

# Whale alerts file path (optional, for local whale_flow_tracker.py)
WHALE_ALERTS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "VS code", "Opus4.6", "Python", "whale_alerts.json"
)

# MT4 bridge file path (NexusBridge.mq4 writes data here)
MT4_BRIDGE_FILE = os.path.join(
    os.environ.get("APPDATA", ""),
    "MetaQuotes", "Terminal",
    "98A82F92176B73A2100FCD1F8ABD7255",
    "MQL4", "Files", "nexus_bridge.json"
)
MT4_ENABLED = True  # Set False to disable MT4 data

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pusher")

# ============================================================
# SUPABASE CLIENT
# ============================================================
from supabase import create_client

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def push(key: str, value):
    """Upsert a row in dashboard_cache"""
    try:
        # Serialize to JSON-safe format (handles datetime, etc.)
        safe_value = json.loads(json.dumps(value, default=str))
        sb.table("dashboard_cache").upsert({
            "key": key,
            "value": safe_value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"Supabase push [{key}]: {e}")


# ============================================================
# MT5 DATA FETCHER
# ============================================================
MT5_AVAILABLE = False
mt5 = None
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    logger.warning("MetaTrader5 not installed. MT5 data will be empty.")


class MT5Fetcher:
    def __init__(self):
        self.connected = False
        self._last_health = 0
        self._reconnect_interval = 300  # Force reconnect every 5 min

    def connect(self):
        if not MT5_AVAILABLE:
            return False
        try:
            # Always shutdown first to clear stale connections
            try:
                mt5.shutdown()
            except Exception:
                pass
            time.sleep(0.3)
            if not mt5.initialize():
                err = mt5.last_error()
                logger.error(f"MT5 initialize failed: {err}")
                self.connected = False
                return False
            info = mt5.account_info()
            if info:
                self.connected = True
                self._last_health = time.time()
                logger.info(f"MT5 connected: #{info.login} ({info.name}) Balance: ${info.balance}")
                return True
            else:
                logger.error("MT5 account_info returned None after initialize")
                self.connected = False
        except Exception as e:
            logger.error(f"MT5 connect error: {e}")
            self.connected = False
        return False

    def health_check(self):
        """Verify MT5 connection is alive, reconnect if stale"""
        if not self.connected or not MT5_AVAILABLE:
            return False
        try:
            info = mt5.account_info()
            if info is None:
                logger.warning("MT5 health check FAILED — connection stale, reconnecting...")
                self.connected = False
                return self.connect()
            self._last_health = time.time()
            return True
        except Exception as e:
            logger.warning(f"MT5 health check error: {e} — reconnecting...")
            self.connected = False
            return self.connect()

    def get_account_info(self):
        if not self.connected:
            return {"connected": False, "balance": INITIAL_BALANCE, "equity": INITIAL_BALANCE}
        try:
            info = mt5.account_info()
            if info:
                return {
                    "connected": True,
                    "login": info.login,
                    "name": info.name,
                    "server": info.server,
                    "balance": info.balance,
                    "equity": info.equity,
                    "margin": info.margin,
                    "margin_free": info.margin_free,
                    "margin_level": info.margin_level if info.margin_level else 0,
                    "profit": info.profit,
                    "leverage": info.leverage,
                    "type": "demo" if info.trade_mode == 0 else "real",
                }
        except Exception as e:
            logger.error(f"Account info error: {e}")
        return {"connected": False, "balance": INITIAL_BALANCE, "equity": INITIAL_BALANCE}

    def get_open_positions(self):
        if not self.connected:
            return []
        try:
            positions = mt5.positions_get()
            if positions is None:
                # Check if connection died
                err = mt5.last_error()
                if err and err[0] != 0:
                    logger.warning(f"positions_get error: {err} — connection may be stale")
                    self.connected = False
                return []
            if len(positions) == 0:
                return []
            return [{
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "swap": p.swap,
                "magic": p.magic,
                "time": datetime.fromtimestamp(p.time_setup).isoformat(),
                "platform": "MT5",
            } for p in positions]
        except Exception as e:
            logger.error(f"Positions error: {e}")
            self.connected = False
        return []

    def get_trade_history(self, days=30):
        if not self.connected:
            return []
        try:
            from_date = datetime.now() - timedelta(days=days)
            to_date = datetime.now() + timedelta(days=1)  # +1 to include today
            deals = mt5.history_deals_get(from_date, to_date)
            if not deals:
                return []
            result = []
            for deal in deals:
                if deal.entry == 1:  # Exit deals only
                    result.append({
                        "ticket": deal.ticket,
                        "order": deal.order,
                        "symbol": deal.symbol,
                        "type": "BUY" if deal.type == 0 else "SELL",
                        "volume": deal.volume,
                        "price": deal.price,
                        "profit": deal.profit,
                        "swap": deal.swap,
                        "commission": deal.commission,
                        "magic": deal.magic,
                        "comment": deal.comment,
                        "time": datetime.fromtimestamp(deal.time).isoformat(),
                        "net_pnl": deal.profit + deal.swap + deal.commission,
                    })
            return result
        except Exception as e:
            logger.error(f"History error: {e}")
        return []

    def get_pnl_summary(self):
        account = self.get_account_info()
        history = self.get_trade_history(90)
        positions = self.get_open_positions()

        now = datetime.now()
        today = now.date()
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)

        daily_pnl = weekly_pnl = monthly_pnl = total_pnl = 0.0
        total_trades = winning_trades = losing_trades = 0
        daily_trades = []

        for trade in history:
            trade_date = datetime.fromisoformat(trade["time"]).date()
            net = trade["net_pnl"]
            total_pnl += net
            total_trades += 1
            if net > 0:
                winning_trades += 1
            elif net < 0:
                losing_trades += 1
            if trade_date == today:
                daily_pnl += net
            if trade_date >= week_start:
                weekly_pnl += net
            if trade_date >= month_start:
                monthly_pnl += net
            daily_trades.append({
                "date": trade_date.isoformat(),
                "pnl": net,
                "cumulative": total_pnl,
            })

        floating_pnl = sum(p["profit"] for p in positions)
        wr = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        avg_win = sum(t["net_pnl"] for t in history if t["net_pnl"] > 0) / max(winning_trades, 1)
        avg_loss = sum(t["net_pnl"] for t in history if t["net_pnl"] < 0) / max(losing_trades, 1)
        gross_wins = abs(sum(t["net_pnl"] for t in history if t["net_pnl"] > 0))
        gross_losses = abs(sum(t["net_pnl"] for t in history if t["net_pnl"] < 0))
        profit_factor = gross_wins / max(gross_losses, 0.01)

        peak = INITIAL_BALANCE
        max_dd = 0.0
        running = INITIAL_BALANCE
        for trade in history:
            running += trade["net_pnl"]
            if running > peak:
                peak = running
            dd = (peak - running) / peak * 100
            if dd > max_dd:
                max_dd = dd

        bal = account.get("balance", INITIAL_BALANCE)
        return {
            "balance": bal,
            "equity": account.get("equity", INITIAL_BALANCE),
            "floating_pnl": floating_pnl,
            "daily_pnl": daily_pnl,
            "weekly_pnl": weekly_pnl,
            "monthly_pnl": monthly_pnl,
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": round(wr, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown": round(max_dd, 2),
            "open_positions": len(positions),
            "daily_target": PNL_TARGETS["daily"],
            "weekly_target": PNL_TARGETS["weekly"],
            "monthly_target": PNL_TARGETS["monthly"],
            "daily_progress": round(daily_pnl / PNL_TARGETS["daily"] * 100, 1) if PNL_TARGETS["daily"] else 0,
            "weekly_progress": round(weekly_pnl / PNL_TARGETS["weekly"] * 100, 1) if PNL_TARGETS["weekly"] else 0,
            "monthly_progress": round(monthly_pnl / PNL_TARGETS["monthly"] * 100, 1) if PNL_TARGETS["monthly"] else 0,
            "daily_trades": daily_trades[-30:],
            "max_daily_loss_alert": daily_pnl < -(bal * MAX_DAILY_LOSS_PCT / 100),
            "connected": account.get("connected", False),
            "account_login": account.get("login", 0),
            "account_server": account.get("server", ""),
            "account_name": account.get("name", ""),
        }

    def get_indicators(self, symbol):
        if not self.connected:
            return {}
        try:
            tf = mt5.TIMEFRAME_M5
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, 200)
            if rates is None or len(rates) < 50:
                return {}

            closes = [r[4] for r in rates]  # close price
            highs = [r[2] for r in rates]
            lows = [r[3] for r in rates]

            def ema(data, period):
                result = [data[0]]
                k = 2 / (period + 1)
                for i in range(1, len(data)):
                    result.append(data[i] * k + result[-1] * (1 - k))
                return result

            ema8 = ema(closes, 8)
            ema21 = ema(closes, 21)
            ema50 = ema(closes, 50)

            # RSI
            gains = losses_r = []
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            gain_sum = sum(max(d, 0) for d in deltas[-14:]) / 14
            loss_sum = sum(abs(min(d, 0)) for d in deltas[-14:]) / 14
            rs = gain_sum / max(loss_sum, 0.0001)
            rsi = 100 - (100 / (1 + rs))

            # MACD
            ema12 = ema(closes, 12)
            ema26 = ema(closes, 26)
            macd_line = ema12[-1] - ema26[-1]

            # ATR
            trs = []
            for i in range(1, len(closes)):
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                trs.append(tr)
            atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0

            # Bollinger Bands
            sma20 = sum(closes[-20:]) / 20
            std20 = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5
            bb_upper = sma20 + 2 * std20
            bb_lower = sma20 - 2 * std20

            # ADX (simplified)
            adx = 25  # Simplified — full ADX calculation would be ~50 lines

            # Spread
            tick = mt5.symbol_info_tick(symbol)
            spread = (tick.ask - tick.bid) if tick else 0

            return {
                "symbol": symbol,
                "price": closes[-1],
                "ema8": round(ema8[-1], 2),
                "ema21": round(ema21[-1], 2),
                "ema50": round(ema50[-1], 2),
                "rsi": round(rsi, 1),
                "macd": round(macd_line, 4),
                "adx": adx,
                "atr": round(atr, 4),
                "bb_upper": round(bb_upper, 2),
                "bb_lower": round(bb_lower, 2),
                "spread": round(spread, 2),
                "source": "mt5",
            }
        except Exception as e:
            logger.error(f"Indicators error [{symbol}]: {e}")
        return {}


# ============================================================
# MARKET DATA FETCHER (CoinGecko, Fear & Greed)
# ============================================================
class MarketFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "NexusDashboard/3.0", "Accept": "application/json"})
        self._cache = {}

    def _get_cached(self, url, cache_key, ttl=120):
        """GET with caching to avoid rate limits"""
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < ttl:
            return cached["data"]
        try:
            r = self.session.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                self._cache[cache_key] = {"data": data, "ts": time.time()}
                return data
            elif r.status_code == 429:
                logger.warning(f"Rate limited on {cache_key}, using cache")
                return cached["data"] if cached else None
        except Exception as e:
            logger.error(f"Fetch {cache_key}: {e}")
        return cached["data"] if cached else None

    def get_prices(self):
        result = {"btc": {}, "gold": {}}
        # BTC
        btc = self._get_cached(
            f"{COINGECKO_BASE}/coins/bitcoin?localization=false&tickers=false&community_data=false&developer_data=false",
            "btc_price", 90
        )
        if btc and "market_data" in btc:
            md = btc["market_data"]
            result["btc"] = {
                "price": md.get("current_price", {}).get("usd", 0),
                "change_24h": md.get("price_change_percentage_24h", 0),
                "high_24h": md.get("high_24h", {}).get("usd", 0),
                "low_24h": md.get("low_24h", {}).get("usd", 0),
                "total_volume": md.get("total_volume", {}).get("usd", 0),
                "market_cap": md.get("market_cap", {}).get("usd", 0),
            }
        # Gold (PAXG proxy)
        gold = self._get_cached(
            f"{COINGECKO_BASE}/coins/pax-gold?localization=false&tickers=false&community_data=false&developer_data=false",
            "gold_price", 90
        )
        if gold and "market_data" in gold:
            md = gold["market_data"]
            result["gold"] = {
                "price": md.get("current_price", {}).get("usd", 0),
                "change_24h": md.get("price_change_percentage_24h", 0),
                "high_24h": md.get("high_24h", {}).get("usd", 0),
                "low_24h": md.get("low_24h", {}).get("usd", 0),
                "total_volume": md.get("total_volume", {}).get("usd", 0),
            }
        return result

    def get_fear_greed(self):
        data = self._get_cached(FEAR_GREED_URL, "fear_greed", 300)
        if data and "data" in data and data["data"]:
            fg = data["data"][0]
            return {
                "value": int(fg.get("value", 0)),
                "classification": fg.get("value_classification", ""),
                "timestamp": fg.get("timestamp", ""),
            }
        return {"value": 50, "classification": "Neutral"}

    def get_market_overview(self):
        data = self._get_cached(f"{COINGECKO_BASE}/global", "global", 120)
        if data and "data" in data:
            d = data["data"]
            return {
                "total_market_cap": d.get("total_market_cap", {}).get("usd", 0),
                "total_volume": d.get("total_volume", {}).get("usd", 0),
                "btc_dominance": d.get("market_cap_percentage", {}).get("btc", 0),
                "market_cap_change_24h": d.get("market_cap_change_percentage_24h_usd", 0),
            }
        return {}


# ============================================================
# NEWS FETCHER
# ============================================================
class NewsFetcher:
    def __init__(self):
        self._cache = {}

    def get_all_news(self):
        all_news = []
        for category, feeds in NEWS_FEEDS.items():
            for feed_info in feeds:
                try:
                    cached = self._cache.get(feed_info["url"])
                    if cached and (time.time() - cached["ts"]) < 300:
                        all_news.extend(cached["data"])
                        continue

                    feed = feedparser.parse(feed_info["url"])
                    items = []
                    for entry in feed.entries[:5]:
                        title = entry.get("title", "")
                        items.append({
                            "title": title,
                            "link": entry.get("link", ""),
                            "published": entry.get("published", ""),
                            "source": feed_info["name"],
                            "category": category,
                            "sentiment": self._simple_sentiment(title),
                        })
                    self._cache[feed_info["url"]] = {"data": items, "ts": time.time()}
                    all_news.extend(items)
                except Exception:
                    cached = self._cache.get(feed_info["url"])
                    if cached:
                        all_news.extend(cached["data"])
        return sorted(all_news, key=lambda x: x.get("published", ""), reverse=True)[:30]

    def _simple_sentiment(self, title):
        t = title.lower()
        bullish = ["surge", "rally", "bull", "high", "gain", "rise", "soar", "jump", "up", "record", "breakout"]
        bearish = ["crash", "bear", "drop", "fall", "plunge", "low", "down", "sell", "panic", "fear", "collapse"]
        b = sum(1 for w in bullish if w in t)
        s = sum(1 for w in bearish if w in t)
        if b > s:
            return "bullish"
        elif s > b:
            return "bearish"
        return "neutral"


# ============================================================
# CALENDAR FETCHER
# ============================================================
class CalendarFetcher:
    def get_events(self):
        all_events = []
        for url in CALENDAR_URLS:
            try:
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    all_events.extend(r.json())
            except Exception as e:
                logger.error(f"Calendar fetch error: {e}")

        result = []
        now = datetime.now()
        # Show: past 2 days + next 10 days (covers full current + next week)
        window_start = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        window_end = (now + timedelta(days=10)).strftime("%Y-%m-%d")

        for ev in all_events:
            impact = ev.get("impact", "").lower()
            if impact not in ["high", "medium", "low"]:
                continue
            raw_date = ev.get("date", "")
            event_day = raw_date[:10] if raw_date else ""
            if not event_day or event_day < window_start or event_day > window_end:
                continue
            # Format time for display
            time_display = raw_date
            try:
                from dateutil.parser import parse as dt_parse
                dt = dt_parse(raw_date)
                if dt.date() == now.date():
                    time_display = f"Today {dt.strftime('%H:%M')}"
                elif dt.date() == (now + timedelta(days=1)).date():
                    time_display = f"Tomorrow {dt.strftime('%H:%M')}"
                elif dt.date() == (now - timedelta(days=1)).date():
                    time_display = f"Yesterday {dt.strftime('%H:%M')}"
                else:
                    time_display = dt.strftime("%a %d %H:%M")
            except Exception:
                time_display = raw_date[:16] if len(raw_date) > 16 else raw_date
            # Determine if past
            is_past = event_day < now.strftime("%Y-%m-%d")
            result.append({
                "title": ev.get("title", "Unknown"),
                "country": ev.get("country", ""),
                "date": raw_date,
                "time": time_display,
                "impact": impact,
                "forecast": ev.get("forecast", ""),
                "previous": ev.get("previous", ""),
                "actual": ev.get("actual", ""),
                "is_past": is_past,
            })
        # Remove duplicates by title+date
        seen = set()
        unique = []
        for e in result:
            key = f"{e['title']}_{e['date']}"
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return sorted(unique, key=lambda x: x.get("date", ""))


# ============================================================
# WHALE FLOW READER
# ============================================================
class WhaleReader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "NexusDashboard/3.0"})

    def get_alerts(self):
        alerts = []
        btc_price = 100000

        # 1. Local whale_alerts.json (if whale_flow_tracker.py is running)
        try:
            if os.path.exists(WHALE_ALERTS_FILE):
                with open(WHALE_ALERTS_FILE, "r") as f:
                    local = json.load(f)
                    if isinstance(local, list):
                        alerts.extend(local)
        except Exception:
            pass

        # 2. Mempool.space — recent large BTC transactions
        try:
            r = self.session.get("https://mempool.space/api/mempool/recent", timeout=8)
            if r.status_code == 200:
                txs = r.json()
                if txs:
                    sorted_txs = sorted(txs, key=lambda t: t.get("value", 0), reverse=True)
                    shown = 0
                    for tx in sorted_txs[:5]:
                        val_btc = tx.get("value", 0) / 1e8
                        if val_btc >= 0.5 or shown == 0:
                            emoji = "🐋" if val_btc >= 50 else "🐬" if val_btc >= 10 else "🐟" if val_btc >= 1 else "🔹"
                            usd = val_btc * btc_price
                            alerts.append({
                                "message": f"{emoji} BTC Mempool: {val_btc:,.4f} BTC (~${usd:,.0f})",
                                "time": datetime.now().strftime("%H:%M"),
                                "type": "btc_whale",
                                "amount_btc": val_btc,
                            })
                            shown += 1
                        if shown >= 3:
                            break
        except Exception as e:
            logger.debug(f"Mempool error: {e}")

        # 3. Mempool stats
        try:
            r = self.session.get("https://mempool.space/api/mempool", timeout=5)
            if r.status_code == 200:
                mp = r.json()
                count = mp.get("count", 0)
                vsize = mp.get("vsize", 0)
                alerts.append({
                    "message": f"📊 Mempool: {count:,} txs | {vsize / 1e6:.1f} vMB",
                    "time": datetime.now().strftime("%H:%M"),
                    "type": "mempool_stats",
                })
        except Exception:
            pass

        # 4. Fee estimates
        try:
            r = self.session.get("https://mempool.space/api/v1/fees/recommended", timeout=5)
            if r.status_code == 200:
                fees = r.json()
                alerts.append({
                    "message": f"⛽ Fees: {fees.get('fastestFee', '?')} sat/vB fast | {fees.get('halfHourFee', '?')} 30min | {fees.get('hourFee', '?')} 1h",
                    "time": datetime.now().strftime("%H:%M"),
                    "type": "fees",
                })
        except Exception:
            pass

        return alerts


# ============================================================
# MT4 BRIDGE READER
# ============================================================
class MT4Reader:
    """Reads MT4 data exported by NexusBridge.mq4 EA"""

    def __init__(self, filepath):
        self.filepath = filepath
        self._last_data = None
        self._last_mtime = 0

    def read(self):
        try:
            if not os.path.exists(self.filepath):
                return None
            mtime = os.path.getmtime(self.filepath)
            if mtime == self._last_mtime and self._last_data:
                return self._last_data
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._last_mtime = mtime
            self._last_data = data
            return data
        except json.JSONDecodeError as e:
            logger.debug(f"MT4 bridge JSON decode error (file being written?): {e}")
            return self._last_data
        except Exception as e:
            logger.error(f"MT4 bridge read error: {e}")
            return self._last_data

    def get_account_info(self):
        data = self.read()
        if not data:
            return None
        return data.get("account", None)

    def get_open_positions(self):
        data = self.read()
        if not data:
            return []
        return data.get("positions", [])

    def get_trade_history(self):
        data = self.read()
        if not data:
            return []
        return data.get("history", [])

    @property
    def available(self):
        return os.path.exists(self.filepath)


# ============================================================
# MAIN LOOP
# ============================================================
def main():
    logger.info("=" * 50)
    logger.info("  NEXUS Data Pusher — Starting")
    logger.info("=" * 50)
    logger.info(f"Supabase: {SUPABASE_URL[:40]}...")

    # Initialize fetchers
    mt5f = MT5Fetcher()
    market = MarketFetcher()
    news = NewsFetcher()
    calendar = CalendarFetcher()
    whale = WhaleReader()
    mt4r = MT4Reader(MT4_BRIDGE_FILE) if MT4_ENABLED else None

    # Connect MT5
    if MT5_AVAILABLE:
        mt5f.connect()

    # MT4 bridge status
    if mt4r:
        if mt4r.available:
            logger.info(f"MT4 bridge file found: {MT4_BRIDGE_FILE}")
        else:
            logger.info(f"MT4 bridge file not found yet (waiting for NexusBridge EA): {MT4_BRIDGE_FILE}")

    # Track last refresh times
    last = {k: 0 for k in INTERVALS}
    last["mt4"] = 0

    logger.info("Entering main loop... Press Ctrl+C to stop.")
    logger.info("")

    while True:
        try:
            now = time.time()

            # --- PRICES (every 90s) ---
            if now - last["prices"] >= INTERVALS["prices"]:
                try:
                    prices = market.get_prices()
                    push("prices", prices)
                    fg = market.get_fear_greed()
                    push("fear_greed", fg)
                    overview = market.get_market_overview()
                    push("market_overview", overview)
                    logger.info(f"[PRICES] Gold: ${prices.get('gold',{}).get('price',0):,.0f} | BTC: ${prices.get('btc',{}).get('price',0):,.0f}")
                except Exception as e:
                    logger.error(f"Price refresh error: {e}")
                last["prices"] = now

            # --- MT5 (every 10s) ---
            if now - last["mt5"] >= INTERVALS["mt5"]:
                try:
                    if mt5f.connected:
                        # Periodic force reconnect to prevent stale connections
                        if now - mt5f._last_health >= mt5f._reconnect_interval:
                            logger.info("MT5 periodic reconnect (keep-alive)...")
                            mt5f.connect()
                        else:
                            # Quick health check
                            mt5f.health_check()

                    if mt5f.connected:
                        pnl = mt5f.get_pnl_summary()
                        push("mt5_pnl", pnl)
                        positions = mt5f.get_open_positions()
                        push("mt5_trades", positions)
                        history = mt5f.get_trade_history(30)
                        push("mt5_history", history)
                        account = mt5f.get_account_info()
                        push("mt5_account", account)
                        logger.debug(f"[MT5] Balance: ${pnl.get('balance',0):,.2f} | Positions: {len(positions)} | Daily: ${pnl.get('daily_pnl',0):,.2f}")
                    else:
                        # Try to reconnect — do NOT push empty/zero data
                        # The last good data stays cached in Supabase
                        if MT5_AVAILABLE:
                            logger.info("MT5 disconnected, attempting reconnect...")
                            mt5f.connect()
                except Exception as e:
                    logger.error(f"MT5 refresh error: {e}")
                    mt5f.connected = False
                last["mt5"] = now

            # --- MT4 (every 10s) ---
            if mt4r and now - last["mt4"] >= INTERVALS["mt5"]:
                try:
                    if mt4r.available:
                        mt4_positions = mt4r.get_open_positions()
                        push("mt4_trades", mt4_positions)
                        mt4_account = mt4r.get_account_info()
                        if mt4_account:
                            push("mt4_account", mt4_account)
                        mt4_history = mt4r.get_trade_history()
                        push("mt4_history", mt4_history)
                        logger.debug(f"[MT4] Positions: {len(mt4_positions)} | History: {len(mt4_history)}")
                except Exception as e:
                    logger.error(f"MT4 refresh error: {e}")
                last["mt4"] = now

            # --- NEWS (every 300s) ---
            if now - last["news"] >= INTERVALS["news"]:
                try:
                    articles = news.get_all_news()
                    push("news", articles)
                    logger.info(f"[NEWS] {len(articles)} articles")
                except Exception as e:
                    logger.error(f"News refresh error: {e}")
                last["news"] = now

            # --- CALENDAR (every 600s) ---
            if now - last["calendar"] >= INTERVALS["calendar"]:
                try:
                    events = calendar.get_events()
                    push("calendar", events)
                    logger.info(f"[CALENDAR] {len(events)} events")
                except Exception as e:
                    logger.error(f"Calendar refresh error: {e}")
                last["calendar"] = now

            # --- WHALE (every 60s) ---
            if now - last["whale"] >= INTERVALS["whale"]:
                try:
                    alerts = whale.get_alerts()
                    push("whale_alerts", alerts)
                    logger.debug(f"[WHALE] {len(alerts)} alerts")
                except Exception as e:
                    logger.error(f"Whale refresh error: {e}")
                last["whale"] = now

            # --- INDICATORS (every 180s) ---
            if now - last["indicators"] >= INTERVALS["indicators"]:
                try:
                    if mt5f.connected:
                        indicators = {}
                        for symbol in MT5_SYMBOLS:
                            ind = mt5f.get_indicators(symbol)
                            if ind:
                                indicators[symbol] = ind
                        if indicators:
                            push("indicators", indicators)
                            logger.info(f"[INDICATORS] {len(indicators)} symbols")
                    # else: keep cached indicators in Supabase
                except Exception as e:
                    logger.error(f"Indicators refresh error: {e}")
                last["indicators"] = now

            # Sleep 1 second between checks
            time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            if MT5_AVAILABLE:
                mt5.shutdown()
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
