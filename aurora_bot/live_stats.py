"""Realistic live purchase feed and combined real + simulated buyer counters."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib

from .db import Database
from .service import Shop, parse_date, tariff_tier_kind

_MSK = timezone(timedelta(hours=3))
_ANCHOR_DATE = date(2026, 1, 1)


def _now_msk() -> datetime:
    return datetime.now(_MSK)

# Cache completed full-day simulated counts and cumulative past sums per (day_date, tariff_key, weight).
# Bounded to avoid unbounded growth over years of uptime.
_FULL_DAY_COUNT_CACHE: dict[tuple[date, str, int], int] = {}
_CUMULATIVE_PAST_CACHE: dict[tuple[date, str, int], int] = {}
_MAX_CACHE_ENTRIES = 20_000

_FALLBACK_TARIFFS = (
    {'id': 1, 'title': '⚡ Базовый · Фуллы из TikTok', 'active': 1},
    {'id': 2, 'title': '🎀 Альтушки и стримерши [30д]', 'active': 1},
    {'id': 3, 'title': '🍑 OnlyFans & Fansly [30д]', 'active': 1},
    {'id': 4, 'title': '😈 Скрытый архив: Лички [30д]', 'active': 1},
    {'id': 5, 'title': '⭐️ Ultra VIP: 5 каналов [ХИТ]', 'active': 1},
    {'id': 6, 'title': '👑 Diamond Club: Все базы', 'active': 1},
    {'id': 7, 'title': '🖤 Black VIP: Все архивы [4K]', 'active': 1},
)


def _tariff_profile(tariff: dict) -> tuple[str, int, int]:
    """Return (stable_key, daily_weight, pre_anchor_base) for a tariff."""
    tid = int(tariff.get('id') or 0)
    t_up = (tariff.get('title') or '').upper()
    tier_kind = tariff_tier_kind(tariff.get('title', ''), tariff.get('tier', ''))
    if tier_kind == 'diamond':
        kind, weight, base = 'diamond', 5, 180
    elif tier_kind == 'ultra' or '[ТОП]' in t_up or '[ХИТ]' in t_up:
        kind, weight, base = 'ultra', 45, 1520
    elif 'BLACK' in t_up:
        kind, weight, base = 'black', 2, 65
    elif any(k in t_up for k in ('БАЗОВ', '24 ЧАСА', '24Ч', 'ПРОПУСК', 'ТЕСТ-ДРАЙВ', 'ПРОБН')):
        kind, weight, base = 'base', 28, 920
    elif 'ONLYFANS' in t_up:
        kind, weight, base = 'onlyfans', 20, 680
    elif 'АЛЬТУШК' in t_up or 'TIKTOK' in t_up:
        kind, weight, base = 'altushki', 16, 520
    elif 'СКРЫТЫЙ АРХИВ' in t_up or 'TABOO' in t_up or 'VIP-АРХИВ' in t_up:
        kind, weight, base = 'taboo', 6, 210
    else:
        custom_hash = int(hashlib.md5(f'custom_{tid}'.encode()).hexdigest()[:6], 16)
        kind = 'custom'
        weight = 3 + (custom_hash % 2)
        base = 120 + ((custom_hash >> 4) % 90)
    return f'{kind}:{tid}', weight, base


def _simulate_tariff_day(day_start: datetime, up_to_ts: int,
                         tariff_key: str, weight: int) -> list[tuple[int, int]]:
    """Generate deterministic (timestamp, users_step) purchases for one tariff in a day."""
    day_ts = int(day_start.timestamp())
    if up_to_ts < day_ts:
        return []
    safe_weight = max(1, weight)
    gap_00 = round(820 * 100 / safe_weight)
    first_seed = int(hashlib.md5(f'{day_start.date()}_{tariff_key}_init'.encode()).hexdigest()[:8], 16)
    current_ts = day_ts + 180 + (first_seed % max(300, gap_00))
    events: list[tuple[int, int]] = []
    step = 0
    while current_ts <= up_to_ts:
        # MSK is fixed UTC+3 (+10800s) with no DST; integer arithmetic avoids per-step datetime allocation.
        hour = ((current_ts + 10800) % 86400) // 3600
        if 3 <= hour < 8:
            shop_gap = 2200
        elif 8 <= hour < 13:
            shop_gap = 1500
        elif 13 <= hour < 18:
            shop_gap = 1050
        elif 18 <= hour < 24:
            shop_gap = 680
        else:
            shop_gap = 820
        base_gap = round(shop_gap * 100 / safe_weight)
        half_jitter = max(60, base_gap // 3)
        seed = int(hashlib.md5(f'{day_start.date()}_{tariff_key}_{step}'.encode()).hexdigest()[:8], 16)
        jitter = (seed % (2 * half_jitter + 1)) - half_jitter
        gap = max(300, base_gap + jitter)
        users_step = 3 + ((seed >> 4) % 4)
        events.append((current_ts, users_step))
        current_ts += gap
        step += 1
    return events


def _full_day_simulated_count(day_date: date, tariff_key: str, weight: int) -> int:
    cache_key = (day_date, tariff_key, weight)
    cached = _FULL_DAY_COUNT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    day_start = datetime(day_date.year, day_date.month, day_date.day, tzinfo=_MSK)
    day_end_ts = int((day_start + timedelta(days=1)).timestamp()) - 1
    count = len(_simulate_tariff_day(day_start, day_end_ts, tariff_key, weight))
    if len(_FULL_DAY_COUNT_CACHE) >= _MAX_CACHE_ENTRIES:
        _FULL_DAY_COUNT_CACHE.clear()
    _FULL_DAY_COUNT_CACHE[cache_key] = count
    return count


def _past_simulated_total(day_date: date, tariff_key: str, weight: int, base: int) -> int:
    """Cumulative baseline up to yesterday (exclusive of day_date), monotonic across midnight."""
    if day_date >= _ANCHOR_DATE:
        start_date, start_base = _ANCHOR_DATE, base
    else:
        # Guard against negative offsets if tests run with dates preceding _ANCHOR_DATE.
        start_date, start_base = date(day_date.year, 1, 1), max(50, base // 2)

    if day_date <= start_date:
        return start_base

    cache_key = (day_date, tariff_key, weight)
    cached = _CUMULATIVE_PAST_CACHE.get(cache_key)
    if cached is not None:
        return start_base + cached

    prev_day = day_date - timedelta(days=1)
    prev_cached = _CUMULATIVE_PAST_CACHE.get((prev_day, tariff_key, weight))
    if prev_cached is not None and prev_day >= start_date:
        sim_sum = prev_cached + _full_day_simulated_count(prev_day, tariff_key, weight)
        if len(_CUMULATIVE_PAST_CACHE) >= _MAX_CACHE_ENTRIES:
            _CUMULATIVE_PAST_CACHE.clear()
        _CUMULATIVE_PAST_CACHE[cache_key] = sim_sum
        return start_base + sim_sum

    sim_sum = 0
    cursor = start_date
    while cursor < day_date:
        sim_sum += _full_day_simulated_count(cursor, tariff_key, weight)
        cursor += timedelta(days=1)
        if len(_CUMULATIVE_PAST_CACHE) >= _MAX_CACHE_ENTRIES:
            _CUMULATIVE_PAST_CACHE.clear()
        _CUMULATIVE_PAST_CACHE[(cursor, tariff_key, weight)] = sim_sum
    return start_base + sim_sum


async def get_live_tariff_buyers(shop: Shop, db: Database, tariff: dict, *,
                                 current: datetime | None = None) -> int:
    """Return realistic buyer count mixing accumulated history, today's simulation, and real buyers."""
    current = (current or _now_msk()).astimezone(_MSK)
    now_ts = int(current.timestamp())
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)

    tariff_key, weight, base = _tariff_profile(tariff)
    past_total = _past_simulated_total(day_start.date(), tariff_key, weight, base)
    today_sim = len(_simulate_tariff_day(day_start, now_ts, tariff_key, weight)) if tariff.get('active', 1) else 0
    real_buyers = await shop.tariff_buyers_count(int(tariff['id']))
    return past_total + today_sim + real_buyers


def _collect_day_sales(day_start: datetime, up_to_ts: int,
                       tariffs: list[dict]) -> list[tuple[str, int, int, str]]:
    """Collect simulated events across tariffs sorted by timestamp ascending."""
    events: list[tuple[str, int, int, str]] = []
    for tariff in tariffs:
        title = tariff.get('title') or ''
        if not title:
            continue
        tariff_key, weight, _ = _tariff_profile(tariff)
        for ts, users_step in _simulate_tariff_day(day_start, up_to_ts, tariff_key, weight):
            events.append((title, ts, users_step, tariff_key))
    events.sort(key=lambda item: item[1])
    return events


async def get_live_recent_purchases(shop: Shop, db: Database, *,
                                    current: datetime | None = None) -> dict:
    """Return live stats feed synchronized with real DB orders and tariff buyer counters."""
    current = (current or _now_msk()).astimezone(_MSK)
    now_ts = int(current.timestamp())
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    day_ts = int(day_start.timestamp())

    tariff_rows = await db.read("SELECT id, title, active, tier FROM tariffs WHERE active=1 ORDER BY id", many=True) or []
    tariffs = [dict(row) for row in tariff_rows if row.get('title')]
    if not tariffs:
        tariffs = [dict(t) for t in _FALLBACK_TARIFFS]

    today_sim = _collect_day_sales(day_start, now_ts, tariffs)
    tail_sim: list[tuple[str, int, int, str]] = []
    if len(today_sim) < 5:
        yesterday_start = day_start - timedelta(days=1)
        tail_sim = _collect_day_sales(yesterday_start, day_ts - 1, tariffs)[-5:]
    sim_sales = [(title, ts) for title, ts, _, _ in tail_sim + today_sim]

    real_orders = await db.read("""SELECT title, paid_at FROM orders
        WHERE status='paid' AND provider!='admin' AND order_kind IN ('tariff', 'upgrade') AND paid_at IS NOT NULL
        ORDER BY paid_at DESC, rowid DESC LIMIT 10""", many=True) or []

    real_sales: list[tuple[str, int]] = []
    for ro in real_orders:
        try:
            r_dt = parse_date(ro['paid_at'])
            if r_dt is not None:
                if r_dt.tzinfo is None:
                    r_dt = r_dt.replace(tzinfo=timezone.utc)
                real_sales.append((ro['title'], int(r_dt.timestamp())))
        except Exception:
            pass

    all_sales = sorted(real_sales + sim_sales, key=lambda x: x[1], reverse=True)

    recent_items: list[tuple[str, str]] = []
    for title, ts in all_sales[:5]:
        diff = max(0, now_ts - ts)
        if diff < 60:
            ago = 'только что 🔥'
        elif diff < 3600:
            ago = f'{diff // 60} мин. назад'
        else:
            ago = f'{diff // 3600} ч. {(diff % 3600) // 60} мин. назад'
        recent_items.append((title, ago))

    day_start_utc = day_start.astimezone(timezone.utc).isoformat(timespec='seconds')
    real_today_rows = await db.read("""SELECT title, COUNT(*) AS c FROM orders
        WHERE status='paid' AND provider!='admin' AND order_kind IN ('tariff', 'upgrade') AND paid_at>=?
        GROUP BY title""", (day_start_utc,), many=True) or []
    real_today_count = sum(row['c'] for row in real_today_rows)
    real_users_row = await db.read("SELECT COUNT(*) AS c FROM users WHERE created_at>=?", (day_start_utc,))
    real_users_today = real_users_row['c'] if real_users_row else 0

    today_counts: dict[str, int] = {}
    for title, _, _, _ in today_sim:
        today_counts[title] = today_counts.get(title, 0) + 1
    for row in real_today_rows:
        t = row['title']
        if t:
            today_counts[t] = today_counts.get(t, 0) + row['c']

    # Pick combined top category today (falling back to highest-weight active tariff early at 00:xx).
    if today_counts:
        top_title = max(today_counts.items(), key=lambda item: item[1])[0]
    else:
        top_title = max(tariffs, key=lambda t: _tariff_profile(t)[1])['title']

    total_sales_today = len(today_sim) + real_today_count
    day_seed = int(hashlib.md5(f'{day_start.date()}_u0'.encode()).hexdigest()[:4], 16)
    base_users = 6 + (day_seed % 5) + sum(users_step for _, _, users_step, _ in today_sim)
    last_ts = today_sim[-1][1] if today_sim else day_ts
    trickle = min(2, max(0, now_ts - last_ts) // 240)
    users_today = base_users + trickle + real_users_today + real_today_count

    return {
        'recent_purchases': recent_items,
        'total_sales_today': total_sales_today,
        'users_today': users_today,
        'popular_category': top_title,
    }
