"""Automated and manual promo-code and review-screenshot push campaigns."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from .alerts import _lock_for
from .analytics import campaign_delivered, prepare_campaign
from .db import DEFAULT_PROJECT_NAME, Database, in_seconds, now
from .service import Shop, price_with_promo, promo_discount_label
from .ui import _utf16_len, button_tariff_name, cb, esc, fmt_rub, is_https_url, kb, link, remaining_time_label, send_text


def parse_push_discount(raw: str | int | None, *, strict: bool = False, default_rub: int = 150) -> tuple[int, int]:
    """Parse discount value like '20%' -> (0, 20) or '150' -> (150, 0)."""
    text = '' if raw is None else str(raw).strip()
    if not text:
        if strict:
            raise ValueError('Укажите скидку: число в ₽ или в % (например 200 или 20%).')
        return default_rub, 0
    if text.endswith('%'):
        num = text[:-1].rstrip()
        if re.fullmatch(r'0*[0-9]{1,2}', num) and 1 <= int(num.lstrip('0') or '0') <= 99:
            return 0, int(num.lstrip('0'))
        if strict:
            raise ValueError('Укажите процент скидки от 1% до 99% (например 20%).')
        return default_rub, 0
    num = text.lstrip('0')
    if re.fullmatch(r'[0-9]{1,6}', num) and 1 <= int(num) <= 100_000:
        return int(num), 0
    if strict:
        raise ValueError('Укажите скидку от 1 до 100000 ₽ или от 1% до 99%.')
    return default_rub, 0

log = logging.getLogger(__name__)

_PROMO_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
_MEDIA_SENDERS = {'photo': 'send_photo', 'video': 'send_video', 'animation': 'send_animation'}
_TEMPLATE_VAR_RE = re.compile(r'\{(project_name|name|code|discount|ttl_note)\}')
_PUSH_LEASE_SECONDS = 600

AUDIENCE_LABELS = {
    'cold': '❄️ Некупившие',
    'buyers': '💳 Купившие',
    'all': '🌍 Все пользователи',
}


async def _send_request(sender, *args, retry_flood: bool, **kwargs):
    try:
        return await asyncio.wait_for(sender(*args, **kwargs), timeout=15)
    except TelegramRetryAfter as exc:
        if not retry_flood or exc.retry_after > 60:
            raise
        await asyncio.sleep(max(exc.retry_after, 0))
        return await asyncio.wait_for(sender(*args, **kwargs), timeout=15)


async def _send_push_text(bot, chat_id: int, text: str, markup, *, retry_flood: bool):
    async def sender(content, **kwargs):
        return await _send_request(bot.send_message, chat_id, content, retry_flood=retry_flood, **kwargs)

    await send_text(sender, text, markup)


async def safe_send_push_text(bot, chat_id: int, text: str, markup) -> bool:
    try:
        await _send_push_text(bot, chat_id, text, markup, retry_flood=False)
        return True
    except (TelegramAPIError, TimeoutError) as exc:
        log.debug('Promo notification delivery to %s failed: %s', chat_id, type(exc).__name__)
        return False


async def _send_push_media(bot, chat_id: int, media_id: str, media_type: str,
                           caption: str, markup, *, retry_flood: bool):
    sender = getattr(bot, _MEDIA_SENDERS.get(media_type, ''), None)
    if not media_id or sender is None:
        await _send_push_text(bot, chat_id, caption, markup, retry_flood=retry_flood)
        return

    caption_fits = _utf16_len(caption) <= 950
    await _send_request(
        sender, chat_id, media_id,
        caption=caption if caption_fits and caption.strip() else None,
        reply_markup=markup, retry_flood=retry_flood,
    )
    if not caption_fits and caption.strip():
        try:
            await _send_push_text(bot, chat_id, caption, None, retry_flood=retry_flood)
        except (TelegramAPIError, TimeoutError) as exc:
            log.debug('Review media delivered to %s, but caption failed: %s', chat_id, type(exc).__name__)


PERMANENT_BLOCK_UNTIL = '9999-12-31T23:59:59+00:00'
_PERMANENT_ERROR_PATTERNS = (
    'bot was blocked',
    'user is deactivated',
    'chat not found',
    'user not found',
    "bot can't initiate",
)


def _is_permanent_delivery_error(exc: Exception) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    if isinstance(exc, TelegramBadRequest):
        msg = str(exc).lower()
        return any(pattern in msg for pattern in _PERMANENT_ERROR_PATTERNS)
    return False


def _retry_at(exc: Exception) -> str:
    if _is_permanent_delivery_error(exc):
        return PERMANENT_BLOCK_UNTIL
    return in_seconds(max(1, exc.retry_after) if isinstance(exc, TelegramRetryAfter) else 15 * 60)


async def safe_send_media(bot, chat_id: int, media_id: str, media_type: str,
                          caption: str, markup=None, *, retry_flood: bool = False) -> bool:
    """Send Telegram media with HTML caption safely, splitting long captions cleanly."""
    try:
        await _send_push_media(bot, chat_id, media_id, media_type, caption, markup, retry_flood=retry_flood)
        return True
    except (TelegramAPIError, TimeoutError) as exc:
        log.debug('Media notification delivery to %s failed: %s', chat_id, type(exc).__name__)
        return False


def render_push_template(template: str, **values: str) -> str:
    """Substitute supported push placeholders once with HTML escaping."""
    variables = {'project_name': DEFAULT_PROJECT_NAME, 'name': '', 'code': '', 'discount': '', 'ttl_note': '', **values}
    return _TEMPLATE_VAR_RE.sub(lambda m: esc(variables[m[1]]), template)


async def push_settings(db: Database) -> dict[str, str]:
    rows = await db.read(
        """SELECT key, value FROM settings WHERE key IN (
               'project_name', 'reviews_url', 'reviews_visible',
               'promo_push_enabled', 'promo_push_mode', 'promo_push_code',
               'promo_push_prefix', 'promo_push_discount', 'promo_push_ttl_hours',
               'promo_push_delay_min', 'promo_push_text',
               'review_push_enabled', 'review_push_audience', 'review_push_delay_min',
               'review_push_interval_hours', 'review_push_max_per_user',
               'review_push_order', 'review_push_text'
           )""",
        many=True,
    )
    return {row['key']: row['value'] for row in rows}


async def refresh_push_brand(db: Database, settings: dict[str, str], key: str) -> dict[str, str]:
    row = await db.read("""SELECT
        (SELECT value FROM settings WHERE key='project_name') AS project_name,
        (SELECT value FROM settings WHERE key=?) AS template,
        (SELECT media_id FROM settings WHERE key=?) AS media_id,
        (SELECT media_type FROM settings WHERE key=?) AS media_type""", (key, key, key))
    return {
        **settings,
        'project_name': (row or {}).get('project_name') or DEFAULT_PROJECT_NAME,
        key: (row or {}).get('template') or '',
        'media_id': (row or {}).get('media_id') or '',
        'media_type': (row or {}).get('media_type') or '',
    }


def _sanitized_prefix(raw: str) -> str:
    cleaned = re.sub(r'[^A-Z0-9_]', '', (raw or '').upper())[:12]
    return cleaned or 'VIP'


def int_setting(settings: dict[str, str], key: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(settings.get(key) or default))
    except (ValueError, TypeError):
        return max(minimum, default)


async def resolve_shared_promo(db: Database, code: str, *, user_id: int | None = None) -> dict | None:
    """Return an active usable shared promo code, optionally checking per-user redemption."""
    clean = (code or '').strip().upper()
    if not clean:
        return None
    promo = await db.read(
        """SELECT * FROM promos
           WHERE code=? AND enabled=1 AND used<max_uses AND is_personal=0
             AND (discount>0 OR COALESCE(discount_percent,0)>0)
             AND (expires_at IS NULL OR expires_at>?)""",
        (clean, now()),
    )
    if not promo:
        return None
    if user_id is not None:
        if promo['owner_user_id'] is not None and promo['owner_user_id'] != user_id:
            return None
        if await db.read('SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?', (clean, user_id)):
            return None
    return promo


async def create_personal_promo(db: Database, user_id: int, prefix: str,
                                discount: int, ttl_hours: int, *, discount_percent: int = 0) -> dict:
    """Create a single-use personal promo code bound to user_id."""
    clean_prefix = _sanitized_prefix(prefix)
    expires_at = in_seconds(ttl_hours * 3600) if ttl_hours > 0 else None
    pct = max(0, min(99, int(discount_percent or 0)))
    rub = max(0, int(discount or 0)) if pct == 0 else 0

    def txn(con):
        for _ in range(10):
            suffix = ''.join(secrets.choice(_PROMO_CODE_ALPHABET) for _ in range(6))
            code = f'{clean_prefix}_{suffix}'[:28]
            if not con.execute('SELECT 1 FROM promos WHERE code=?', (code,)).fetchone():
                con.execute(
                    """INSERT INTO promos(code, discount, discount_percent, max_uses, used, enabled, owner_user_id, expires_at, is_personal)
                       VALUES(?,?,?,1,0,1,?,?,1)""",
                    (code, rub, pct, user_id, expires_at),
                )
                return dict(con.execute('SELECT * FROM promos WHERE code=?', (code,)).fetchone())
        raise RuntimeError('Failed to allocate unique personal promo code')

    return await db.atomic(txn)


async def prepare_promo_push(db: Database, user: dict, *,
                             settings: dict[str, str] | None = None,
                             dry_run: bool = False) -> tuple[str, object, str, bool] | None:
    """Build (text, markup, promo_code, is_personal) for a user's promo push, or None if unavailable."""
    cfg = settings or await push_settings(db)
    mode = cfg.get('promo_push_mode') or 'shared'
    project_name = cfg.get('project_name') or DEFAULT_PROJECT_NAME
    template = cfg.get('promo_push_text') or ''

    if mode == 'personal':
        prefix = _sanitized_prefix(cfg.get('promo_push_prefix', 'VIP'))
        discount, discount_percent = parse_push_discount(cfg.get('promo_push_discount'))
        if discount <= 0 and discount_percent <= 0:
            return None
        ttl_hours = int_setting(cfg, 'promo_push_ttl_hours', 0, minimum=0)
        ttl_note = f' (действует {ttl_hours} ч.)' if ttl_hours > 0 else ''
        if dry_run:
            code = f'{prefix}_DEMO99'[:28]
        else:
            existing = None
            saved_code = (user.get('promo_push_code') or '').strip().upper()
            if saved_code:
                existing = await db.read(
                    """SELECT * FROM promos
                       WHERE code=? AND is_personal=1 AND owner_user_id=? AND enabled=1 AND used<max_uses
                         AND (discount>0 OR COALESCE(discount_percent,0)>0)
                         AND (expires_at IS NULL OR expires_at>?)
                         AND NOT EXISTS (SELECT 1 FROM promo_redemptions WHERE code=promos.code AND user_id=?)""",
                    (saved_code, user['user_id'], now(), user['user_id']),
                )
            if existing:
                code = existing['code']
                discount = existing['discount']
                discount_percent = int(existing.get('discount_percent') or 0)
                if existing['expires_at']:
                    ttl_note = f' (действует {ttl_hours} ч.)' if ttl_hours > 0 else ' (ограничен по времени)'
                else:
                    ttl_note = ''
            else:
                promo = await create_personal_promo(
                    db, user['user_id'], prefix, discount, ttl_hours, discount_percent=discount_percent,
                )
                code = promo['code']
        is_personal = True
    else:
        shared_code = (cfg.get('promo_push_code') or '').strip().upper()
        promo = await resolve_shared_promo(db, shared_code, user_id=None if dry_run else user['user_id'])
        if not promo:
            return None
        code = promo['code']
        discount = promo['discount']
        discount_percent = int(promo.get('discount_percent') or 0)
        ttl_note = ''
        is_personal = False

    discount_label = promo_discount_label({'discount': discount, 'discount_percent': discount_percent})
    render_template = template
    if discount_percent > 0:
        render_template = re.sub(r'(\{discount\}(?:\s*</(?:b|strong|i|em|u|ins|s|strike|del|code|span|tg-spoiler)>)*)(?:[^\S\r\n]|&nbsp;|&#160;|&#x[aA]0;)*₽',
                                 r'\1', template)
    text = render_push_template(
        render_template,
        name=user.get('name') or 'друг',
        project_name=project_name,
        code=code,
        discount=discount_label if discount_percent > 0 else str(discount),
        ttl_note=ttl_note,
    )
    viewed_tid = user.get('last_viewed_tariff_id') if not dry_run else None
    second_btn = None
    if viewed_tid:
        viewed = await db.read(
            """SELECT id, title FROM tariffs
               WHERE id=? AND active=1 AND (channel_ids!='' OR delivery_urls!='[]' OR allow_no_channel=1)""",
            (viewed_tid,),
        )
        if viewed:
            v_label = button_tariff_name(viewed['title'], 'тариф', max_len=20)
            second_btn = cb(f'🔓 «{v_label}» со скидкой', f"claim_tariff:{viewed['id']}:{code}", style='primary')
    btn_text = f'🎟 Применить промокод · {discount_label}' if is_personal else f'🎟 Применить промокод · {discount_label} · {code}'
    rows = [(cb(btn_text, f'claim_promo:{code}', style='success'),)]
    if second_btn:
        rows.append((second_btn,))
    markup = kb(*rows)
    return text, markup, code, is_personal


async def build_review_push(db: Database, shop: Shop, user: dict, review: dict, *,
                            settings: dict[str, str] | None = None,
                            is_paying: bool | None = None) -> tuple[str, object]:
    """Build (caption, markup) for a review-screenshot push (clean, no promo buttons)."""
    cfg = settings or await push_settings(db)
    project_name = cfg.get('project_name') or DEFAULT_PROJECT_NAME
    reviews_url = (cfg.get('reviews_url') or '').strip()
    raw_caption = (review.get('caption') or '').strip()
    if raw_caption:
        caption = render_push_template(
            raw_caption,
            name=user.get('name') or 'друг',
            project_name=project_name,
        )
    else:
        caption = render_push_template(
            cfg.get('review_push_text') or '',
            name=user.get('name') or 'друг',
            project_name=project_name,
        )
    if is_paying is None:
        is_paying = await shop.is_paying_user(user['user_id'])
    cta_label = '🔥 Другие разделы и апгрейд' if is_paying else '📂 Смотреть свежие заливы'
    rows: list[tuple] = [(cb(cta_label, 'catalog', style='success'),)]
    if is_https_url(reviews_url):
        rows.append((link('⭐ Все отзывы в канале ↗', reviews_url),))
    return caption, kb(*rows)


def _audience_sql(audience: str) -> str:
    if audience == 'buyers':
        return (
            "EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id "
            "AND o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL)"
        )
    if audience == 'all':
        return '1=1'
    return (
        "NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id "
        "AND ((o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL) "
        "OR o.status='review'))"
    )


PROMO_PUSH_ELIGIBLE_SQL = f"""
    u.promo_push_sent=0 AND u.first_start_at IS NOT NULL
    AND COALESCE(u.broadcast_opt_out, 0)=0
    AND (u.promo_push_retry_at IS NULL OR u.promo_push_retry_at<=:timestamp)
    AND {_audience_sql('cold')}
    AND NOT EXISTS (
        SELECT 1 FROM promos p WHERE p.code=u.active_promo AND p.enabled=1 AND p.used<p.max_uses
          AND (p.discount>0 OR COALESCE(p.discount_percent,0)>0)
          AND (p.owner_user_id IS NULL OR p.owner_user_id=u.user_id)
          AND (p.expires_at IS NULL OR p.expires_at>:timestamp)
          AND NOT EXISTS (
              SELECT 1 FROM promo_redemptions pr WHERE pr.code=p.code AND pr.user_id=u.user_id
          )
    )
"""


async def send_promo_push_to_user(
    bot, db: Database, user: dict, cfg: dict[str, str], *, retry_flood: bool = False
) -> str:
    """Validate promo eligibility, atomically claim user slot, and deliver promo push.

    A 'flooded' result tells the caller to halt the batch.
    """
    user_id = user['user_id']
    lease = in_seconds(_PUSH_LEASE_SECONDS)
    claimed = await db.execute(
        f"""UPDATE users AS u SET promo_push_retry_at=:lease
            WHERE u.user_id=:user_id AND {PROMO_PUSH_ELIGIBLE_SQL}""",
        {'lease': lease, 'user_id': user_id, 'timestamp': now()},
    )
    if not claimed:
        return 'skipped'

    payload = None
    try:
        settings = await refresh_push_brand(db, cfg, 'promo_push_text')
        payload = await prepare_promo_push(db, user, settings=settings)
        if payload:
            latest = await db.read('SELECT broadcast_opt_out FROM users WHERE user_id=?', (user_id,))
            if latest and latest['broadcast_opt_out']:
                payload = None
            else:
                text, markup, code, _ = payload
                touch_id, markup = await prepare_campaign(db, user_id, 'promo', markup)
                media_id = settings.get('media_id') or ''
                media_type = settings.get('media_type') or 'photo'
                if media_id:
                    await _send_push_media(bot, user_id, media_id, media_type, text, markup, retry_flood=retry_flood)
                else:
                    await _send_push_text(bot, user_id, text, markup, retry_flood=retry_flood)
                await campaign_delivered(db, touch_id)
    except (TelegramAPIError, TimeoutError) as exc:
        retry_at = _retry_at(exc)
        timed_out = isinstance(exc, TimeoutError)
        log.debug('Promo push delivery to %s failed: %s', user_id, type(exc).__name__)

        def failed(con):
            if payload and payload[3] and not timed_out:
                deleted = con.execute(
                    """DELETE FROM promos WHERE code=? AND is_personal=1 AND used=0
                       AND NOT EXISTS (SELECT 1 FROM promo_redemptions WHERE code=promos.code)""",
                    (payload[2],),
                ).rowcount
                if not deleted:
                    con.execute('UPDATE promos SET enabled=0 WHERE code=?', (payload[2],))
            # Keep the generated personal promo code when Telegram timed out because
            # the message may already have been delivered to the user.
            if payload and payload[3] and timed_out:
                con.execute(
                    """UPDATE users SET promo_push_retry_at=?, promo_push_code=?
                       WHERE user_id=? AND promo_push_retry_at=?""",
                    (retry_at, payload[2], user_id, lease),
                )
            else:
                con.execute(
                    'UPDATE users SET promo_push_retry_at=? WHERE user_id=? AND promo_push_retry_at=?',
                    (retry_at, user_id, lease),
                )

        await db.atomic(failed)
        if isinstance(exc, TelegramRetryAfter):
            return 'flooded'
        return 'failed'

    if payload:
        await db.execute(
            """UPDATE users SET promo_push_sent=1, promo_push_sent_at=?, promo_push_code=?, promo_push_retry_at=NULL
               WHERE user_id=? AND promo_push_retry_at=?""",
            (now(), code, user_id, lease),
        )
        return 'sent'
    await db.execute(
        'UPDATE users SET promo_push_retry_at=NULL WHERE user_id=? AND promo_push_retry_at=?',
        (user_id, lease),
    )
    return 'skipped'


async def dispatch_promo_pushes(
    bot, db: Database, shop: Shop, *, max_age_hours: int = 48, limit: int = 20
) -> int:
    """Send configurable post-/start promo push to users with zero purchases."""
    del shop
    cfg = await push_settings(db)
    if cfg.get('promo_push_enabled') != '1':
        return 0
    mode = cfg.get('promo_push_mode') or 'shared'
    shared_code = ''
    if mode == 'shared':
        promo = await resolve_shared_promo(db, cfg.get('promo_push_code', ''))
        if not promo:
            return 0
        shared_code = promo['code']
    delay_min = int_setting(cfg, 'promo_push_delay_min', 30, minimum=1)
    min_age = in_seconds(-delay_min * 60)
    max_age = in_seconds(-delay_min * 60 - max_age_hours * 3600)
    sent = 0
    async with _lock_for(db):
        rows = await db.read(
            f"""SELECT u.*
               FROM users u
               WHERE {PROMO_PUSH_ELIGIBLE_SQL}
                 AND u.first_start_at BETWEEN :max_age AND :min_age
                 AND NOT EXISTS (
                     SELECT 1 FROM promo_redemptions pr WHERE pr.code=:shared_code AND pr.user_id=u.user_id
                 )
               ORDER BY u.first_start_at, u.user_id
               LIMIT :limit""",
            {'timestamp': now(), 'max_age': max_age, 'min_age': min_age, 'shared_code': shared_code, 'limit': limit},
            many=True,
        )
        for user in rows:
            result = await send_promo_push_to_user(bot, db, user, cfg)
            if result == 'flooded':
                break
            if result == 'sent':
                sent += 1
    return sent


async def send_review_push_to_user(
    bot, db: Database, shop: Shop, user: dict, cfg: dict[str, str], *,
    review: dict | None = None, order_mode: str = 'seq', retry_flood: bool = False,
    scheduled: bool = False,
) -> str:
    """Claim a recipient and send an unseen review, recording confirmed media delivery."""
    user_id = user['user_id']
    active_only = review is None
    if review is not None:
        review = await db.read('SELECT * FROM reviews WHERE id=?', (review['id'],))
    else:
        order_clause = 'RANDOM()' if order_mode == 'random' else 'r.sort_order ASC, r.id ASC'
        review = await db.read(
            f"""SELECT r.* FROM reviews r
                WHERE r.active=1
                  AND NOT EXISTS (
                      SELECT 1 FROM user_review_pushes urp
                      WHERE urp.user_id=? AND urp.review_id=r.id
                  )
                ORDER BY {order_clause}
                LIMIT 1""",
            (user_id,),
        )
    if not review:
        return 'skipped'

    review_id = review['id']
    lease = in_seconds(_PUSH_LEASE_SECONDS)
    where = ''
    params = (lease, user_id, now(), review_id, active_only, user_id, review_id)
    if scheduled:
        where = f"""AND u.last_review_push_at IS ?
                     AND (u.promo_push_sent_at IS NULL OR u.promo_push_sent_at<=?)
                     AND {_audience_sql(cfg.get('review_push_audience', 'cold'))}"""
        params += (user['last_review_push_at'], in_seconds(-15 * 60))
    claimed = await db.execute(
        f"""UPDATE users AS u SET review_push_retry_at=?
           WHERE user_id=? AND COALESCE(broadcast_opt_out, 0)=0
             AND EXISTS (SELECT 1 FROM settings WHERE key='reviews_visible' AND value='1')
             AND (review_push_retry_at IS NULL OR review_push_retry_at<=?)
             AND EXISTS (SELECT 1 FROM reviews WHERE id=? AND (?=0 OR active=1))
             AND NOT EXISTS (SELECT 1 FROM user_review_pushes WHERE user_id=? AND review_id=?)
             {where}""",
        params,
    )
    if not claimed:
        return 'skipped'

    latest = await db.read("""SELECT broadcast_opt_out,
        (SELECT value FROM settings WHERE key='reviews_visible') AS reviews_visible
        FROM users WHERE user_id=?""", (user_id,))
    if not latest or latest['broadcast_opt_out'] or latest['reviews_visible'] != '1':
        await db.execute(
            'UPDATE users SET review_push_retry_at=NULL WHERE user_id=? AND review_push_retry_at=?',
            (user_id, lease),
        )
        return 'skipped'

    settings = await refresh_push_brand(db, cfg, 'review_push_text')
    caption, markup = await build_review_push(db, shop, user, review, settings=settings)
    try:
        touch_id, markup = await prepare_campaign(db, user_id, 'review', markup)
        await _send_push_media(
            bot, user_id, review['media_id'], review['media_type'], caption, markup, retry_flood=retry_flood,
        )
        await campaign_delivered(db, touch_id)
    except (TelegramAPIError, TimeoutError) as exc:
        log.debug('Review delivery to %s failed: %s', user_id, type(exc).__name__)
        await db.execute(
            'UPDATE users SET review_push_retry_at=? WHERE user_id=? AND review_push_retry_at=?',
            (_retry_at(exc), user_id, lease),
        )
        return 'flooded' if isinstance(exc, TelegramRetryAfter) else 'failed'

    def delivered(con):
        timestamp = now()
        cur = con.execute(
            """UPDATE users SET last_review_push_at=?, review_push_retry_at=NULL
               WHERE user_id=? AND review_push_retry_at=?""",
            (timestamp, user_id, lease),
        )
        if cur.rowcount:
            con.execute(
                """INSERT OR IGNORE INTO user_review_pushes(user_id, review_id, sent_at)
                   SELECT ?, id, ? FROM reviews WHERE id=?""",
                (user_id, timestamp, review_id),
            )
    await db.atomic(delivered)
    return 'sent'


async def dispatch_review_pushes(
    bot, db: Database, shop: Shop, *, max_age_hours: int = 30 * 24, limit: int = 15
) -> int:
    """Send scheduled review-screenshot pushes for conversion and retention."""
    cfg = await push_settings(db)
    if cfg.get('reviews_visible') != '1' or cfg.get('review_push_enabled') != '1':
        return 0
    if not await db.read('SELECT 1 FROM reviews WHERE active=1 LIMIT 1'):
        return 0

    audience = cfg.get('review_push_audience') or 'cold'
    if audience not in AUDIENCE_LABELS:
        audience = 'cold'
    delay_min = int_setting(cfg, 'review_push_delay_min', 120, minimum=1)
    interval_hours = int_setting(cfg, 'review_push_interval_hours', 24, minimum=0)
    max_per_user = int_setting(cfg, 'review_push_max_per_user', 3, minimum=0)
    order_mode = cfg.get('review_push_order') or 'seq'

    first_cutoff = in_seconds(-delay_min * 60)
    max_age = in_seconds(-delay_min * 60 - max_age_hours * 3600)
    repeat_cutoff = in_seconds(-interval_hours * 3600) if interval_hours > 0 else '1970-01-01T00:00:00+00:00'
    promo_cooldown = in_seconds(-15 * 60)
    audience_where = _audience_sql(audience)
    anchor_expr = (
        "COALESCE((SELECT MAX(COALESCE(o2.paid_at, o2.created_at)) FROM orders o2 "
        "WHERE o2.user_id=u.user_id AND o2.status='paid' AND o2.provider!='admin' "
        "AND o2.refund_requested_at IS NULL), u.first_start_at, u.created_at)"
    )

    sent = 0
    async with _lock_for(db):
        rows = await db.read(
            f"""SELECT u.*
                FROM users u
                WHERE {audience_where}
                  AND COALESCE(u.broadcast_opt_out, 0)=0
                  AND (u.review_push_retry_at IS NULL OR u.review_push_retry_at<=?)
                  AND (u.promo_push_sent_at IS NULL OR u.promo_push_sent_at<=?)
                  AND (? = 0 OR (SELECT COUNT(*) FROM user_review_pushes urp WHERE urp.user_id=u.user_id) < ?)
                  AND (
                      (u.last_review_push_at IS NULL AND {anchor_expr} BETWEEN ? AND ?)
                      OR (u.last_review_push_at IS NOT NULL AND ? > 0 AND u.last_review_push_at <= ?)
                  )
                  AND EXISTS (
                      SELECT 1 FROM reviews r
                      WHERE r.active=1
                        AND NOT EXISTS (
                            SELECT 1 FROM user_review_pushes urp2
                            WHERE urp2.user_id=u.user_id AND urp2.review_id=r.id
                        )
                  )
                ORDER BY COALESCE(u.last_review_push_at, u.created_at) ASC
                LIMIT ?""",
            (now(), promo_cooldown, max_per_user, max_per_user, max_age, first_cutoff,
             interval_hours, repeat_cutoff, limit),
            many=True,
        )
        for user in rows:
            result = await send_review_push_to_user(bot, db, shop, user, cfg, order_mode=order_mode, scheduled=True)
            if result == 'flooded':
                break
            if result == 'sent':
                sent += 1
    return sent


async def dispatch_promo_expiry_reminders(
    bot, db: Database, shop: Shop, *, window_seconds: int = 3600, min_age_seconds: int = 1800, limit: int = 15
) -> int:
    """Send a single last-chance reminder in the final hour before a user's personal promo expires."""
    del shop
    cfg = await push_settings(db)
    if cfg.get('promo_push_enabled') != '1':
        return 0
    project_name = cfg.get('project_name') or DEFAULT_PROJECT_NAME
    now_ts = now()
    exp_cutoff = in_seconds(window_seconds)
    min_sent_at = in_seconds(-min_age_seconds)
    sent = 0
    async with _lock_for(db):
        rows = await db.read(
            """SELECT u.user_id, u.name, u.last_viewed_tariff_id,
                      p.code, p.discount, p.discount_percent, p.expires_at
               FROM users u
               JOIN promos p ON p.code=u.promo_push_code
               WHERE u.promo_push_sent=1
                 AND u.promo_push_code!=''
                 AND COALESCE(u.promo_expiry_reminder_sent, 0)=0
                 AND COALESCE(u.broadcast_opt_out, 0)=0
                 AND u.promo_push_sent_at IS NOT NULL AND u.promo_push_sent_at<=?
                 AND p.is_personal=1 AND p.enabled=1 AND p.used<p.max_uses
                 AND (p.discount>0 OR COALESCE(p.discount_percent, 0)>0)
                 AND p.expires_at IS NOT NULL AND p.expires_at>? AND p.expires_at<=?
                 AND NOT EXISTS (
                     SELECT 1 FROM orders o WHERE o.user_id=u.user_id
                       AND ((o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL)
                            OR o.status='review')
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM promo_redemptions pr WHERE pr.code=p.code AND pr.user_id=u.user_id
                 )
               ORDER BY p.expires_at ASC, u.user_id
               LIMIT ?""",
            (min_sent_at, now_ts, exp_cutoff, limit),
            many=True,
        )
        for row in rows:
            claimed = await db.execute(
                'UPDATE users SET promo_expiry_reminder_sent=1 WHERE user_id=? AND COALESCE(promo_expiry_reminder_sent, 0)=0',
                (row['user_id'],),
            )
            if not claimed:
                continue
            code = row['code']
            d_label = promo_discount_label(row)
            rem = remaining_time_label(row['expires_at']) or '30 минут'
            buttons: list[tuple] = []
            viewed = None
            if row.get('last_viewed_tariff_id'):
                viewed = await db.read(
                    """SELECT id, title, rub, stars FROM tariffs
                       WHERE id=? AND active=1 AND (channel_ids!='' OR delivery_urls!='[]' OR allow_no_channel=1)""",
                    (row['last_viewed_tariff_id'],),
                )
            if viewed:
                v_name = button_tariff_name(viewed['title'], 'тариф', max_len=20)
                v_disc, _ = price_with_promo(
                    viewed['rub'], viewed['stars'],
                    row['discount'], int(row.get('discount_percent') or 0),
                )
                buttons.append((cb(f'⚡ Оформить со скидкой {d_label} · {fmt_rub(v_disc)} ₽', f"claim_pay:{viewed['id']}:{code}", style='success'),))
            else:
                entry = await db.read(
                    """SELECT id, title, rub, stars FROM tariffs
                       WHERE active=1 AND (channel_ids!='' OR delivery_urls!='[]' OR allow_no_channel=1)
                       ORDER BY CASE WHEN duration_days<=3 THEN 0 ELSE 1 END, rub ASC, stars ASC, id ASC
                       LIMIT 1"""
                )
                if entry:
                    e_name = button_tariff_name(entry['title'], 'пропуск', max_len=20)
                    e_disc, _ = price_with_promo(
                        entry['rub'], entry['stars'],
                        row['discount'], int(row.get('discount_percent') or 0),
                    )
                    buttons.append((cb(f'⚡ Оформить со скидкой {d_label} · {fmt_rub(e_disc)} ₽', f"claim_pay:{entry['id']}:{code}", style='success'),))
            buttons.append((cb('📂 Открыть каталог со скидкой', f'claim_promo:{code}'),))
            text = (
                f"⏳ <b>Твоя персональная скидка {d_label} сгорает через {rem}</b>\n"
                "После этого промокод аннулируется, а цены вернутся к базовым.\n"
                "👇 <b>Зафиксировать скидку до сгорания:</b>"
            )
            touch_id, tracked_markup = await prepare_campaign(db, row['user_id'], 'promo_exp', kb(*buttons))
            if await safe_send_push_text(bot, row['user_id'], text, tracked_markup):
                await campaign_delivered(db, touch_id)
                sent += 1
    return sent


async def dispatch_winback_pushes(
    bot,
    db: Database,
    shop: Shop,
    *,
    min_age_hours: int = 28,
    max_age_days: int = 7,
    ttl_hours: int = 12,
    limit: int = 15,
) -> int:
    """Send a single winback push with 30% discount to non-buying cold leads after 28 hours."""
    del shop
    cfg = await push_settings(db)
    if cfg.get('promo_push_enabled') != '1' or (cfg.get('promo_push_mode') or 'shared') != 'personal':
        return 0
    wb_pct = 30
    wb_disc = 0

    project_name = cfg.get('project_name') or DEFAULT_PROJECT_NAME
    now_ts = now()
    min_sent_at = in_seconds(-min_age_hours * 3600)
    max_sent_at = in_seconds(-max_age_days * 86400)
    sent = 0
    async with _lock_for(db):
        rows = await db.read(
            """SELECT u.user_id, u.name, u.last_viewed_tariff_id
               FROM users u
               LEFT JOIN promos p ON p.code=u.promo_push_code
               WHERE u.promo_push_sent=1
                 AND COALESCE(u.winback_push_sent, 0)=0
                 AND COALESCE(u.broadcast_opt_out, 0)=0
                 AND u.promo_push_sent_at IS NOT NULL
                 AND u.promo_push_sent_at BETWEEN ? AND ?
                 AND (
                     p.code IS NULL
                     OR p.enabled=0
                     OR p.used>=p.max_uses
                     OR (p.expires_at IS NOT NULL AND p.expires_at<=?)
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM orders o WHERE o.user_id=u.user_id
                       AND ((o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL)
                            OR o.status='review')
                 )
               ORDER BY u.promo_push_sent_at ASC, u.user_id
               LIMIT ?""",
            (max_sent_at, min_sent_at, now_ts, limit),
            many=True,
        )
        if not rows:
            return 0
        ultra = await db.read(
            """SELECT id, title, rub, stars FROM tariffs
               WHERE active=1 AND (LOWER(tier)='ultra' OR UPPER(title) LIKE '%ВСЁ СРАЗУ%' OR UPPER(title) LIKE '%ВСЕ СРАЗУ%')
               LIMIT 1"""
        )
        entry = await db.read(
            """SELECT id, title, rub, stars FROM tariffs
               WHERE active=1 AND (channel_ids!='' OR delivery_urls!='[]' OR allow_no_channel=1)
               ORDER BY CASE WHEN duration_days<=3 THEN 0 ELSE 1 END, rub ASC, stars ASC, id ASC
               LIMIT 1"""
        )
        for row in rows:
            claimed = await db.execute(
                'UPDATE users SET winback_push_sent=1 WHERE user_id=? AND COALESCE(winback_push_sent, 0)=0',
                (row['user_id'],),
            )
            if not claimed:
                continue
            promo = await create_personal_promo(
                db, row['user_id'], 'WIN', wb_disc, ttl_hours, discount_percent=wb_pct,
            )
            code = promo['code']
            await db.execute(
                'UPDATE users SET promo_push_code=?, promo_expiry_reminder_sent=1 WHERE user_id=?',
                (code, row['user_id']),
            )
            buttons: list[tuple] = []
            if ultra:
                u_disc, _ = price_with_promo(ultra['rub'], ultra['stars'], wb_disc, wb_pct)
                buttons.append((cb(f'🔥 Забрать «ВСЁ СРАЗУ» навсегда за {fmt_rub(u_disc)} ₽', f"claim_pay:{ultra['id']}:{code}", style='success'),))
            if entry:
                e_disc, _ = price_with_promo(entry['rub'], entry['stars'], wb_disc, wb_pct)
                e_name = button_tariff_name(entry['title'], 'пропуск')
                buttons.append((cb(f'⚡ Взять «{e_name}» за {fmt_rub(e_disc)} ₽', f"claim_pay:{entry['id']}:{code}"),))
            buttons.append((cb('📂 Открыть весь каталог со скидкой 30%', f'claim_promo:{code}'),))
            text = (
                f"👋 <b>Давно не заходил в {esc(project_name)}</b>\n"
                "За это время в архивы выгрузили больше 40 новых эксклюзивных роликов.\n"
                "Мы выдали тебе финальный промокод на <b>максимальную скидку -30%</b>. Он действует ровно 12 часов:\n"
                "• Пропуск TikTok (3 дн.): <b>133 ₽</b> вместо 190 ₽\n"
                "• «ВСЁ СРАЗУ» навсегда: <b>693 ₽</b> вместо 990 ₽\n"
                "👇 <b>Активируй в 1 клик, пока действует скидка:</b>"
            )
            touch_id, tracked_markup = await prepare_campaign(db, row['user_id'], 'winback', kb(*buttons))
            if await safe_send_push_text(bot, row['user_id'], text, tracked_markup):
                await campaign_delivered(db, touch_id)
                sent += 1
    return sent
    return sent
