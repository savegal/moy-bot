"""Discovery and delivery for welcome messages on Telegram join requests."""
from __future__ import annotations

import asyncio
import logging
from time import time
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import ChatJoinRequest, ChatMemberUpdated

from .access import resolve_join_request
from .db import Database, in_seconds, now
from .ui import cb, kb, link, rendered_section, send_text

log = logging.getLogger(__name__)
WELCOME_CHAT_TYPES = ('channel', 'supergroup')
WELCOME_CATALOG_BUTTON = '🔞 Открыть каталог фуллов'


async def _welcome_approval_allowed(db: Database, job: dict) -> bool:
    if await db.paid_channel(job['chat_id']):
        return False
    return bool(await db.read("""SELECT 1 FROM welcome_channels c
        JOIN settings s ON s.key='greeting_enabled'
        JOIN greeting_events e ON e.id=? AND e.chat_id=c.chat_id AND e.user_id=?
        WHERE c.chat_id=? AND c.is_admin=1 AND c.can_invite=1 AND c.enabled=1
          AND c.auto_approve=1 AND s.value='1' AND e.sent_at IS NOT NULL""",
        (job['event_id'], job['user_id'], job['chat_id'])))


async def _attempt_welcome_approval(bot: Bot, db: Database, job: dict, request=None):
    identity = (job['chat_id'], job['user_id'], job['event_id'])

    async def remove():
        await db.execute('DELETE FROM welcome_approvals WHERE chat_id=? AND user_id=? AND event_id=?', identity)

    if not await _welcome_approval_allowed(db, job):
        await remove()
        return
    try:
        if request is not None:
            await asyncio.wait_for(resolve_join_request(request, 'approve'), timeout=5)
        else:
            approved = await asyncio.wait_for(bot.approve_chat_join_request(
                job['chat_id'], job['user_id'], request_timeout=5), timeout=5)
            if approved is False:
                raise RuntimeError('Telegram did not confirm join request approval')
    except Exception as exc:
        if isinstance(exc, TelegramBadRequest) and any(code in exc.message.upper() for code in (
            'HIDE_REQUESTER_MISSING', 'USER_ALREADY_PARTICIPANT', 'INPUT_USER_DEACTIVATED',
            'JOIN REQUEST NOT FOUND', 'REQUESTER NOT FOUND',
        )):
            await remove()
            return
        delay = min(1800, 30 * 2 ** min(job['attempts'], 6))
        if isinstance(exc, TelegramRetryAfter):
            delay = max(delay, exc.retry_after)
        await db.execute("""UPDATE welcome_approvals SET attempts=attempts+1,retry_at=?
            WHERE chat_id=? AND user_id=? AND event_id=?""", (in_seconds(delay), *identity))
        log.info('Welcome approval deferred in chat %s: %s', job['chat_id'], type(exc).__name__)
    else:
        await remove()


async def retry_welcome_approvals(bot: Bot, db: Database):
    """Recover saved approvals independently of Telegram update redelivery."""
    jobs = await db.read("""SELECT * FROM welcome_approvals WHERE retry_at<=?
        ORDER BY retry_at,created_at,chat_id,user_id LIMIT 10""", (now(),), many=True)
    for job in jobs:
        await _attempt_welcome_approval(bot, db, job)


async def observe_channel(db: Database, chat_id: int, title: str, *, is_admin: bool,
                          can_invite: bool, timestamp: int, from_request: bool = False):
    def txn(con):
        existing = con.execute('SELECT * FROM welcome_channels WHERE chat_id=?', (chat_id,)).fetchone()
        if existing:
            previous = existing['rights_at']
            if previous is None or timestamp > previous or (not from_request and timestamp == previous):
                con.execute("""UPDATE welcome_channels SET title=?,is_admin=?,can_invite=?,rights_at=?
                    WHERE chat_id=?""", (title, is_admin, can_invite, timestamp, chat_id))
        elif is_admin:
            auto = con.execute("SELECT value FROM settings WHERE key='greeting_auto_new'").fetchone()[0]
            con.execute("""INSERT INTO welcome_channels(chat_id,title,is_admin,can_invite,enabled,rights_at,auto_approve)
                VALUES(?,?,?,?,?,?,0)""", (chat_id, title, is_admin, can_invite, auto == '1', timestamp))
        return dict(con.execute('SELECT * FROM welcome_channels WHERE chat_id=?', (chat_id,)).fetchone() or {})
    return await db.atomic(txn)


async def membership_changed(event: ChatMemberUpdated, db: Database):
    member = event.new_chat_member
    await observe_channel(db, event.chat.id, event.chat.title or '', is_admin=member.status == 'administrator',
                          can_invite=getattr(member, 'can_invite_users', False),
                          timestamp=int(event.date.timestamp()))


async def inspect_channel(db: Database, bot: Bot, chat_id: int):
    chat, member = await asyncio.gather(bot.get_chat(chat_id, request_timeout=5),
                                        bot.get_chat_member(chat_id, bot.id, request_timeout=5))
    if chat.type not in WELCOME_CHAT_TYPES:
        return {}
    return await observe_channel(db, chat_id, chat.title or '', is_admin=member.status == 'administrator',
                                 can_invite=getattr(member, 'can_invite_users', False), timestamp=int(time()))


async def send_welcome(bot: Bot, db: Database, request: ChatJoinRequest, event_id: str):
    bot_user = await bot.me()
    user_name = (getattr(request.from_user, 'full_name', None) or '').strip() or 'друг'
    channel_title = (getattr(request.chat, 'title', None) or '').strip() or 'канал'
    bot_username = (getattr(bot_user, 'username', None) or '').strip()
    row = await rendered_section(
        db,
        'join_welcome',
        name=user_name,
        channel=channel_title,
        bot_username=bot_username,
    )
    text = row['value']
    cta_button = (
        link(WELCOME_CATALOG_BUTTON, f'https://t.me/{bot_username}?start=g_{event_id}', style='success')
        if bot_username else cb(WELCOME_CATALOG_BUTTON, 'catalog', style='success')
    )
    markup = kb((cta_button,))
    method_name = {'photo': 'send_photo', 'video': 'send_video', 'animation': 'send_animation'}.get(row.get('media_type'))
    method = getattr(bot, method_name, None) if method_name else None
    if method and row.get('media_id'):
        caption = text if len(text.encode('utf-16-le')) // 2 <= 950 else None
        try:
            await method(request.user_chat_id, row['media_id'], caption=caption,
                         reply_markup=markup if caption else None, request_timeout=3)
        except TelegramBadRequest:
            pass
        else:
            if caption:
                return
    async def send(content, **kwargs):
        return await bot.send_message(request.user_chat_id, content, **kwargs)
    await send_text(send, text, markup=markup, request_timeout=3)


async def greet_request(bot: Bot, db: Database, request: ChatJoinRequest, *, paid_channel: bool = False):
    chat_id, user_id = request.chat.id, request.from_user.id
    if request.chat.type not in WELCOME_CHAT_TYPES:
        await resolve_join_request(request, 'queue')
        return
    requested_at = int(request.date.timestamp())
    await observe_channel(db, chat_id, request.chat.title or '', is_admin=True, can_invite=True,
                          timestamp=requested_at, from_request=True)
    if not paid_channel and await db.paid_channel(chat_id):
        own_invite = request.invite_link and request.invite_link.creator.id == bot.id
        await resolve_join_request(request, 'decline' if own_invite else 'queue')
        return
    event_id = uuid4().hex
    def claim(con):
        con.execute("""INSERT OR IGNORE INTO greeting_events(id,chat_id,user_id,requested_at)
            VALUES(?,?,?,?)""", (event_id, chat_id, user_id, requested_at))
        event = con.execute("""SELECT id,sent_at FROM greeting_events
            WHERE chat_id=? AND user_id=? AND requested_at=?""", (chat_id, user_id, requested_at)).fetchone()
        if time() - requested_at >= 290:
            return None
        channel = con.execute("""SELECT c.* FROM welcome_channels c JOIN settings s ON s.key='greeting_enabled'
            WHERE c.chat_id=? AND c.is_admin=1 AND c.can_invite=1 AND (c.enabled=1 OR ?) AND s.value='1'""",
            (chat_id, paid_channel)).fetchone()
        if not channel:
            return None
        if event['sent_at']:
            return dict(channel), event['id'], True
        changed = con.execute("""INSERT INTO welcome_requests(chat_id,user_id,requested_at) VALUES(?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET requested_at=excluded.requested_at
            WHERE welcome_requests.requested_at < excluded.requested_at""", (chat_id, user_id, requested_at)).rowcount
        return (dict(channel), event['id'], False) if changed else None

    claimed = await db.atomic(claim)
    if not claimed:
        if not paid_channel:
            await resolve_join_request(request, 'queue')
        return
    channel, event_id, sent = claimed
    if not sent:
        try:
            await asyncio.wait_for(send_welcome(bot, db, request, event_id), timeout=5 if getattr(request, 'query_id', None) else 12)
            timestamp = now()
            def delivered(con):
                con.execute('UPDATE greeting_events SET sent_at=COALESCE(sent_at,?) WHERE id=?',
                            (timestamp, event_id))
                if channel['auto_approve'] and not paid_channel:
                    con.execute("""INSERT OR IGNORE INTO welcome_approvals
                        (chat_id,user_id,event_id,created_at,retry_at,attempts) VALUES(?,?,?,?,?,0)""",
                        (chat_id, user_id, event_id, timestamp, in_seconds(15)))
            await db.atomic(delivered)
            sent = True
        except Exception as exc:
            log.info('Welcome failed in chat %s: %s', chat_id, type(exc).__name__)
            await db.execute('DELETE FROM welcome_requests WHERE chat_id=? AND user_id=? AND requested_at=?',
                             (chat_id, user_id, requested_at))
    if sent and channel['auto_approve'] and not paid_channel and not await db.paid_channel(chat_id):
        current = await db.read("""SELECT 1 FROM welcome_channels c JOIN settings s ON s.key='greeting_enabled'
            WHERE c.chat_id=? AND c.is_admin=1 AND c.can_invite=1 AND c.enabled=1
              AND c.auto_approve=1 AND s.value='1'""", (chat_id,))
        if current:
            # Polling may acknowledge the update before this handler finishes.
            # Persist the action before network I/O so a timeout or restart can
            # be recovered without sending a second welcome message.
            await db.execute("""INSERT OR IGNORE INTO welcome_approvals
                (chat_id,user_id,event_id,created_at,retry_at,attempts) VALUES(?,?,?,?,?,0)""",
                (chat_id, user_id, event_id, now(), in_seconds(15)))
            job = await db.read('SELECT * FROM welcome_approvals WHERE chat_id=? AND user_id=?',
                                (chat_id, user_id))
            if job:
                await _attempt_welcome_approval(bot, db, job, request=request)
            return
    if not paid_channel:
        await resolve_join_request(request, 'queue')
