"""Domain service: checkout snapshots, payment state and customer entitlements."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import re
import sqlite3
from time import monotonic
import uuid
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from .analytics import latest_greeting_touch
from .db import Database, in_seconds, now


MAX_DURATION_DAYS = 3650
MAX_CHANNELS = 25
EXTERNAL_INVOICE_TTL_SECONDS = 35 * 60


class ShopError(ValueError):
    pass


def channels_of(value: str) -> list[int]:
    result: list[int] = []
    for part in value.replace('\n', ',').split(','):
        if not part.strip():
            continue
        chat_id = int(part.strip())
        if not -(2 ** 52) < chat_id < 0 or chat_id in result:
            raise ShopError('Нужны уникальные отрицательные chat_id закрытых каналов.')
        result.append(chat_id)
    if len(result) > MAX_CHANNELS:
        raise ShopError(f'В одном тарифе допускается до {MAX_CHANNELS} каналов.')
    return result


def telegram_links(value: str) -> list[str]:
    urls = value.split()
    if not 1 <= len(urls) <= MAX_CHANNELS:
        raise ShopError(f'Укажите от 1 до {MAX_CHANNELS} ссылок, по одной на строку.')
    result = []
    for url in urls:
        parsed = urlsplit(url)
        if (len(url) > 160 or parsed.scheme != 'https'
            or parsed.netloc.lower() not in ('t.me', 'telegram.me')
            or parsed.query or parsed.fragment or not re.fullmatch(
                r'/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+|[A-Za-z][A-Za-z0-9_]{3,31})/?', parsed.path
            )):
            raise ShopError('Нужна ссылка Telegram на канал: https://t.me/+… или https://t.me/имя_канала.')
        canonical = 'https://t.me' + parsed.path.rstrip('/')
        if canonical in result:
            raise ShopError('Уберите повторяющиеся ссылки.')
        result.append(canonical)
    return result


def tariff_delivery(tariff: dict) -> tuple[list[int], list[str]]:
    channels = channels_of(tariff['channel_ids'])
    urls = json.loads(tariff['delivery_urls'])
    if (channels and urls) or not (channels or urls or tariff.get('allow_no_channel')):
        raise ShopError('Укажите ссылки, ID каналов или выберите продажу без канала.')
    if urls:
        if tariff['duration_days']:
            raise ShopError('Готовые ссылки доступны только для бессрочных товаров.')
        urls = telegram_links('\n'.join(urls))
    return channels, urls


def matches_delivery(order, tariff, channels: list[int], urls: list[str]) -> bool:
    return (order['duration_days'] == tariff['duration_days']
            and json.loads(order['channels']) == channels
            and json.loads(order['delivery_urls']) == urls)


def bind_order_channels(con: sqlite3.Connection, order) -> list[int]:
    """Convert previously sold URLs with a known channel into managed access."""
    channels = json.loads(order['channels'])
    if order['delivery_urls'] == '[]':
        return channels
    matched = {row['url']: row['chat_id'] for row in con.execute(
        'SELECT url,chat_id FROM product_channel_links WHERE url IN (SELECT value FROM json_each(?))',
        (order['delivery_urls'],),
    )}
    if matched:
        urls = json.loads(order['delivery_urls'])
        channels = list(dict.fromkeys([*channels, *(matched[url] for url in urls if url in matched)]))
        remaining = [url for url in urls if url not in matched]
        con.execute('UPDATE orders SET channels=?,delivery_urls=? WHERE id=?',
                    (json.dumps(channels), json.dumps(remaining), order['id']))
    return channels


def until(days: int) -> str | None:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec='seconds') if days else None


def parse_date(value: str | None):
    return datetime.fromisoformat(value) if value else None


UPGRADE_FLOOR_RUB = 300


def scale_stars(rub: int, stars: int, final_rub: int) -> int:
    if rub <= 0 or final_rub >= rub:
        return stars
    scaled = (Decimal(stars) * Decimal(final_rub) / Decimal(rub)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return max(1, int(scaled))


def promo_rub_discount(rub: int, discount: int = 0, discount_percent: int = 0) -> int:
    """Return the effective RUB discount for fixed RUB or percentage promos."""
    pct = max(0, min(99, int(discount_percent or 0)))
    if pct > 0:
        return int((Decimal(max(0, rub)) * Decimal(pct) / Decimal(100)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return max(0, int(discount or 0))


def promo_discount_label(promo: dict | None) -> str:
    """Format promo discount as percentage ('20%') or fixed RUB ('150 ₽')."""
    if not promo:
        return '0 ₽'
    pct = max(0, min(99, int(promo.get('discount_percent') or 0)))
    if pct > 0:
        return f'{pct}%'
    return f"{max(0, int(promo.get('discount') or 0))} ₽"


def promo_has_discount(promo) -> bool:
    """Return True if promo provides a positive fixed RUB or percentage discount."""
    if not promo:
        return False
    keys = promo.keys() if hasattr(promo, 'keys') else promo
    pct = promo['discount_percent'] if 'discount_percent' in keys else 0
    return int(promo['discount'] or 0) > 0 or int(pct or 0) > 0


def price_with_promo(rub: int, stars: int, discount: int = 0, discount_percent: int = 0) -> tuple[int, int]:
    off_rub = promo_rub_discount(rub, discount, discount_percent)
    final_rub = max(1, rub - off_rub)
    if discount_percent > 0:
        off_stars = promo_rub_discount(stars, discount_percent=discount_percent)
        return final_rub, max(1, stars - off_stars)
    return final_rub, scale_stars(rub, stars, final_rub)


def _promo_slot_available(con: sqlite3.Connection, order, timestamp: str, *, switching_to_stars: bool = False) -> bool:
    code, user_id = order['promo_code'], order['user_id']
    promo = con.execute('SELECT * FROM promos WHERE code=? AND enabled=1', (code,)).fetchone()
    if (not promo or not promo_has_discount(promo)
            or (promo['owner_user_id'] is not None and promo['owner_user_id'] != user_id)
            or (promo['expires_at'] and promo['expires_at'] <= timestamp)
            or con.execute('SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?', (code, user_id)).fetchone()):
        return False

    include_personal_drafts = switching_to_stars and bool(promo['is_personal'])
    holds = con.execute("""SELECT DISTINCT user_id FROM orders WHERE promo_code=? AND id!=?
        AND ((provider='stars' AND status IN ('pending','expired') AND checkout_id!='')
            OR (provider='manual' AND status='review')
            OR (provider='manual' AND status='pending' AND expires_at>?)
            OR (provider IN ('cryptopay','2328') AND expires_at>? AND status IN ('pending','expired')
                AND ((? AND status='pending') OR
                    external_id!='' OR payment_url!='' OR invoice_sending_at IS NOT NULL)))""",
        (code, order['id'], timestamp, timestamp, include_personal_drafts)).fetchall()
    return all(hold['user_id'] != user_id for hold in holds) and promo['used'] + len(holds) < promo['max_uses']


def _is_paying_user_con(con: sqlite3.Connection, user_id: int) -> bool:
    return bool(con.execute(
        """SELECT 1 FROM orders
           WHERE user_id=? AND status='paid' AND provider!='admin' AND refund_requested_at IS NULL
           LIMIT 1""",
        (user_id,),
    ).fetchone())


def _user_ltv_con(con: sqlite3.Connection, user_id: int) -> int:
    row = con.execute(
        """SELECT COALESCE(SUM(amount_rub), 0) FROM orders
           WHERE user_id=? AND status='paid' AND provider!='admin' AND refund_requested_at IS NULL""",
        (user_id,),
    ).fetchone()
    return int(row[0] or 0)


def _upgrade_credit_con(con: sqlite3.Connection, user_id: int) -> int:
    row = con.execute(
        """SELECT COALESCE(SUM(o.amount_rub), 0) FROM orders o
           WHERE o.user_id=? AND o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL
             AND o.order_kind IN ('tariff', 'upgrade')
             AND NOT EXISTS (SELECT 1 FROM purchases p WHERE p.order_id=o.id AND p.revoked_by_admin=1)""",
        (user_id,),
    ).fetchone()
    return int(row[0] or 0)


def tariff_tier_kind(title: str = '', tier: str = '') -> str:
    """Resolve normalized flagship tier ('diamond', 'ultra', or '') from explicit flag or title fallback."""
    explicit = (tier or '').strip().lower()
    if explicit == 'regular':
        return ''
    if explicit in ('diamond', 'ultra'):
        return explicit
    title_up = (title or '').upper()
    if 'DIAMOND' in title_up:
        return 'diamond'
    if 'ULTRA' in title_up:
        return 'ultra'
    return ''


def _upgrade_offers_con(con: sqlite3.Connection, user_id: int) -> list[dict]:
    credit = _upgrade_credit_con(con, user_id)
    if credit <= 0:
        return []
    unlimited_rows = con.execute(
        """SELECT p.title, p.revoked_by_admin,
                  COALESCE(t.tier, '') AS tier,
                  COALESCE(t.rub, o.amount_rub, 0) AS tier_rub
           FROM purchases p
           JOIN orders o ON o.id=p.order_id
           LEFT JOIN tariffs t ON t.id=p.tariff_id
           WHERE p.user_id=? AND o.status='paid' AND o.refund_requested_at IS NULL
             AND p.expires_at IS NULL""",
        (user_id,),
    ).fetchall()
    owned_tiers = {tariff_tier_kind(row['title'], row['tier']) for row in unlimited_rows}
    if 'diamond' in owned_tiers:
        return []
    owns_ultra = 'ultra' in owned_tiers
    max_owned_unlimited_rub = max(
        (row['tier_rub'] for row in unlimited_rows if not row['revoked_by_admin']),
        default=0,
    )

    candidates = con.execute(
        """SELECT * FROM tariffs
           WHERE active=1 AND duration_days=0 AND (channel_ids!='' OR delivery_urls!='[]')
           ORDER BY rub ASC, stars ASC, id ASC"""
    ).fetchall()
    offers: list[dict] = []
    for row in candidates:
        if max_owned_unlimited_rub >= row['rub']:
            continue
        tier_kind = tariff_tier_kind(row['title'], row['tier'] if 'tier' in row.keys() else '')
        if tier_kind == 'diamond':
            kind = 'DIAMOND'
        elif tier_kind == 'ultra':
            if owns_ultra:
                continue
            kind = 'ULTRA'
        else:
            continue
        upgraded_rub = max(UPGRADE_FLOOR_RUB, min(row['rub'], row['rub'] - credit))
        if upgraded_rub >= row['rub']:
            continue
        upgraded_stars = scale_stars(row['rub'], row['stars'], upgraded_rub)
        offers.append({
            'tariff': dict(row),
            'kind': kind,
            'rub': upgraded_rub,
            'stars': upgraded_stars,
            'credit_rub': credit,
            'saved_rub': row['rub'] - upgraded_rub,
        })
    return offers


def _insert_order_con(con: sqlite3.Connection, *, order_id: str, user_id: int, tariff_id: int | None,
                      title: str, rub: int, stars: int, days: int, channels: list[int], urls: list[str],
                      promo_code: str, provider: str, order_kind: str = 'tariff') -> dict:
    expiry = in_seconds(35 * 60)
    attribution = latest_greeting_touch(con, user_id) if provider != 'admin' else None
    con.execute(
        '''INSERT INTO orders(id,user_id,tariff_id,title,amount_rub,amount_stars,
           duration_days,channels,promo_code,provider,created_at,expires_at,delivery_urls,
           greeting_event_id,order_kind)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (order_id, user_id, tariff_id, title, rub, stars, days, json.dumps(channels), promo_code,
         provider, now(), expiry, json.dumps(urls), attribution['id'] if attribution else None,
         order_kind),
    )
    return dict(con.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())


class Shop:
    def __init__(self, db: Database):
        self.db = db
        self.entitlement_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()

    async def catalogs(self):
        return await self.db.read('SELECT * FROM tariffs WHERE active=1 ORDER BY rub ASC, stars ASC, id ASC', many=True)

    async def get_tariff(self, tariff_id: int):
        return await self.db.read('SELECT * FROM tariffs WHERE id=?', (tariff_id,))

    async def profile(self, user_id: int):
        return await self.db.read('SELECT * FROM users WHERE user_id=?', (user_id,))

    async def tariff_buyers_count(self, tariff_id: int) -> int:
        row = await self.db.read("""SELECT COUNT(DISTINCT user_id) AS c FROM orders
            WHERE tariff_id=? AND status='paid' AND provider!='admin'""", (tariff_id,))
        return row['c']

    async def activate_promo(self, user_id: int, code: str):
        code = code.strip().upper()
        def txn(con):
            promo = con.execute('SELECT * FROM promos WHERE code=? AND enabled=1 AND used<max_uses', (code,)).fetchone()
            if not promo:
                raise ShopError('Код не найден или лимит исчерпан.')
            if not promo_has_discount(promo):
                raise ShopError('Этот промокод не даёт скидку и больше не действует.')
            if promo['owner_user_id'] is not None and promo['owner_user_id'] != user_id:
                raise ShopError('Этот промокод персональный и закреплён за другим пользователем.')
            if promo['expires_at'] and promo['expires_at'] <= now():
                raise ShopError('Срок действия этого промокода истёк.')
            redeemed = con.execute('SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?', (code, user_id)).fetchone()
            if redeemed:
                raise ShopError('Этот промокод уже использован.')
            con.execute('UPDATE users SET active_promo=? WHERE user_id=?', (code, user_id))
            return dict(promo)
        return await self.db.atomic(txn)

    async def user_promo(self, user_id: int) -> dict | None:
        user = await self.profile(user_id)
        if not user or not user['active_promo']:
            return None
        promo = await self.db.read('SELECT * FROM promos WHERE code=?', (user['active_promo'],))
        if (promo and promo['enabled'] and promo['used'] < promo['max_uses']
                and promo_has_discount(promo)
                and (promo['owner_user_id'] is None or promo['owner_user_id'] == user_id)
                and (not promo['expires_at'] or promo['expires_at'] > now())):
            if not await self.db.read('SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?', (promo['code'], user_id)):
                return promo
        return None

    async def user_price(self, user_id: int, tariff: dict):
        promo = await self.user_promo(user_id)
        discount = promo['discount'] if promo else 0
        discount_percent = int(promo.get('discount_percent') or 0) if promo else 0
        has_discount = bool(discount > 0 or discount_percent > 0)
        return (
            *price_with_promo(tariff['rub'], tariff['stars'], discount, discount_percent),
            promo['code'] if has_discount else '',
        )

    async def is_paying_user(self, user_id: int) -> bool:
        return await self.db.atomic(lambda con: _is_paying_user_con(con, user_id))

    async def user_ltv(self, user_id: int) -> int:
        return await self.db.atomic(lambda con: _user_ltv_con(con, user_id))

    async def upgrade_offers(self, user_id: int) -> list[dict]:
        return await self.db.atomic(lambda con: _upgrade_offers_con(con, user_id))

    async def create_order(self, user_id: int, tariff_id: int, provider: str, *, duration_days: int | None = None):
        if provider not in ('stars', 'manual', 'cryptopay', '2328', 'admin'):
            raise ShopError('Неизвестный способ оплаты.')
        order_id = uuid.uuid4().hex
        def txn(con):
            tariff = con.execute("SELECT * FROM tariffs WHERE id=? AND (active=1 OR ?='admin')",
                                 (tariff_id, provider)).fetchone()
            if not tariff:
                raise ShopError('Тариф не найден.')
            channels, urls = tariff_delivery(dict(tariff))
            days = tariff['duration_days'] if duration_days is None else duration_days
            if not 0 <= days <= MAX_DURATION_DAYS or (duration_days is not None and provider != 'admin'):
                raise ShopError('Проверьте срок тарифа. Допустимо от 0 до 3650 дней.')
            if urls and days:
                raise ShopError('Для готовых ссылок укажите срок 0.')
            user = con.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
            if not user:
                raise ShopError('Сначала нажми /start.')
            promo_code, discount, discount_percent = '', 0, 0
            timestamp = now()
            if provider != 'admin' and user['active_promo']:
                promo = con.execute('SELECT * FROM promos WHERE code=? AND enabled=1', (user['active_promo'],)).fetchone()
                if (promo and promo['used'] < promo['max_uses']
                        and promo_has_discount(promo)
                        and (promo['owner_user_id'] is None or promo['owner_user_id'] == user_id)
                        and (not promo['expires_at'] or promo['expires_at'] > timestamp)
                        and not con.execute(
                            'SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?', (promo['code'], user_id)).fetchone()):
                    discount = promo['discount']
                    discount_percent = promo['discount_percent'] if 'discount_percent' in promo.keys() else 0
                    promo_code = promo['code']
                    if provider != 'stars' and promo['is_personal'] and con.execute(
                        """SELECT 1 FROM orders WHERE promo_code=? AND user_id=?
                           AND (
                               (provider='stars' AND status IN ('pending','expired') AND checkout_id!='')
                               OR (provider='manual' AND status='review')
                               OR (provider='manual' AND status IN ('pending','expired') AND expires_at>?)
                               OR (provider IN ('2328','cryptopay') AND expires_at>?
                                   AND (status='pending' OR (status='expired' AND (external_id!='' OR payment_url!=''))))
                           ) LIMIT 1""",
                        (promo_code, user_id, timestamp, timestamp),
                    ).fetchone():
                        raise ShopError('Не удалось создать счёт. Выбери другой способ оплаты или напиши в поддержку.')
            rub, stars = price_with_promo(tariff['rub'], tariff['stars'], discount, discount_percent)
            if provider == 'stars':
                pending = con.execute("""SELECT * FROM orders
                    WHERE provider='stars' AND user_id=? AND tariff_id=? AND order_kind='tariff'
                      AND status='pending' AND expires_at>?
                    ORDER BY checkout_id!='' DESC,created_at DESC,rowid DESC""", (user_id, tariff_id, timestamp)).fetchall()
                for existing in pending:
                    if existing['checkout_id'] and existing['checkout_until'] > timestamp:
                        return dict(existing)
                for existing in pending:
                    if (not existing['checkout_id'] and matches_delivery(existing, tariff, channels, urls)
                        and existing['amount_stars'] == stars and existing['promo_code'] == promo_code
                        and (existing['payment_url'] or existing['invoice_sent_at'] is None)):
                        return dict(existing)
                con.execute("""UPDATE orders SET status='expired' WHERE user_id=? AND tariff_id=?
                    AND order_kind='tariff' AND provider='stars' AND status='pending' AND checkout_id=''""",
                    (user_id, tariff_id))
            return _insert_order_con(
                con, order_id=order_id, user_id=user_id, tariff_id=tariff_id, title=tariff['title'],
                rub=rub, stars=stars, days=days, channels=channels, urls=urls, promo_code=promo_code,
                provider=provider, order_kind='tariff',
            )
        return await self.db.atomic(txn)

    async def create_upgrade_order(self, user_id: int, tariff_id: int, provider: str):
        if provider not in ('stars', 'manual', 'cryptopay', '2328'):
            raise ShopError('Неизвестный способ оплаты.')
        order_id = uuid.uuid4().hex
        def txn(con):
            user = con.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
            if not user:
                raise ShopError('Сначала нажми /start.')
            offers = _upgrade_offers_con(con, user_id)
            offer = next((item for item in offers if item['tariff']['id'] == tariff_id), None)
            if not offer:
                raise ShopError('Предложение апгрейда для этого тарифа больше недоступно.')
            tariff = offer['tariff']
            channels, urls = tariff_delivery(tariff)
            title = f"Апгрейд: {tariff['title']}"[:180]
            rub, stars = offer['rub'], offer['stars']
            timestamp = now()
            if provider == 'stars':
                pending = con.execute("""SELECT * FROM orders
                    WHERE provider='stars' AND user_id=? AND tariff_id=? AND order_kind='upgrade'
                      AND status='pending' AND expires_at>?
                    ORDER BY checkout_id!='' DESC,created_at DESC,rowid DESC""", (user_id, tariff_id, timestamp)).fetchall()
                for existing in pending:
                    if existing['checkout_id'] and existing['checkout_until'] > timestamp:
                        return dict(existing)
                for existing in pending:
                    if (not existing['checkout_id'] and matches_delivery(existing, tariff, channels, urls)
                        and existing['amount_rub'] == rub and existing['amount_stars'] == stars
                        and (existing['payment_url'] or existing['invoice_sent_at'] is None)):
                        return dict(existing)
                con.execute("""UPDATE orders SET status='expired' WHERE user_id=? AND tariff_id=?
                    AND order_kind='upgrade' AND provider='stars' AND status='pending' AND checkout_id=''""",
                    (user_id, tariff_id))
            return _insert_order_con(
                con, order_id=order_id, user_id=user_id, tariff_id=tariff_id, title=title,
                rub=rub, stars=stars, days=0, channels=channels, urls=urls, promo_code='',
                provider=provider, order_kind='upgrade',
            )
        return await self.db.atomic(txn)

    async def order(self, order_id: str):
        return await self.db.read('SELECT * FROM orders WHERE id=?', (order_id,))

    async def mark_invoice_sent(self, order_id: str, payment_url: str = ''):
        await self.db.execute(
            """UPDATE orders SET invoice_sent_at=?,invoice_sending_at=NULL,
               payment_url=COALESCE(NULLIF(?,''),payment_url) WHERE id=?""",
            (now(), payment_url, order_id),
        )

    async def release_invoice_claim(self, order_id: str):
        """Release an unfinished invoice creation lock after a transient Telegram error."""
        await self.db.execute("""UPDATE orders SET invoice_sending_at=NULL WHERE id=? AND invoice_sent_at IS NULL""", (order_id,))

    async def claim_invoice(self, order_id: str) -> bool:
        timestamp = now()
        return bool(await self.db.execute("""UPDATE orders SET invoice_sending_at=?
            WHERE id=? AND status='pending' AND expires_at>? AND invoice_sent_at IS NULL AND checkout_id=''
              AND (invoice_sending_at IS NULL OR invoice_sending_at<?)""",
            (timestamp, order_id, timestamp, in_seconds(-30))))

    async def authorize_stars(self, order_id: str, user_id: int, amount: int, checkout_id: str,
                              *, deadline: float | None = None, ignore_repeat: bool = False) -> bool | None:
        """Reserve the invoice. Return None when the caller should skip a duplicate query."""
        def txn(con):
            if deadline is not None and monotonic() >= deadline:
                return False
            timestamp = now()
            order = con.execute("""SELECT * FROM orders WHERE id=? AND user_id=? AND amount_stars=?
                AND provider='stars' AND status='pending' AND expires_at>?""",
                (order_id, user_id, amount, timestamp)).fetchone()
            if not order:
                return False
            if order['checkout_id']:
                if order['checkout_id'] != checkout_id:
                    return False
                return None if ignore_repeat else order['checkout_until'] > timestamp
            tariff = con.execute('SELECT * FROM tariffs WHERE id=? AND active=1', (order['tariff_id'],)).fetchone()
            if not tariff:
                return False
            try:
                channels, urls = tariff_delivery(dict(tariff))
                if not matches_delivery(order, tariff, channels, urls):
                    return False
            except ValueError:
                return False
            # Existing installs may still have several valid invoices for this product.
            if con.execute("""SELECT 1 FROM orders WHERE user_id=? AND tariff_id=? AND order_kind=? AND id!=?
                AND provider='stars' AND status IN ('pending','expired') AND checkout_id!='' AND checkout_until>?
                LIMIT 1""", (user_id, order['tariff_id'], order['order_kind'], order_id, timestamp)).fetchone():
                return False
            if order['order_kind'] == 'upgrade':
                offer = next((item for item in _upgrade_offers_con(con, user_id)
                              if item['tariff']['id'] == order['tariff_id']), None)
                if not offer or offer['stars'] > order['amount_stars']:
                    return False
            code = order['promo_code']
            if code and not _promo_slot_available(con, order, timestamp, switching_to_stars=True):
                return False
            if deadline is not None and monotonic() >= deadline:
                return False
            checkout_until = in_seconds(15 * 60)
            con.execute('UPDATE orders SET checkout_id=?,checkout_until=? WHERE id=?',
                        (checkout_id, checkout_until, order_id))
            con.execute("""UPDATE orders SET status='expired' WHERE user_id=? AND tariff_id=? AND order_kind=? AND id!=?
                AND provider='stars' AND status='pending' AND checkout_id=''""",
                (user_id, order['tariff_id'], order['order_kind'], order_id))
            return True
        return await self.db.atomic(txn)

    async def release_stars_checkout(self, order_id: str, checkout_id: str):
        await self.db.execute("""UPDATE orders SET checkout_id='',checkout_until=NULL
            WHERE id=? AND provider='stars' AND status IN ('pending','expired') AND checkout_id=?""",
            (order_id, checkout_id))

    async def settle_stars(self, order_id: str, user_id: int, amount: int, charge_id: str,
                           *, paid_at: datetime | None = None) -> bool:
        order = await self.order(order_id)
        if not order or order['provider'] != 'stars' or order['user_id'] != user_id or order['amount_stars'] != amount:
            raise ShopError('Платёж Stars не соответствует заказу.')
        return await self.settle(order_id, 'stars', charge_id=charge_id, paid_at=paid_at)

    async def refund_stars(self, charge_id: str, amount: int, *, user_id: int | None = None,
                           order_id: str | None = None,
                           request_refund: Callable[[], Awaitable[bool]] | None = None) -> bool:
        if not charge_id:
            return False

        def txn(con):
            order = con.execute("SELECT * FROM orders WHERE provider='stars' AND charge_id=?", (charge_id,)).fetchone()
            if not order and order_id:
                order = con.execute("SELECT * FROM orders WHERE provider='stars' AND id=?", (order_id,)).fetchone()
            if not order:
                con.execute('INSERT OR IGNORE INTO stars_refund_inbox VALUES(?,?,?)',
                            (charge_id, amount, user_id))
                return False
            if (order_id and order['id'] != order_id) or (
                user_id is not None and order['user_id'] != user_id
            ) or order['amount_stars'] != amount or order['charge_id'] not in ('', charge_id):
                return False
            if order['status'] not in ('paid', 'pending', 'expired'):
                return False
            con.execute('UPDATE orders SET charge_id=? WHERE id=?', (charge_id, order['id']))
            self._finish_refund(con, order['id'])
            return True
        async with self.delivery_lock:
            if request_refund:
                order = await self.order(order_id)
                if (not order or order['status'] != 'paid'
                    or order['provider'] != 'stars' or order['charge_id'] != charge_id
                    or order['amount_stars'] != amount or order['user_id'] != user_id):
                    return False
                previous_pause = order['refund_requested_at']
                requested_at = previous_pause or now()
                if not previous_pause:
                    await self.db.execute('UPDATE orders SET refund_requested_at=? WHERE id=?', (requested_at, order_id))

                async def restore_delivery():
                    # A previous timed-out attempt may still have succeeded remotely.
                    await self.db.execute("""UPDATE orders SET refund_requested_at=?
                        WHERE id=? AND status IN ('paid','expired') AND refund_requested_at=?""",
                        (previous_pause, order_id, requested_at))

                try:
                    refunded = await request_refund()
                except TelegramBadRequest as exc:
                    if 'charge_already_refunded' in exc.message.lower():
                        refunded = True
                    else:
                        await restore_delivery()
                        raise
                except (TelegramForbiddenError, TelegramRetryAfter):
                    await restore_delivery()
                    raise
                if not refunded:
                    await restore_delivery()
                    return False
            async with self.entitlement_lock:
                return await self.db.atomic(txn)

    @staticmethod
    def _finish_refund(con, order_id: str):
        timestamp = now()
        con.execute("UPDATE orders SET status='refunded',refund_requested_at=NULL WHERE id=?", (order_id,))
        con.execute("""UPDATE purchases SET expires_at=? WHERE order_id=?
            AND (expires_at IS NULL OR expires_at>?)""", (timestamp, order_id, timestamp))

    async def refund_gateway(self, order_id: str, external_id: str) -> bool:
        def txn(con):
            row = con.execute("""SELECT status FROM orders WHERE id=? AND provider='2328' AND external_id=?
                AND external_id!=''""", (order_id, external_id)).fetchone()
            if not row or row['status'] not in ('pending', 'expired', 'paid'):
                return False
            self._finish_refund(con, order_id)
            return True
        async with self.delivery_lock, self.entitlement_lock:
            return await self.db.atomic(txn)

    async def pause_gateway_delivery(self, order_id: str, external_id: str):
        async with self.delivery_lock, self.entitlement_lock:
            await self.db.execute("""UPDATE orders SET refund_requested_at=?,reconcile_at=NULL
                WHERE id=? AND provider='2328' AND external_id=? AND status IN ('pending','expired','paid')
                  AND refund_requested_at IS NULL""", (now(), order_id, external_id))

    async def resume_gateway_delivery(self, order_id: str, external_id: str):
        """Resume only after the merchant API confirms a failed refund."""
        async with self.delivery_lock, self.entitlement_lock:
            return bool(await self.db.execute("""UPDATE orders
                SET refund_requested_at=NULL,reconcile_at=NULL
                WHERE id=? AND provider='2328' AND external_id=? AND external_id!=''
                  AND status IN ('pending','expired','paid') AND refund_requested_at IS NOT NULL""",
                (order_id, external_id)))

    async def replace_purchase_links(self, purchase_id: int, value: str) -> bool:
        urls = json.dumps(telegram_links(value))
        async with self.delivery_lock:
            return bool(await self.db.execute("""UPDATE orders
                SET delivery_urls=?,notice_sent=0,notice_retry_at=NULL
                WHERE id=(SELECT order_id FROM purchases WHERE id=? AND (expires_at IS NULL OR expires_at>?))
                  AND status='paid' AND refund_requested_at IS NULL AND delivery_urls!='[]'""",
                (urls, purchase_id, now())))

    async def begin_external_invoice(self, order_id: str, provider: str) -> bool:
        def txn(con):
            timestamp = now()
            order = con.execute("""SELECT * FROM orders WHERE id=? AND provider=? AND status='pending'
                AND expires_at>? AND external_id='' AND refund_requested_at IS NULL""",
                (order_id, provider, timestamp)).fetchone()
            if not order:
                return False
            if order['invoice_sending_at']:
                # Only 2328 supports a repeat of the same provider request.
                return provider == '2328'
            if order['promo_code'] and not _promo_slot_available(con, order, timestamp):
                con.execute("UPDATE orders SET status='expired',expires_at=? WHERE id=?", (timestamp, order_id))
                return False
            con.execute('UPDATE orders SET invoice_sending_at=?,expires_at=MAX(expires_at,?) WHERE id=?',
                        (timestamp, in_seconds(EXTERNAL_INVOICE_TTL_SECONDS), order_id))
            return True
        return await self.db.atomic(txn)

    async def set_external(self, order_id: str, provider: str, external_id: str, url: str,
                           *, expires_at: str | None = None) -> bool:
        """Bind a verified invoice and return whether checkout is still open."""
        if not url.startswith('https://') or not external_id:
            raise ShopError('Провайдер не выдал безопасную ссылку или номер счёта.')
        def txn(con):
            timestamp = now()
            order = con.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not order or order['provider'] != provider:
                raise ShopError('Заказ уже закрыт.')
            if order['external_id'] and order['external_id'] != external_id:
                raise ShopError('Идентификатор внешнего счёта нельзя менять.')
            if order['external_id'] == external_id and order['payment_url']:
                return (order['status'] == 'pending' and order['expires_at'] > timestamp
                        and not order['refund_requested_at'])
            # Retain invoice evidence for reconciliation even after checkout closed.
            if order['status'] not in ('pending', 'expired'):
                raise ShopError('Заказ уже закрыт.')
            effective_until = expires_at or in_seconds(EXTERNAL_INVOICE_TTL_SECONDS)
            if con.execute("""SELECT 1 FROM orders
                WHERE provider=? AND external_id=? AND id!=? LIMIT 1""", (provider, external_id, order_id)).fetchone():
                raise ShopError('Дублирующийся ID платежа.')
            open_checkout = order['status'] == 'pending' and effective_until > timestamp
            next_status = 'pending' if open_checkout else 'expired'
            con.execute(
                '''UPDATE orders SET external_id=?,payment_url=?,expires_at=?,status=?,invoice_sending_at=NULL
                   WHERE id=?''',
                (external_id, url, effective_until, next_status, order_id),
            )
            return open_checkout and not order['refund_requested_at']
        return await self.db.atomic(txn)

    async def expire_external_invoice(self, order_id: str, provider: str, external_id: str):
        await self.db.execute("""UPDATE orders SET status='expired',expires_at=MIN(expires_at,?)
            WHERE id=? AND provider=? AND external_id=? AND external_id!='' AND status='pending'""",
            (now(), order_id, provider, external_id))

    async def manual_review(self, order_id: str, user_id: int) -> bool:
        def txn(con):
            order = con.execute(
                "SELECT * FROM orders WHERE id=? AND user_id=? AND provider='manual' AND status IN ('pending','expired')",
                (order_id, user_id),
            ).fetchone()
            if not order:
                return False
            return bool(con.execute(
                "UPDATE orders SET status='review' WHERE id=? AND user_id=? AND provider='manual' AND status IN ('pending','expired')",
                (order_id, user_id),
            ).rowcount)
        return await self.db.atomic(txn)

    async def settle(self, order_id: str, provider: str, *, external_id: str = '', charge_id: str = '',
                     paid_at: datetime | None = None) -> bool:
        payment_time = (paid_at.replace(tzinfo=timezone.utc) if paid_at and paid_at.tzinfo is None else paid_at)
        paid_timestamp = payment_time.astimezone(timezone.utc).isoformat(timespec='seconds') if payment_time else now()
        def txn(con):
            row = con.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not row:
                raise ShopError('Заказ не найден.')
            if row['provider'] != provider:
                raise ShopError('Несоответствие провайдера платежа.')
            if row['status'] in ('paid', 'refunded'):
                if charge_id and row['charge_id'] and charge_id != row['charge_id']:
                    raise ShopError('Попытка повторного платежа с другим идентификатором.')
                # The ledger can repair dates written by an older version during
                # recovery, without issuing a second purchase or renewing access.
                evidence_matches = (provider == 'stars' and charge_id and charge_id == row['charge_id']) or (
                    provider in ('cryptopay', '2328') and external_id and external_id == row['external_id'])
                if row['status'] == 'paid' and payment_time is not None and evidence_matches and row['paid_at'] != paid_timestamp:
                    con.execute('UPDATE orders SET paid_at=? WHERE id=?', (paid_timestamp, order_id))
                return False
            allowed = ('review',) if provider == 'manual' else ('pending', 'expired')
            if row['status'] not in allowed:
                raise ShopError('Заказ не ожидает подтверждения.')
            if provider in ('2328', 'cryptopay'):
                if not external_id or external_id != row['external_id']:
                    raise ShopError('Платёж не соответствует сохранённому инвойсу.')
                if con.execute("""SELECT 1 FROM orders WHERE provider=? AND external_id=?
                    AND id!=? AND status IN ('paid','refunded') LIMIT 1""",
                    (provider, external_id, order_id)).fetchone():
                    raise ShopError('Дублирующийся ID платежа.')
            if row['refund_requested_at']:
                return False
            if provider == 'stars' and not charge_id:
                raise ShopError('Нет telegram_payment_charge_id.')
            if provider == 'stars' and con.execute("""SELECT 1 FROM stars_refund_inbox
                WHERE charge_id=? AND amount=? AND (user_id IS NULL OR user_id=?)""",
                (charge_id, row['amount_stars'], row['user_id'])).fetchone():
                con.execute("UPDATE orders SET status='refunded',charge_id=? WHERE id=?", (charge_id, order_id))
                con.execute('DELETE FROM stars_refund_inbox WHERE charge_id=?', (charge_id,))
                return False
            if provider == 'manual' and not row['external_id']:
                con.execute('UPDATE orders SET external_id=? WHERE id=?', ('admin-verified', order_id))
            try:
                con.execute('''UPDATE orders SET status='paid',paid_at=?,charge_id=? WHERE id=?''',
                            (paid_timestamp, charge_id, order_id))
            except sqlite3.IntegrityError as exc:
                raise ShopError('Дублирующийся ID платежа.') from exc
            # Previously issued invoices may contain out-of-range durations.
            expires = until(max(0, min(row['duration_days'], MAX_DURATION_DAYS)))
            cur = con.execute('''INSERT INTO purchases(order_id,user_id,tariff_id,title,started_at,expires_at)
                             VALUES(?,?,?,?,?,?)''', (order_id, row['user_id'], row['tariff_id'], row['title'], now(), expires))
            for ch in bind_order_channels(con, row):
                con.execute('INSERT INTO grants(purchase_id,user_id,chat_id) VALUES(?,?,?)', (cur.lastrowid, row['user_id'], ch))
            if row['promo_code']:
                con.execute('INSERT OR IGNORE INTO promo_redemptions(code,user_id,order_id) VALUES(?,?,?)',
                            (row['promo_code'], row['user_id'], order_id))
                if con.execute('SELECT changes()').fetchone()[0]:
                    con.execute('UPDATE promos SET used=used+1 WHERE code=?', (row['promo_code'],))
                con.execute("UPDATE users SET active_promo='' WHERE user_id=? AND active_promo=?",
                            (row['user_id'], row['promo_code']))
            return True
        async with self.entitlement_lock:
            return bool(await self.db.atomic(txn))

    async def purchases(self, user_id: int, *, limit: int = 30, offset: int = 0):
        return await self.db.read("""SELECT p.*,o.provider,o.status AS order_status,o.delivery_urls,o.refund_requested_at FROM purchases p
             JOIN orders o ON p.order_id=o.id WHERE p.user_id=?
             ORDER BY CASE WHEN p.expires_at IS NULL OR p.expires_at>? THEN 0 ELSE 1 END, p.id DESC
             LIMIT ? OFFSET ?""", (user_id, now(), limit, offset), many=True)

    async def purchase_for_user(self, user_id: int, purchase_id: int):
        return await self.db.read("""SELECT p.*,o.status AS order_status,o.delivery_urls,o.refund_requested_at
            FROM purchases p JOIN orders o ON o.id=p.order_id WHERE p.user_id=? AND p.id=?""",
            (user_id, purchase_id))

    async def purchase_grants(self, purchase_id: int):
        return await self.db.read('''SELECT g.*, l.url AS shared_url FROM grants g
            LEFT JOIN product_channel_links l ON l.chat_id=g.chat_id
            WHERE g.purchase_id=? ORDER BY g.chat_id''', (purchase_id,), many=True)

    async def statistics(self):
        """Internal ground-truth counters for administration and tests."""
        def read():
            with self.db.connect() as con:
                return {
                    'users': con.execute('SELECT COUNT(*) FROM users').fetchone()[0],
                    'paid': con.execute("SELECT COUNT(*) FROM orders WHERE status='paid' AND provider!='admin'").fetchone()[0],
                    'pending': con.execute("SELECT COUNT(*) FROM orders WHERE status IN ('pending','review')").fetchone()[0],
                    'grants': con.execute("""SELECT COUNT(*) FROM grants g JOIN purchases p ON p.id=g.purchase_id
                        JOIN orders o ON o.id=p.order_id
                        LEFT JOIN product_channel_links l ON l.chat_id=g.chat_id
                        WHERE o.status='paid' AND o.refund_requested_at IS NULL AND p.revoked_by_admin=0
                          AND g.state NOT IN ('expired','revoked')
                          AND (g.state IN ('ready','joined') OR l.url IS NOT NULL)
                          AND (p.expires_at IS NULL OR p.expires_at>?)""",
                        (now(),)).fetchone()[0],
                }
        return await asyncio.to_thread(read)

    async def get_recent_purchases(self, limit: int = 3) -> list[tuple[str, str]]:
        rows = await self.db.read("""SELECT title, paid_at FROM orders
            WHERE status='paid' AND provider!='admin' AND order_kind IN ('tariff', 'upgrade') AND paid_at IS NOT NULL
            ORDER BY paid_at DESC,rowid DESC LIMIT ?""", (limit,), many=True)
        current = datetime.now(timezone.utc)
        result = []
        for row in rows:
            seconds = max(0, int((current - parse_date(row['paid_at'])).total_seconds()))
            if seconds < 60:
                ago = 'только что'
            elif seconds < 3600:
                ago = f'{seconds // 60} мин назад'
            elif seconds < 86400:
                ago = f'{seconds // 3600} ч назад'
            else:
                ago = f'{seconds // 86400} дн назад'
            result.append((row['title'], ago))
        return result

    async def get_stats_data(self) -> dict:
        current = datetime.now(timezone.utc)
        started = await self.db.setting('project_started_at')
        start = parse_date(started['value']) if started and started['value'] else current
        today = await self.db.read("SELECT COUNT(*) AS c FROM users WHERE created_at>=?",
                                   (current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),))
        popular = await self.db.read("""SELECT t.title FROM orders o JOIN tariffs t ON t.id=o.tariff_id
            WHERE o.status='paid' AND o.provider!='admin'
            GROUP BY o.tariff_id ORDER BY COUNT(*) DESC, o.tariff_id LIMIT 1""")
        return {
            'days_working': max(0, (current - start).days),
            'new_users_today': today['c'],
            'popular_category': popular['title'] if popular else 'Пока нет покупок',
            'recent_purchases': await self.get_recent_purchases(),
        }


async def available_quick_promo(
    db: Database, user_id: int, *, user: dict | None = None
) -> tuple[dict | None, str, bool]:
    """Return (promo_row, claim_callback, redeemed_luxury) if user has an unapplied 1-tap promo."""
    user_row = user if user is not None else await db.read('SELECT * FROM users WHERE user_id=?', (user_id,))
    saved_personal = ((user_row or {}).get('promo_push_code') or '').strip().upper()
    if saved_personal and saved_personal != 'LUXURYNEW':
        pers_row = await db.read(
            """SELECT p.code, p.discount, p.discount_percent, p.enabled, p.used, p.max_uses
               FROM promos p
               WHERE p.code=? AND p.enabled=1 AND p.used<p.max_uses
                 AND (p.discount>0 OR COALESCE(p.discount_percent,0)>0)
                 AND (p.owner_user_id IS NULL OR p.owner_user_id=?)
                 AND (p.expires_at IS NULL OR p.expires_at>?)
                 AND NOT EXISTS(SELECT 1 FROM promo_redemptions r WHERE r.code=p.code AND r.user_id=?)""",
            (saved_personal, user_id, now(), user_id),
        )
        if pers_row:
            return pers_row, f"claim_promo:{pers_row['code']}", False
    promo_row = await db.read(
        """SELECT p.code, p.discount, p.discount_percent, p.enabled, p.used, p.max_uses, p.expires_at,
                  EXISTS(SELECT 1 FROM promo_redemptions r WHERE r.code=p.code AND r.user_id=?) AS redeemed
           FROM promos p WHERE p.code='LUXURYNEW'""",
        (user_id,),
    )
    if (promo_row and promo_row['enabled'] and promo_row['used'] < promo_row['max_uses']
            and (not promo_row['expires_at'] or promo_row['expires_at'] > now())
            and (promo_row['discount'] > 0 or int(promo_row.get('discount_percent') or 0) > 0)):
        redeemed_luxury = bool(promo_row['redeemed'])
        if not redeemed_luxury:
            return promo_row, 'claim_promo', False
        return None, '', True
    return None, '', False
