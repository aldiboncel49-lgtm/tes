"""
╔══════════════════════════════════════════════════════════════════╗
║          NDA4 AI AGENT SOLANA TRADING BOT                     ║
║          Auto Trade | Auto Learn | Auto Improve                  ║
╚══════════════════════════════════════════════════════════════════╝

Features:
- Real-time on-chain scanning (DexScreener + Jupiter + Birdeye)
- AI Agent decides entry type (limit/market), TP, SL
- Auto-improve filters setelah 3x loss berturut-turut
- Telegram notifications per trade + report tiap 1 jam
- Win Rate tracking & simulasi dengan balance 1 SOL
"""

import asyncio
import aiohttp
import json
import logging
import os
import time
import random
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field, asdict
from collections import deque

# ──────────────────────────────────────────────────────────────────
# CONFIG — isi semua ini di .env atau langsung di sini
# ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "YOUR_OPENROUTER_KEY")
OPENROUTER_MODEL    = os.getenv("OPENROUTER_MODEL", "openrouter/owl-alpha")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

# Trading parameters
INITIAL_BALANCE_SOL      = float(os.getenv("INITIAL_BALANCE", "1.0"))
TRADE_SIZE_SOL           = float(os.getenv("TRADE_SIZE", "0.03"))
MAX_OPEN_TRADES          = int(os.getenv("MAX_OPEN_TRADES", "5"))
SCAN_INTERVAL_SEC        = int(os.getenv("SCAN_INTERVAL", "60"))
REPORT_INTERVAL_SEC      = 3600
TARGET_WIN_RATE          = 0.55
CONSECUTIVE_LOSS_TRIGGER = 3

# ──────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────────

@dataclass
class FilterConfig:
    """AI-tunable filter parameters — auto-improved saat sering loss"""
    min_liquidity_usd: float = 30_000
    min_volume_24h_usd: float = 50_000
    min_holder_count: int = 100
    max_price_impact_pct: float = 3.0
    min_price_change_5m: float = 2.0
    max_price_change_5m: float = 25.0
    min_tx_count_5m: int = 20
    min_buy_sell_ratio: float = 1.5
    min_market_cap_usd: float = 50_000
    max_market_cap_usd: float = 5_000_000
    require_renounced: bool = False
    require_burned_lp: bool = False
    bundle_threshold: float = 0.25
    min_age_minutes: int = 5
    max_age_hours: int = 24
    tp_multiplier: float = 1.25
    sl_multiplier: float = 0.92
    generation: int = 1

    def to_prompt(self) -> str:
        return f"""
CURRENT FILTER CONFIG (Generation {self.generation}):
- Minimum Liquidity: ${self.min_liquidity_usd:,.0f}
- Minimum 24h Volume: ${self.min_volume_24h_usd:,.0f}
- Minimum Holders: {self.min_holder_count}
- Max Price Impact: {self.max_price_impact_pct}%
- Price Change 5m Range: {self.min_price_change_5m}% - {self.max_price_change_5m}%
- Min Tx 5m: {self.min_tx_count_5m}
- Min Buy/Sell Ratio: {self.min_buy_sell_ratio}x
- Market Cap Range: ${self.min_market_cap_usd:,.0f} - ${self.max_market_cap_usd:,.0f}
- Max Bundle %: {self.bundle_threshold*100:.0f}%
- Token Age: {self.min_age_minutes}m - {self.max_age_hours}h
- TP: +{(self.tp_multiplier-1)*100:.0f}% | SL: -{(1-self.sl_multiplier)*100:.0f}%
"""


@dataclass
class TokenData:
    """Raw data dari scanner"""
    address: str
    symbol: str
    name: str
    price_usd: float
    price_sol: float
    liquidity_usd: float
    volume_24h: float
    volume_5m: float
    price_change_5m: float
    price_change_1h: float
    price_change_24h: float
    market_cap: float
    holder_count: int
    tx_count_5m: int
    buy_count_5m: int
    sell_count_5m: int
    age_minutes: float
    dex: str
    pair_address: str
    scanned_at: float = field(default_factory=time.time)


@dataclass
class Trade:
    """Satu posisi trading"""
    id: str
    token_address: str
    token_symbol: str
    entry_type: str
    entry_price: float
    entry_price_sol: float
    tp_price: float
    sl_price: float
    size_sol: float
    status: str
    opened_at: float
    closed_at: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_sol: Optional[float] = None
    pnl_pct: Optional[float] = None
    ai_reasoning: str = ""
    current_price: float = 0.0
    last_updated: float = field(default_factory=time.time)

    @property
    def is_active(self):
        return self.status in ("open", "pending_limit")

    @property
    def pnl_display(self):
        if self.pnl_sol is None:
            if self.status == "open":
                pnl = (self.current_price - self.entry_price) / self.entry_price * self.size_sol
                return f"{pnl:+.4f} SOL ({(self.current_price/self.entry_price-1)*100:+.1f}%)"
            return "—"
        return f"{self.pnl_sol:+.4f} SOL ({self.pnl_pct:+.1f}%)"


@dataclass
class BotState:
    """State lengkap bot"""
    balance_sol: float = INITIAL_BALANCE_SOL
    trades: list = field(default_factory=list)
    filters: FilterConfig = field(default_factory=FilterConfig)
    consecutive_losses: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_sol: float = 0.0
    improve_count: int = 0
    started_at: float = field(default_factory=time.time)
    last_report_at: float = field(default_factory=time.time)
    scanned_tokens_today: int = 0
    skipped_tokens_today: int = 0
    recent_losses: deque = field(default_factory=lambda: deque(maxlen=10))

    @property
    def win_rate(self) -> float:
        finished = self.winning_trades + self.losing_trades
        return self.winning_trades / finished if finished > 0 else 0.0

    @property
    def open_trades(self):
        return [t for t in self.trades if t.is_active]

    @property
    def finished_trades(self):
        return [t for t in self.trades if not t.is_active]

    def can_open_trade(self) -> bool:
        return (
            len(self.open_trades) < MAX_OPEN_TRADES
            and self.balance_sol >= TRADE_SIZE_SOL
        )


# ──────────────────────────────────────────────────────────────────
# SCANNER — Real-time DexScreener scan
# ──────────────────────────────────────────────────────────────────

class SolanaScanner:
    BASE_URL = "https://api.dexscreener.com/latest/dex"

    async def get_trending_tokens(self, session: aiohttp.ClientSession) -> list[TokenData]:
        tokens = []
        try:
            url = f"{self.BASE_URL}/search?q=SOL"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return tokens
                data = await r.json()
                pairs = data.get("pairs", [])

            sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            for p in sol_pairs[:50]:
                try:
                    token = self._parse_pair(p)
                    if token:
                        tokens.append(token)
                except Exception:
                    continue
        except Exception as e:
            logging.warning(f"Scanner error: {e}")
        return tokens

    async def get_new_tokens(self, session: aiohttp.ClientSession) -> list[TokenData]:
        tokens = []
        try:
            url = f"{self.BASE_URL}/tokens/solana"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return tokens
                data = await r.json()
                pairs = data.get("pairs", [])

            for p in pairs[:30]:
                try:
                    token = self._parse_pair(p)
                    if token:
                        tokens.append(token)
                except Exception:
                    continue
        except Exception as e:
            logging.warning(f"New tokens scanner error: {e}")
        return tokens

    async def get_token_detail(self, session: aiohttp.ClientSession, address: str) -> Optional[TokenData]:
        try:
            url = f"{self.BASE_URL}/tokens/{address}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                pairs = data.get("pairs", [])
                if not pairs:
                    return None
                best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                return self._parse_pair(best)
        except Exception:
            return None

    def _parse_pair(self, p: dict) -> Optional[TokenData]:
        try:
            base = p.get("baseToken", {})
            price_usd = float(p.get("priceUsd", 0) or 0)
            price_native = float(p.get("priceNative", 0) or 0)
            liq = p.get("liquidity", {})
            vol = p.get("volume", {})
            pc = p.get("priceChange", {})
            txns_5m = p.get("txns", {}).get("m5", {})
            info = p.get("info", {})

            created_at = p.get("pairCreatedAt", 0)
            age_minutes = (time.time()*1000 - (created_at or time.time()*1000)) / 60000

            holders = 0
            for ext in (info.get("extensions") or []):
                if isinstance(ext, dict) and ext.get("type") == "holders":
                    holders = int(ext.get("value", 0))

            buy5 = int(txns_5m.get("buys", 0) or 0)
            sell5 = int(txns_5m.get("sells", 0) or 0)

            if price_usd <= 0:
                return None

            return TokenData(
                address=base.get("address", ""),
                symbol=base.get("symbol", "???"),
                name=base.get("name", "Unknown"),
                price_usd=price_usd,
                price_sol=price_native,
                liquidity_usd=float(liq.get("usd", 0) or 0),
                volume_24h=float(vol.get("h24", 0) or 0),
                volume_5m=float(vol.get("m5", 0) or 0),
                price_change_5m=float(pc.get("m5", 0) or 0),
                price_change_1h=float(pc.get("h1", 0) or 0),
                price_change_24h=float(pc.get("h24", 0) or 0),
                market_cap=float(p.get("marketCap", 0) or 0),
                holder_count=holders,
                tx_count_5m=buy5 + sell5,
                buy_count_5m=buy5,
                sell_count_5m=sell5,
                age_minutes=max(0, age_minutes),
                dex=p.get("dexId", "unknown"),
                pair_address=p.get("pairAddress", ""),
            )
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────────
# AI AGENT — OpenRouter (owl-alpha) decides entry, TP, SL, improves filters
# ──────────────────────────────────────────────────────────────────

class AIAgent:
    def __init__(self):
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://github.com/nda4-trading-bot",  # opsional, untuk OpenRouter ranking
            "X-Title": "NDA4 Solana Trading Bot",                   # opsional
        }

    async def _call_api(self, session: aiohttp.ClientSession, prompt: str, max_tokens: int = 400) -> Optional[str]:
        """Panggil OpenRouter API dan kembalikan teks response"""
        payload = {
            "model": OPENROUTER_MODEL,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }
        try:
            async with session.post(
                OPENROUTER_BASE_URL,
                headers=self.headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status != 200:
                    err = await r.text()
                    logging.error(f"OpenRouter API error {r.status}: {err}")
                    return None
                data = await r.json()
                text = data["choices"][0]["message"]["content"]
                return text.strip()
        except Exception as e:
            logging.error(f"OpenRouter call error: {e}")
            return None

    async def analyze_token(self, session: aiohttp.ClientSession, token: TokenData, state: BotState) -> Optional[dict]:
        """AI memutuskan apakah masuk trade dan bagaimana"""
        prompt = f"""
Kamu adalah AI trading agent Solana on-chain. Tugasmu: analisis token dan tentukan apakah layak di-trade.

{state.filters.to_prompt()}

TOKEN DATA (REAL-TIME):
- Symbol: {token.symbol} ({token.name})
- Address: {token.address}
- Price: ${token.price_usd:.8f} ({token.price_sol:.8f} SOL)
- Liquidity: ${token.liquidity_usd:,.0f}
- Volume 24h: ${token.volume_24h:,.0f}
- Volume 5m: ${token.volume_5m:,.0f}
- Price Change 5m: {token.price_change_5m:+.1f}%
- Price Change 1h: {token.price_change_1h:+.1f}%
- Price Change 24h: {token.price_change_24h:+.1f}%
- Market Cap: ${token.market_cap:,.0f}
- Holders: {token.holder_count}
- Tx 5m: {token.tx_count_5m} (Buy: {token.buy_count_5m} | Sell: {token.sell_count_5m})
- Age: {token.age_minutes:.0f} menit
- DEX: {token.dex}

BOT STATE:
- Balance: {state.balance_sol:.3f} SOL
- Trade size: {TRADE_SIZE_SOL} SOL
- Open trades: {len(state.open_trades)}/{MAX_OPEN_TRADES}
- Current WR: {state.win_rate*100:.1f}%
- Consecutive losses: {state.consecutive_losses}
- Total trades today: {state.total_trades}

INSTRUKSI:
1. Evaluasi token terhadap filter di atas
2. Tentukan apakah BUY, SKIP, atau WATCHLIST
3. Jika BUY, tentukan: entry_type (market/limit), entry_price, tp_price, sl_price
   - Jika "limit": entry sedikit di bawah harga sekarang (max -2%)
   - Jika "market": entry di harga sekarang
4. TP dan SL dalam USD
5. Pertimbangkan momentum, buy/sell ratio, liquidity depth

Jawab HANYA dengan JSON ini (tanpa backtick, tanpa penjelasan):
{{
  "action": "BUY" or "SKIP",
  "entry_type": "market" or "limit",
  "entry_price_usd": float,
  "tp_price_usd": float,
  "sl_price_usd": float,
  "confidence": 0-100,
  "reasoning": "singkat max 2 kalimat",
  "skip_reason": "jika SKIP, kenapa"
}}
"""
        text = await self._call_api(session, prompt, max_tokens=400)
        if not text:
            return None
        try:
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        except Exception as e:
            logging.error(f"AI analyze parse error: {e} | raw: {text}")
            return None

    async def improve_filters(self, session: aiohttp.ClientSession, state: BotState) -> tuple:
        """AI improve filter setelah 3x loss berturut-turut"""
        recent = state.recent_losses
        losses_info = "\n".join([
            f"- {t.token_symbol}: Entry ${t.entry_price:.6f}, Exit ${t.exit_price:.6f}, PnL {t.pnl_pct:+.1f}%"
            for t in list(recent)[-5:]
            if t.pnl_sol is not None
        ])

        current = state.filters
        prompt = f"""
Kamu adalah AI filter optimizer untuk trading bot Solana on-chain.

Bot mengalami {state.consecutive_losses} loss berturut-turut. Current win rate: {state.win_rate*100:.1f}% (target: 55%).
Ini generation ke-{current.generation} filter.

RECENT LOSSES:
{losses_info if losses_info else "Tidak ada detail loss tersedia"}

FILTER SAAT INI:
{current.to_prompt()}

TUGAS: Improve filter untuk meningkatkan win rate ke 55%+.
Strategi yang bisa dilakukan:
- Naikkan min_liquidity (lebih aman, kurang false signal)
- Naikkan min_volume_24h
- Perketat min_buy_sell_ratio
- Sesuaikan rentang price_change_5m
- Sesuaikan TP/SL ratio
- Naikkan min_age_minutes (hindari token terlalu baru)
- Perkecil max_market_cap (fokus ke token yang masih ada ruang naik)

Jawab HANYA dengan JSON (tanpa backtick):
{{
  "min_liquidity_usd": float,
  "min_volume_24h_usd": float,
  "min_holder_count": int,
  "max_price_impact_pct": float,
  "min_price_change_5m": float,
  "max_price_change_5m": float,
  "min_tx_count_5m": int,
  "min_buy_sell_ratio": float,
  "min_market_cap_usd": float,
  "max_market_cap_usd": float,
  "bundle_threshold": float,
  "min_age_minutes": int,
  "max_age_hours": int,
  "tp_multiplier": float,
  "sl_multiplier": float,
  "improvement_notes": "apa yang diubah dan kenapa"
}}
"""
        text = await self._call_api(session, prompt, max_tokens=600)
        if not text:
            return self._fallback_improve(current)

        try:
            text = text.replace("```json", "").replace("```", "").strip()
            data = json.loads(text)
            notes = data.pop("improvement_notes", "")
            new_filters = FilterConfig(**{k: v for k, v in data.items()}, generation=current.generation + 1)
            return new_filters, notes
        except Exception as e:
            logging.error(f"AI improve parse error: {e}")
            return self._fallback_improve(current)

    def _fallback_improve(self, current: FilterConfig) -> tuple:
        import copy
        f = copy.copy(current)
        f.min_liquidity_usd *= 1.3
        f.min_volume_24h_usd *= 1.3
        f.min_buy_sell_ratio = min(f.min_buy_sell_ratio + 0.2, 3.0)
        f.generation += 1
        return f, "Auto-tighten fallback: naikkan liquidity & volume requirement 30%"


# ──────────────────────────────────────────────────────────────────
# FILTER ENGINE
# ──────────────────────────────────────────────────────────────────

class FilterEngine:
    def passes(self, token: TokenData, cfg: FilterConfig) -> tuple[bool, str]:
        if token.liquidity_usd < cfg.min_liquidity_usd:
            return False, f"Liquidity ${token.liquidity_usd:,.0f} < ${cfg.min_liquidity_usd:,.0f}"
        if token.volume_24h < cfg.min_volume_24h_usd:
            return False, f"Volume24h ${token.volume_24h:,.0f} < ${cfg.min_volume_24h_usd:,.0f}"
        if token.price_change_5m < cfg.min_price_change_5m:
            return False, f"Momentum 5m {token.price_change_5m:+.1f}% terlalu lemah"
        if token.price_change_5m > cfg.max_price_change_5m:
            return False, f"Pump 5m {token.price_change_5m:+.1f}% terlalu tinggi (risiko dump)"
        if token.tx_count_5m < cfg.min_tx_count_5m:
            return False, f"Tx 5m {token.tx_count_5m} < {cfg.min_tx_count_5m}"
        if token.market_cap > 0 and token.market_cap < cfg.min_market_cap_usd:
            return False, f"MCap ${token.market_cap:,.0f} terlalu kecil"
        if token.market_cap > cfg.max_market_cap_usd:
            return False, f"MCap ${token.market_cap:,.0f} terlalu besar"
        if token.age_minutes < cfg.min_age_minutes:
            return False, f"Token terlalu baru ({token.age_minutes:.0f}m)"
        if token.age_minutes > cfg.max_age_hours * 60:
            return False, f"Token terlalu lama ({token.age_minutes/60:.1f}h)"
        if token.sell_count_5m > 0:
            ratio = token.buy_count_5m / token.sell_count_5m
            if ratio < cfg.min_buy_sell_ratio:
                return False, f"Buy/Sell ratio {ratio:.1f}x < {cfg.min_buy_sell_ratio}x"
        if token.holder_count > 0 and token.holder_count < cfg.min_holder_count:
            return False, f"Holders {token.holder_count} < {cfg.min_holder_count}"
        return True, ""


# ──────────────────────────────────────────────────────────────────
# TELEGRAM NOTIFIER
# ──────────────────────────────────────────────────────────────────

class TelegramBot:
    BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    async def send(self, session: aiohttp.ClientSession, text: str, parse_mode="HTML"):
        try:
            url = f"{self.BASE}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                result = await r.json()
                if not result.get("ok"):
                    logging.warning(f"Telegram error: {result}")
        except Exception as e:
            logging.error(f"Telegram send error: {e}")

    def format_trade_open(self, trade: Trade) -> str:
        icon = "🟡" if trade.entry_type == "limit" else "🟢"
        status = "LIMIT ORDER" if trade.entry_type == "limit" else "MARKET ENTRY"
        return f"""
{icon} <b>NEW TRADE — {status}</b>

🪙 Token: <b>${trade.token_symbol}</b>
📊 Entry Type: <b>{trade.entry_type.upper()}</b>
💰 Entry Price: <b>${trade.entry_price:.8f}</b>
🎯 Take Profit: <b>${trade.tp_price:.8f}</b> (+{(trade.tp_price/trade.entry_price-1)*100:.1f}%)
🛑 Stop Loss: <b>${trade.sl_price:.8f}</b> (-{(1-trade.sl_price/trade.entry_price)*100:.1f}%)
💼 Size: <b>{trade.size_sol} SOL</b>
🔗 <a href="https://dexscreener.com/solana/{trade.token_address}">DexScreener</a> | <a href="https://birdeye.so/token/{trade.token_address}">Birdeye</a>

📝 Reason: <i>{trade.ai_reasoning}</i>
🕐 {datetime.now().strftime('%H:%M:%S')}
""".strip()

    def format_trade_close(self, trade: Trade) -> str:
        won = trade.pnl_sol and trade.pnl_sol > 0
        icon = "✅" if won else "❌"
        return f"""
{icon} <b>TRADE CLOSED — {'PROFIT' if won else 'LOSS'}</b>

🪙 Token: <b>${trade.token_symbol}</b>
📊 Exit: <b>{trade.status.replace('_', ' ').upper()}</b>
💰 Entry: <b>${trade.entry_price:.8f}</b>
💱 Exit: <b>${trade.exit_price:.8f}</b>
{'📈' if won else '📉'} PnL: <b>{trade.pnl_sol:+.4f} SOL ({trade.pnl_pct:+.1f}%)</b>
🕐 {datetime.now().strftime('%H:%M:%S')}
""".strip()

    def format_filter_improve(self, old_gen: int, new_gen: int, notes: str) -> str:
        return f"""
🔧 <b>AI FILTER IMPROVED</b>

Generation: {old_gen} → {new_gen}
Trigger: 3x loss berturut-turut

📋 Perubahan:
<i>{notes}</i>

Bot akan lebih selektif dalam memilih entry. 💪
""".strip()

    def format_hourly_report(self, state: BotState) -> str:
        open_trades = state.open_trades
        finished = state.finished_trades
        pending = [t for t in open_trades if t.status == "pending_limit"]
        running = [t for t in open_trades if t.status == "open"]
        uptime = timedelta(seconds=int(time.time() - state.started_at))

        lines = [
            f"📊 <b>LAPORAN JAM — {datetime.now().strftime('%H:%M')} WIB</b>",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"⏱ Uptime: {str(uptime).split('.')[0]}",
            f"💼 Balance: <b>{state.balance_sol:.4f} SOL</b>",
            f"📈 Total PnL: <b>{state.total_pnl_sol:+.4f} SOL</b>",
            f"",
            f"📉 <b>STATISTIK TRADE</b>",
            f"Total Trade: {state.total_trades}",
            f"✅ Win: {state.winning_trades} | ❌ Loss: {state.losing_trades}",
            f"🎯 Win Rate: <b>{state.win_rate*100:.1f}%</b> (Target: 55%)",
            f"🔁 Filter Gen: #{state.filters.generation}",
            f"",
        ]

        if running:
            lines.append(f"🟢 <b>POSISI TERBUKA ({len(running)})</b>")
            for t in running:
                pct = (t.current_price - t.entry_price) / t.entry_price * 100 if t.current_price else 0
                pnl_sol = (t.current_price - t.entry_price) / t.entry_price * t.size_sol if t.current_price else 0
                lines.append(
                    f"• ${t.token_symbol} | Entry ${t.entry_price:.6f} | Now ${t.current_price:.6f} | "
                    f"{'📈' if pct >= 0 else '📉'} {pct:+.1f}% ({pnl_sol:+.4f} SOL)"
                )
                lines.append(f"  🎯 TP: ${t.tp_price:.6f} | 🛑 SL: ${t.sl_price:.6f}")
            lines.append("")

        if pending:
            lines.append(f"🟡 <b>LIMIT ORDER MENUNGGU ({len(pending)})</b>")
            for t in pending:
                lines.append(
                    f"• ${t.token_symbol} | Limit Entry: ${t.entry_price:.6f} | "
                    f"TP: ${t.tp_price:.6f} | SL: ${t.sl_price:.6f}"
                )
            lines.append("")

        recent_closed = [t for t in finished[-5:] if t.status in ("tp_hit", "sl_hit")]
        if recent_closed:
            lines.append(f"📋 <b>TRADE TERAKHIR</b>")
            for t in recent_closed:
                icon = "✅" if t.status == "tp_hit" else "❌"
                lines.append(f"{icon} ${t.token_symbol} | {t.pnl_sol:+.4f} SOL ({t.pnl_pct:+.1f}%)")
            lines.append("")

        lines.append(f"🔍 Scan: {state.scanned_tokens_today} token | Skip: {state.skipped_tokens_today}")
        lines.append(f"⚙️ Filter Gen #{state.filters.generation} | Improve: {state.improve_count}x")
        lines.append(f"🤖 Model: {OPENROUTER_MODEL}")

        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# MAIN TRADING ENGINE
# ──────────────────────────────────────────────────────────────────

class TradingEngine:
    def __init__(self):
        self.scanner = SolanaScanner()
        self.ai      = AIAgent()
        self.filter  = FilterEngine()
        self.tg      = TelegramBot()
        self.state   = BotState()
        self.seen_tokens: set = set()
        self.trade_counter = 0

    def _new_trade_id(self) -> str:
        self.trade_counter += 1
        return f"T{self.trade_counter:04d}"

    async def run(self):
        logging.info("🚀 Bot started!")
        async with aiohttp.ClientSession() as session:
            await self.tg.send(session, self._startup_message())
            await asyncio.gather(
                self._scan_loop(session),
                self._price_update_loop(session),
                self._report_loop(session),
            )

    def _startup_message(self) -> str:
        return f"""
🤖 <b>NDA4 AI TRADING BOT STARTED</b>

💼 Balance: {INITIAL_BALANCE_SOL} SOL
📦 Trade Size: {TRADE_SIZE_SOL} SOL per trade
🎯 Target WR: 55%
⚡ Max Open Trades: {MAX_OPEN_TRADES}
🔍 Scan Interval: {SCAN_INTERVAL_SEC}s
📊 Report Interval: 1 jam
🔧 Filter Gen: #1
🤖 AI Model: {OPENROUTER_MODEL}

Bot sedang aktif scan token Solana secara real-time...
Notifikasi akan dikirim untuk setiap trade & report per jam. 📡
""".strip()

    async def _scan_loop(self, session: aiohttp.ClientSession):
        while True:
            try:
                await self._do_scan(session)
            except Exception as e:
                logging.error(f"Scan loop error: {e}")
            await asyncio.sleep(SCAN_INTERVAL_SEC)

    async def _do_scan(self, session: aiohttp.ClientSession):
        if not self.state.can_open_trade():
            logging.info(f"Skip scan: balance={self.state.balance_sol:.3f} SOL, open={len(self.state.open_trades)}")
            return

        trending   = await self.scanner.get_trending_tokens(session)
        new_tokens = await self.scanner.get_new_tokens(session)
        all_tokens = {t.address: t for t in (trending + new_tokens)}.values()
        self.state.scanned_tokens_today += len(list(all_tokens))

        candidates = []
        for token in all_tokens:
            if not token.address:
                continue
            open_addresses = {t.token_address for t in self.state.open_trades}
            if token.address in open_addresses:
                continue
            passed, reason = self.filter.passes(token, self.state.filters)
            if not passed:
                self.state.skipped_tokens_today += 1
                logging.debug(f"SKIP {token.symbol}: {reason}")
                continue
            candidates.append(token)

        if not candidates:
            logging.info("No candidates passed filters this scan")
            return

        candidates.sort(key=lambda t: t.price_change_5m, reverse=True)

        for token in candidates[:3]:
            if not self.state.can_open_trade():
                break
            # ✅ Pass session to async AI call
            decision = await self.ai.analyze_token(session, token, self.state)
            if not decision:
                continue
            if decision.get("action") == "BUY" and decision.get("confidence", 0) >= 60:
                await self._open_trade(session, token, decision)
                await asyncio.sleep(2)

    async def _open_trade(self, session: aiohttp.ClientSession, token: TokenData, decision: dict):
        entry_type  = decision.get("entry_type", "market")
        entry_price = float(decision.get("entry_price_usd", token.price_usd))
        tp_price    = float(decision.get("tp_price_usd", entry_price * self.state.filters.tp_multiplier))
        sl_price    = float(decision.get("sl_price_usd", entry_price * self.state.filters.sl_multiplier))
        reasoning   = decision.get("reasoning", "")

        sol_usd = token.price_usd / token.price_sol if token.price_sol > 0 else 150

        trade = Trade(
            id=self._new_trade_id(),
            token_address=token.address,
            token_symbol=token.symbol,
            entry_type=entry_type,
            entry_price=entry_price,
            entry_price_sol=entry_price / sol_usd,
            tp_price=tp_price,
            sl_price=sl_price,
            size_sol=TRADE_SIZE_SOL,
            status="pending_limit" if entry_type == "limit" else "open",
            opened_at=time.time(),
            current_price=token.price_usd,
            ai_reasoning=reasoning,
        )

        self.state.trades.append(trade)
        self.state.balance_sol -= TRADE_SIZE_SOL
        self.state.total_trades += 1

        logging.info(f"OPEN TRADE: {trade.id} ${token.symbol} {entry_type} @ ${entry_price:.8f}")
        await self.tg.send(session, self.tg.format_trade_open(trade))

    async def _price_update_loop(self, session: aiohttp.ClientSession):
        while True:
            try:
                await self._update_prices(session)
            except Exception as e:
                logging.error(f"Price update error: {e}")
            await asyncio.sleep(15)

    async def _update_prices(self, session: aiohttp.ClientSession):
        for trade in list(self.state.open_trades):
            token_data = await self.scanner.get_token_detail(session, trade.token_address)
            if not token_data:
                continue

            current_price = token_data.price_usd
            trade.current_price = current_price
            trade.last_updated  = time.time()

            if trade.status == "pending_limit":
                if current_price <= trade.entry_price:
                    trade.status = "open"
                    logging.info(f"Limit filled: {trade.id} @ ${current_price:.8f}")
                    await self.tg.send(session,
                        f"🟢 <b>LIMIT ORDER FILLED</b>\n"
                        f"${trade.token_symbol} | Entry: ${trade.entry_price:.8f}\n"
                        f"TP: ${trade.tp_price:.8f} | SL: ${trade.sl_price:.8f}"
                    )
                continue

            if current_price >= trade.tp_price:
                await self._close_trade(session, trade, current_price, "tp_hit")
            elif current_price <= trade.sl_price:
                await self._close_trade(session, trade, current_price, "sl_hit")

    async def _close_trade(self, session: aiohttp.ClientSession, trade: Trade, exit_price: float, reason: str):
        trade.exit_price = exit_price
        trade.closed_at  = time.time()
        trade.status     = reason

        pct = (exit_price - trade.entry_price) / trade.entry_price
        trade.pnl_pct = pct * 100
        trade.pnl_sol = pct * trade.size_sol
        self.state.total_pnl_sol += trade.pnl_sol
        self.state.balance_sol   += TRADE_SIZE_SOL + trade.pnl_sol

        won = reason == "tp_hit"
        if won:
            self.state.winning_trades    += 1
            self.state.consecutive_losses = 0
        else:
            self.state.losing_trades      += 1
            self.state.consecutive_losses += 1
            self.state.recent_losses.append(trade)

        logging.info(
            f"CLOSE {trade.id} ${trade.token_symbol} {reason} "
            f"@ ${exit_price:.8f} | PnL: {trade.pnl_sol:+.4f} SOL ({trade.pnl_pct:+.1f}%)"
        )

        await self.tg.send(session, self.tg.format_trade_close(trade))

        if self.state.consecutive_losses >= CONSECUTIVE_LOSS_TRIGGER:
            await self._improve_filters(session)

    async def _improve_filters(self, session: aiohttp.ClientSession):
        old_gen = self.state.filters.generation
        logging.info(f"🔧 Improving filters (gen {old_gen})...")

        # ✅ Pass session to async AI call
        new_filters, notes = await self.ai.improve_filters(session, self.state)
        self.state.filters            = new_filters
        self.state.consecutive_losses = 0
        self.state.improve_count     += 1

        logging.info(f"✅ Filter improved: gen {old_gen} → {new_filters.generation}")
        await self.tg.send(session, self.tg.format_filter_improve(old_gen, new_filters.generation, notes))

    async def _report_loop(self, session: aiohttp.ClientSession):
        await asyncio.sleep(REPORT_INTERVAL_SEC)
        while True:
            try:
                report = self.tg.format_hourly_report(self.state)
                await self.tg.send(session, report)
                self.state.last_report_at = time.time()
                logging.info("📊 Hourly report sent")
            except Exception as e:
                logging.error(f"Report error: {e}")
            await asyncio.sleep(REPORT_INTERVAL_SEC)


# ──────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("bot.log", encoding="utf-8")
        ]
    )

    missing = []
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":   missing.append("TELEGRAM_BOT_TOKEN")
    if TELEGRAM_CHAT_ID   == "YOUR_CHAT_ID":     missing.append("TELEGRAM_CHAT_ID")
    if OPENROUTER_API_KEY == "YOUR_OPENROUTER_KEY": missing.append("OPENROUTER_API_KEY")

    if missing:
        print(f"\n❌ Set environment variables dulu: {', '.join(missing)}")
        print("   Bisa pakai .env file atau export langsung\n")
        return

    engine = TradingEngine()
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
