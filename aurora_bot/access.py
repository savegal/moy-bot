"""Reliable invitation provisioning, join-request authorization and expiry."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import html
import json
import logging
from time import monotonic
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary, ref as weak_ref
if TYPE_CHECKING:
    from aiogram import Bot

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from .alerts import paying_perk_rows, send_expiry_upsell_if_needed
from .db import Database, in_seconds, now
from .service import Shop, ShopError, parse_date
from .ui import is_https_url, kb, link

log = logging.getLogger(__name__)


def invite_expiry(purchase_expiry: str | None) -> datetime:
    hard_limit = datetime.now(timezone.utc) + timedelta(days=6)
    end = parse_date(purchase_expiry)
    return min(hard_limit, end) if end else hard_limit


_CHANNEL_VALIDATION_CACHE: dict[int, tuple[weak_ref, dict[int, float]]] = {}
_CHANNEL_VALIDATION_TTL = 60.0


def _cached_channel_ok(bot: object, chat_id: int, now_mono: float) -> bool:
    entry = _CHANNEL_VALIDATION_CACHE.get(id(bot))
    if entry and entry[0]() is bot:
        return entry[1].get(chat_id, 0.0) > now_mono
    local = getattr(bot, '_aurora_channel_cache', None)
    return isinstance(local, dict) and local.get(chat_id, 0.0) > now_mono


def _mark_channel_ok(bot: object, chat_id: int, now_mono: float) -> None:
    key = id(bot)
    expiry = now_mono + _CHANNEL_VALIDATION_TTL
    entry = _CHANNEL_VALIDATION_CACHE.get(key)
    if entry and entry[0]() is bot:
        entry[1][chat_id] = expiry
        return
    try:
        bot_ref = weak_ref(bot, lambda _dead, k=key: _CHANNEL_VALIDATION_CACHE.pop(k, None))
    except TypeError:
        local = getattr(bot, '_aurora_channel_cache', None)
        if not isinstance(local, dict):
            local = {}
            try:
                setattr(bot, '_aurora_channel_cache', local)
            except AttributeError:
                return
        local[chat_id] = expiry
        return
    _CHANNEL_VALIDATION_CACHE[key] = (bot_ref, {chat_id: expiry})


async def validate_sale_channels(bot: Bot, channels: list[int]):
    if not channels:
        raise ShopError('К тарифу нужно привязать хотя бы один закрытый канал.')
    now_mono = monotonic()
    pending = [cid for cid in channels if not _cached_channel_ok(bot, cid, now_mono)]
    if not pending:
        return
    await _validate_sale_channels_uncached(bot, pending, cache_target=bot)


async def _validate_sale_channels_uncached(bot: Bot, channels: list[int], *, cache_target: object | None = None):
    async def validate(chat_id):
        try:
            chat, membership = await asyncio.gather(
                bot.get_chat(chat_id, request_timeout=6),
                bot.get_chat_member(chat_id, bot.id, request_timeout=6),
            )
        except Exception as exc:
            raise ShopError(f'Не удалось проверить канал {chat_id}. Проверьте ID и доступ бота.') from exc
        if chat.type not in ('channel', 'supergroup') or chat.username or chat.active_usernames:
            raise ShopError(f'{chat_id} должен быть закрытым каналом или закрытой супергруппой.')
        if not (membership.status == 'administrator' and membership.can_invite_users
                and membership.can_restrict_members):
            raise ShopError(f'В канале {chat_id} боту нужны права приглашения и исключения.')

    results = await asyncio.gather(*(validate(chat_id) for chat_id in channels), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            raise result
    done_mono = monotonic()
    target_bot = cache_target or bot
    for chat_id in channels:
        _mark_channel_ok(target_bot, chat_id, done_mono)


async def resolve_join_request(request, result: str):
    query_id = getattr(request, 'query_id', None)
    if query_id:
        await request.bot.answer_chat_join_request_query(query_id, result, request_timeout=3)
    elif result == 'approve':
        await asyncio.wait_for(request.approve(), timeout=8)
    elif result == 'decline':
        await asyncio.wait_for(request.decline(), timeout=8)


class AccessManager:
    def __init__(self, db: Database, shop: Shop):
        self.db, self.shop = db, shop
        self.lock = shop.entitlement_lock
        self.provision_lock = asyncio.Lock()
        self.grant_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    def _grant_lock(self, grant_id: int) -> asyncio.Lock:
        lock = self.grant_locks.get(grant_id)
        if lock is None:
            lock = asyncio.Lock()
            self.grant_locks[grant_id] = lock
        return lock

    async def create_invite(self, bot: Bot, grant: dict, *, force: bool = False) -> bool:
        """Claim a row before network I/O, retry stale claims after restart."""
        def claim(con):
            row = con.execute('''SELECT g.*,p.expires_at FROM grants g JOIN purchases p ON g.purchase_id=p.id
                                JOIN orders o ON o.id=p.order_id
                                WHERE g.id=? AND o.refund_requested_at IS NULL''', (grant['id'],)).fetchone()
            if not row or row['state'] in ('expired','revoked') or (row['state']=='joined' and not force):
                return None
            if row['expires_at'] and parse_date(row['expires_at']) <= datetime.now(timezone.utc):
                return None
            if invite_expiry(row['expires_at']) < datetime.now(timezone.utc) + timedelta(seconds=15):
                return None
            if row['state'] == 'ready' and not force and row['invite_expires_at'] and parse_date(row['invite_expires_at']) > datetime.now(timezone.utc):
                return None
            if row['state'] == 'creating' and row['processing_at'] and parse_date(row['processing_at']) > datetime.now(timezone.utc) - timedelta(minutes=2):
                return None
            con.execute("UPDATE grants SET state='creating', processing_at=? WHERE id=?", (now(),grant['id']))
            return dict(row)
        async with self._grant_lock(grant['id']):
            async with self.lock:
                row = await self.db.atomic(claim)
            if not row:
                return False
            expiry = invite_expiry(row['expires_at'])
            try:
                link = await asyncio.wait_for(bot.create_chat_invite_link(
                    chat_id=row['chat_id'],
                    name=f"Access {row['purchase_id']}-{row['user_id']}"[:32],
                    expire_date=expiry, creates_join_request=True), timeout=10)
                old = row['invite_url']
                def save_ready(con):
                    current_ts = now()
                    return con.execute(
                        '''UPDATE grants SET invite_url=?,invite_expires_at=?,state='ready',
                                             last_error='',processing_at=NULL
                           WHERE id=? AND state='creating'
                             AND EXISTS (SELECT 1 FROM purchases p JOIN orders o ON o.id=p.order_id
                                         WHERE p.id=grants.purchase_id
                                           AND (p.expires_at IS NULL OR p.expires_at>?)
                                           AND o.refund_requested_at IS NULL)''',
                        (link.invite_link, expiry.isoformat(timespec='seconds'), row['id'], current_ts),
                    ).rowcount
                async with self.lock:
                    saved = await self.db.atomic(save_ready)
                if not saved:
                    try:
                        await asyncio.wait_for(bot.revoke_chat_invite_link(row['chat_id'], link.invite_link), timeout=10)
                    except Exception:
                        log.info('Superseded invite cleanup failed for grant %s', row['id'])
                    return False
                if old and old != link.invite_link:
                    try:
                        await asyncio.wait_for(bot.revoke_chat_invite_link(row['chat_id'], old), timeout=10)
                    except Exception:
                        log.info('Old invite already revoked for grant %s', row['id'])
                return True
            except Exception as exc:
                log.warning('Invite failed for grant %s: %s', row['id'], exc)
                # A failed refresh must not invalidate the still-working previous link.
                state = row['state'] if (row['state'] in ('ready', 'joined') and row['invite_url']
                    and row['invite_expires_at'] and row['invite_expires_at'] > now()) else 'error'
                await self.db.execute("UPDATE grants SET state=?,last_error=?,processing_at=? WHERE id=? AND state='creating'",
                                      (state,str(exc)[:300],now(),row['id']))
                return False

    async def provision(self, bot: Bot, *, limit: int = 25):
        async with self.provision_lock:
            grants = await self.db.read("""SELECT g.* FROM grants g JOIN purchases p ON g.purchase_id=p.id
                JOIN orders o ON o.id=p.order_id
                WHERE NOT EXISTS (SELECT 1 FROM product_channel_links l WHERE l.chat_id=g.chat_id)
                  AND (g.state='need_link' OR (g.state='ready' AND
                       (g.invite_expires_at IS NULL OR g.invite_expires_at<=?)) OR (g.state IN ('error','creating') AND
                       (g.processing_at IS NULL OR g.processing_at<?)))
                  AND (p.expires_at IS NULL OR p.expires_at>?) AND o.refund_requested_at IS NULL
                ORDER BY COALESCE(g.processing_at, ''), g.id LIMIT ?""",
                (now(), in_seconds(-120), in_seconds(15), limit), many=True)
            for grant in grants:
                await self.create_invite(bot, grant)

            eligible = """FROM orders o JOIN purchases p ON p.order_id=o.id
                WHERE o.status='paid' AND o.refund_requested_at IS NULL AND o.notice_sent=0
                  AND (o.notice_retry_at IS NULL OR o.notice_retry_at<=?)
                  AND (p.expires_at IS NULL OR p.expires_at>?)
                  AND (o.channels='[]' OR EXISTS (SELECT 1 FROM grants g WHERE g.purchase_id=p.id))
                  AND NOT EXISTS (SELECT 1 FROM grants g WHERE g.purchase_id=p.id AND
                    (g.state IN ('expired','revoked') OR (
                      NOT EXISTS (SELECT 1 FROM product_channel_links l WHERE l.chat_id=g.chat_id)
                      AND (g.state NOT IN ('ready','joined') OR (g.state='ready' AND
                           (g.invite_expires_at IS NULL OR g.invite_expires_at<=?))))))"""
            candidates = await self.db.read(f"""SELECT o.id {eligible}
                ORDER BY COALESCE(o.notice_retry_at,o.paid_at),o.id LIMIT ?""",
                (now(), now(), now(), limit), many=True)
            for candidate in candidates:
                async with self.shop.delivery_lock:
                    order = await self.db.read(f"""SELECT o.*,p.expires_at AS access_expires_at
                        {eligible} AND o.id=?""", (now(), now(), now(), candidate['id']))
                    if order:
                        await self.send_notice(bot, order)

    async def send_notice(self, bot: Bot, order: dict):
        term_line = (f"🕘 Действует до: <b>{order['access_expires_at'][:10]}</b>"
                     if order['access_expires_at'] else '🕘 Срок доступа: <b>НАВСЕГДА</b>')
        heading = 'Оплата подтверждена! Доступ готов' if order['provider'] != 'admin' else 'Доступ открыт'
        try:
            urls = json.loads(order['delivery_urls'])
            text = (f"✅ <b>{heading}</b>\n\n{html.escape(order['title'])}\n"
                    f"{term_line}\n\n"
                    "Открыть доступ можно в разделе «🔓 Мои доступы».")
            rows = [(link(f'🔗 Открыть канал {index}', url, style='success'),)
                    for index, url in enumerate(urls, 1)]
            grant_rows = await self.db.read(
                """SELECT COALESCE(l.url, g.invite_url) AS invite_url FROM grants g
                   JOIN purchases p ON p.id=g.purchase_id
                   LEFT JOIN product_channel_links l ON l.chat_id=g.chat_id
                   WHERE p.order_id=? AND g.state NOT IN ('expired','revoked')
                     AND (l.url IS NOT NULL OR (g.state IN ('ready','joined') AND g.invite_url!=''))
                   ORDER BY g.id""",
                (order['id'],), many=True,
            )
            rows.extend((link(f'🔗 Войти в канал {index}', row['invite_url'], style='success'),)
                    for index, row in enumerate(grant_rows, len(rows) + 1) if is_https_url(row['invite_url']))
            if not urls and order['channels'] == '[]':
                text = f"✅ <b>{'Покупка подтверждена' if order['provider'] != 'admin' else 'Доступ открыт'}</b>\n\n{html.escape(order['title'])}"
                text += f"\n{term_line}\n\nТовар добавлен в «🧾 Мои покупки»."
            if order['provider'] != 'admin':
                rows.extend(await paying_perk_rows(self.shop, self.db, order['user_id']))
            if rows:
                try:
                    await asyncio.wait_for(bot.send_message(order['user_id'], text, reply_markup=kb(*rows)), timeout=10)
                except TypeError:
                    await asyncio.wait_for(bot.send_message(order['user_id'], text), timeout=10)
            else:
                await asyncio.wait_for(bot.send_message(order['user_id'], text), timeout=10)
        except Exception as exc:
            delay = max(300, exc.retry_after) if isinstance(exc, TelegramRetryAfter) else 300
            if isinstance(exc, TelegramForbiddenError):
                delay = 86400
            await self.db.execute('UPDATE orders SET notice_retry_at=? WHERE id=?',
                                  (in_seconds(delay), order['id']))
            log.warning('Delivery for order %s deferred: %s', order['id'], type(exc).__name__)
        else:
            await self.db.execute('UPDATE orders SET notice_sent=1,notice_retry_at=NULL WHERE id=?', (order['id'],))

    async def refresh_user(self, bot: Bot, user_id: int, grant_id: int) -> bool:
        grant = await self.db.read('SELECT g.*,p.expires_at FROM grants g JOIN purchases p ON p.id=g.purchase_id WHERE g.id=? AND g.user_id=?',
                                   (grant_id,user_id))
        if not grant or grant['state'] in ('revoked','expired'):
            return False
        if grant['expires_at'] and parse_date(grant['expires_at']) <= datetime.now(timezone.utc):
            return False
        return await self.create_invite(bot, grant, force=True)

    async def on_join_request(self, request, *, greet=None) -> str:
        """Authorize a join by active access to the requested chat."""
        chat_id, user_id = request.chat.id, request.from_user.id
        async with self.lock:
            grant = await self.db.read("""SELECT g.id FROM grants g
                JOIN purchases p ON p.id=g.purchase_id
                JOIN orders o ON o.id=p.order_id
                WHERE g.chat_id=? AND g.user_id=? AND g.state NOT IN ('expired','revoked')
                  AND p.revoked_by_admin=0 AND (p.expires_at IS NULL OR p.expires_at>?)
                  AND o.status='paid' AND o.refund_requested_at IS NULL
                ORDER BY g.id DESC LIMIT 1""", (chat_id, user_id, now()))
            if grant:
                # Keep approval serialized with refunds and access revocation.
                await resolve_join_request(request, 'approve')
                await self.db.execute(
                    "UPDATE grants SET state='joined',joined_at=COALESCE(joined_at, ?) WHERE id=?",
                    (now(), grant['id']),
                )
                return 'approved'
        if not await self.db.paid_channel(chat_id):
            return 'unhandled'
        try:
            if greet:
                await greet()
        finally:
            # Closing the request ends Telegram's temporary messaging window.
            await resolve_join_request(request, 'decline')
        return 'denied'

    async def revoke_expired(self, bot: Bot, *, limit: int = 50):
        """Remove expired members if no other active entitlement covers same channel."""
        grants = await self.db.read("""SELECT g.id FROM grants g JOIN purchases p ON p.id=g.purchase_id
            WHERE p.expires_at<=? AND g.state NOT IN ('expired','revoked')
              AND (g.revoke_retry_at IS NULL OR g.revoke_retry_at<=?)
            ORDER BY COALESCE(g.revoke_retry_at,p.expires_at),g.id LIMIT ?""", (now(), now(), limit), many=True)
        for grant in grants:
            await self.revoke_grant(bot, grant['id'])

    async def revoke_grant(self, bot: Bot, grant_id: int):
        async with self._grant_lock(grant_id):
            async with self.lock:
                current_ts = now()
                grant = await self.db.read("""SELECT g.*,p.expires_at FROM grants g JOIN purchases p ON p.id=g.purchase_id
                    WHERE g.id=? AND p.expires_at<=?
                      AND g.state NOT IN ('expired','revoked')""", (grant_id, current_ts))
                if not grant:
                    return
                other = await self.db.read("""SELECT 1 FROM grants g JOIN purchases p ON p.id=g.purchase_id
                    JOIN orders o ON o.id=p.order_id
                    WHERE g.user_id=? AND g.chat_id=? AND g.id!=?
                      AND p.revoked_by_admin=0 AND o.status='paid' AND o.refund_requested_at IS NULL
                      AND g.state NOT IN ('expired','revoked') AND (p.expires_at IS NULL OR p.expires_at>?) LIMIT 1""",
                    (grant['user_id'], grant['chat_id'], grant_id, current_ts))
            chat_id, user_id = grant['chat_id'], grant['user_id']
            try:
                if not other:
                    member = await asyncio.wait_for(bot.get_chat_member(chat_id, user_id), timeout=6)
                    if member.status in ('administrator', 'creator'):
                        raise ShopError('Участник имеет права администратора или владельца канала. '
                                        'Сначала снимите эту роль вручную, затем повторите отзыв.')
                    if member.status == 'member' or (member.status == 'restricted' and member.is_member):
                        # A temporary ban removes the member without lifting an existing moderator ban.
                        until = datetime.now(timezone.utc) + timedelta(seconds=60)
                        await asyncio.wait_for(bot.ban_chat_member(chat_id, user_id, until_date=until), timeout=10)
            except Exception as exc:
                delay = max(120, exc.retry_after) if isinstance(exc, TelegramRetryAfter) else 120
                await self.db.execute('UPDATE grants SET revoke_retry_at=?,last_error=? WHERE id=?',
                                      (in_seconds(delay),str(exc)[:300],grant_id))
                log.warning('Member removal for grant %s failed: %s', grant_id, type(exc).__name__)
                return
            if grant['invite_url']:
                try:
                    await asyncio.wait_for(bot.revoke_chat_invite_link(chat_id, grant['invite_url']), timeout=10)
                except TelegramBadRequest:
                    # An expired or already revoked link can no longer be used.
                    pass
                except Exception:
                    log.warning('Could not revoke invite for grant %s; join requests remain denied', grant_id)
            async with self.lock:
                finalized = await self.db.execute(
                    """UPDATE grants SET state='expired',revoke_retry_at=NULL,unban_pending=0,last_error=''
                       WHERE id=? AND state NOT IN ('expired','revoked')
                         AND EXISTS (SELECT 1 FROM purchases p WHERE p.id=grants.purchase_id
                                     AND p.expires_at IS NOT NULL AND p.expires_at<=?)""",
                    (grant_id, now()),
                )
            if finalized:
                await send_expiry_upsell_if_needed(bot, self.db, self.shop, grant['purchase_id'])
