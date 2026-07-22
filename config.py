"""Central configuration: asset registry, rating weights, strategy parameters.

The asset registry is the single source of truth for how each symbol maps onto
each upstream provider. Provider selection is driven entirely off this table --
no module should ever branch on a symbol name directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "bot.db"
STATIC_DIR = BASE_DIR / "static"

# --- Polling cadence -------------------------------------------------------

CYCLE_SECONDS = 60

# Candles and market-cap data move far slower than price, so they refresh on a
# multiple of the base cycle. The multiples are chosen so the *wall-clock* rate
# stays the same as it was at a 120s cycle -- lowering them risks 429s on the
# free tiers.
KLINE_EVERY_N_CYCLES = 10     # hourly candles ~every 10 min
MARKET_EVERY_N_CYCLES = 8     # CoinGecko ~every 8 min
DAILY_EVERY_N_CYCLES = 60     # daily candles ~every hour

CANDLE_INTERVAL = "1h"
DAILY_INTERVAL = "1d"
CANDLE_LIMIT = 300            # enough history for EMA200 + 30d stats
DAILY_LIMIT = 365             # a year of daily closes for long-range charts

# --- Assets ----------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    symbol: str
    name: str
    thesis: str
    coingecko_id: str
    binance_symbol: str | None = None
    hyperliquid_coin: str | None = None

    @property
    def price_source(self) -> str:
        """Primary provider for live price and candles."""
        return "binance" if self.binance_symbol else "hyperliquid"


# HYPE is deliberately the odd one out: it is not listed on Binance spot
# (HYPEUSDT returns -1121 Invalid symbol), so it sources from Hyperliquid's
# own public API instead. Every other asset uses Binance as primary with
# CoinGecko as the fallback.
ASSETS: list[Asset] = [
    Asset("BTC", "Bitcoin", "The premier store of value.",
          "bitcoin", binance_symbol="BTCUSDT"),
    Asset("ETH", "Ethereum", "The leading foundation for smart contracts and DeFi.",
          "ethereum", binance_symbol="ETHUSDT"),
    Asset("SOL", "Solana", "Low fees, high speeds, and massive dApp revenue.",
          "solana", binance_symbol="SOLUSDT"),
    Asset("BNB", "BNB", "Powers the Binance ecosystem with ongoing coin burns.",
          "binancecoin", binance_symbol="BNBUSDT"),
    Asset("XRP", "XRP", "High-efficiency institutional cross-border payments.",
          "ripple", binance_symbol="XRPUSDT"),
    Asset("LINK", "Chainlink", "The oracle layer bridging chains and real-world data.",
          "chainlink", binance_symbol="LINKUSDT"),
    Asset("SUI", "Sui", "Rising Layer-1 with explosive user and dev growth.",
          "sui", binance_symbol="SUIUSDT"),
    Asset("AVAX", "Avalanche", "Scalable platform favored for subnets and institutional DeFi.",
          "avalanche-2", binance_symbol="AVAXUSDT"),
    Asset("TRX", "TRON", "Major network for stablecoin transfers and content hosting.",
          "tron", binance_symbol="TRXUSDT"),
    Asset("ADA", "Cardano", "Peer-reviewed chain focused on security and sustainability.",
          "cardano", binance_symbol="ADAUSDT"),
    Asset("ARB", "Arbitrum", "Leading Layer-2 making Ethereum faster and cheaper.",
          "arbitrum", binance_symbol="ARBUSDT"),
    Asset("ONDO", "Ondo Finance", "Major player in Real-World Asset tokenization.",
          "ondo-finance", binance_symbol="ONDOUSDT"),
    Asset("TAO", "Bittensor", "Decentralized network incentivizing machine learning.",
          "bittensor", binance_symbol="TAOUSDT"),
    Asset("HYPE", "Hyperliquid", "Dominates decentralized perpetuals and trading infra.",
          "hyperliquid", hyperliquid_coin="HYPE"),
    Asset("DOGE", "Dogecoin", "The most popular and resilient meme coin.",
          "dogecoin", binance_symbol="DOGEUSDT"),
]

BY_SYMBOL: dict[str, Asset] = {a.symbol: a for a in ASSETS}
SYMBOLS: list[str] = [a.symbol for a in ASSETS]
BENCHMARK = "BTC"

# --- Rating ----------------------------------------------------------------

# Weights must sum to 1.0. The dashboard can override these per-request; these
# are the defaults used by the engine when it persists ratings.
DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum": 0.30,
    "risk": 0.25,
    "structure": 0.25,
    "relative": 0.20,
}

# Letter grade cutoffs, checked high to low.
GRADE_BANDS: list[tuple[float, str]] = [
    (90, "A+"), (85, "A"), (80, "A-"),
    (75, "B+"), (70, "B"), (65, "B-"),
    (60, "C+"), (55, "C"), (50, "C-"),
    (45, "D+"), (40, "D"), (35, "D-"),
    (0, "F"),
]

# --- Signals ---------------------------------------------------------------

# The gap between BUY_THRESHOLD and EXIT_THRESHOLD is a hysteresis dead band.
# Ratings recompute every cycle; without this gap a composite hovering near a
# single threshold would flip its signal almost every minute.
BUY_THRESHOLD = 70.0
STRONG_BUY_THRESHOLD = 82.0
EXIT_THRESHOLD = 45.0
STRONG_SELL_THRESHOLD = 32.0

# --- Paper trading ---------------------------------------------------------

STARTING_CAPITAL = 100_000.0   # default; the user picks theirs on the start screen
CAPITAL_MIN = 100.0
CAPITAL_MAX = 10_000_000.0
FEE_RATE = 0.001               # 0.1% per side

# --- Languages -------------------------------------------------------------

# Russian was removed at the user's request and replaced with Ukrainian;
# Greek was added alongside. The dashboard's I18N dictionaries must cover
# exactly this set, in full key parity.
SUPPORTED_LANGUAGES = ("en", "hy", "uk", "es", "el")

# --- Retention -------------------------------------------------------------

RETENTION_DAYS = 90

# Retention for the *durable* store. db.RETENTION_DAYS above covers market data
# on the ephemeral disk, which is regenerable and costs nothing to lose; these
# cover Postgres, which on the free plan has a hard 1 GB ceiling and holds the
# only copy of every account.
#
# user_equity is the fastest-growing table anywhere in the project: one row per
# portfolio per cycle, so 1,440 rows per portfolio per day. Nothing trimmed it
# before, which meant the one table that cannot be regenerated was also the one
# with no bound on its size.
EQUITY_RETENTION_DAYS = 90

# Every cookie-less request mints a guest row so a first-time visitor can trade
# before signing up -- including requests from crawlers, uptime probes and
# anyone who opened the page once. Guests that never traded are abandoned page
# loads, and each one used to cost an equity row every cycle forever.
GUEST_TTL_DAYS = 7

# Window for counting a guest as currently active, in hours. Comfortably wider
# than userstore.TOUCH_INTERVAL_MS (1h): last_seen_ts is throttled to one write
# per hour, so a continuously-active visitor's stamp can trail reality by that
# much, and a window any tighter would flicker them in and out of the count.
GUEST_ACTIVE_HOURS = 24

# How often the engine runs the maintenance pass, in cycles. Hourly at the
# 60-second cadence: these are bulk deletes over indexed columns, and running
# them per cycle would spend more time pruning than the rows cost to keep.
MAINTENANCE_EVERY = 60
