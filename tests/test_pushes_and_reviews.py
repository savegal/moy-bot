"""Tests for configurable promo-code pushes and review-screenshot retention pushes."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, SendPhoto

from aurora_bot import handlers_admin as admin
from aurora_bot import handlers_user as public
from aurora_bot.config import Config
from aurora_bot.db import DEFAULT_SETTINGS, Database, in_seconds
from aurora_bot.pushes import (
    create_personal_promo, dispatch_promo_pushes, dispatch_review_pushes,
    push_settings, safe_send_media, send_promo_push_to_user,
)
from aurora_bot.service import Shop, ShopError
from aurora_bot.ui import validate_section_html


class PromoAndReviewPushTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'pushes.sqlite3')
        await self.db.init()
        await self.db.set_setting('reviews_visible', '1')
        self.shop = Shop(self.db)
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id IN (1, 2, 5)")
        await self.db.register(101, 'alice', 'Alice <VIP>', via_start=True)
        await self.db.register(202, 'bob', 'Bob', via_start=True)
        await self.db.register(303, 'charlie', 'Charlie', via_start=True)
        self.storage = MemoryStorage()
        self.addAsyncCleanup(self.storage.close)
        self.state = FSMContext(self.storage, StorageKey(bot_id=123, chat_id=99, user_id=99))

    def admin_msg(self, text=None, *, photo=None, caption=None):
        return NS(
            text=text,
            caption=caption,
            entities=None,
            caption_entities=None,
            html_text=caption or text or '',
            photo=photo,
            video=None,
            animation=None,
            from_user=NS(id=99, full_name='Admin'),
            chat=NS(id=99),
            message_id=10,
            answer=AsyncMock(),
        )

    def admin_call(self, data, bot=None):
        return NS(
            data=data,
            from_user=NS(id=99, full_name='Admin'),
            bot=bot or NS(send_message=AsyncMock(), send_photo=AsyncMock()),
            message=self.admin_msg(),
            answer=AsyncMock(),
        )

    async def test_default_push_templates_valid_html(self):
        validate_section_html(DEFAULT_SETTINGS['promo_push_text'])
        validate_section_html(DEFAULT_SETTINGS['review_push_text'])

    async def test_promo_push_shared_mode_timing_eligibility_and_one_click_claim(self):
        bot = NS(send_message=AsyncMock())
        # Disabled by default
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id=101', (in_seconds(-40 * 60),))
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 0)

        # Enabled, but selected promo LUXURYNEW is disabled -> does not send or consume flag
        await self.db.set_setting('promo_push_enabled', '1')
        await self.db.set_setting('promo_push_mode', 'shared')
        await self.db.set_setting('promo_push_code', 'LUXURYNEW')
        await self.db.set_setting('promo_push_delay_min', '30')
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 0)
        self.assertEqual((await self.db.read('SELECT promo_push_sent FROM users WHERE user_id=101'))['promo_push_sent'], 0)

        # Bob is already a buyer; Charlie started only 10m ago (<30m delay)
        ord_bob = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(ord_bob['id'], 'stars', charge_id='chg-bob-1')
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id IN (202, 303)', (in_seconds(-40 * 60),))
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id=303', (in_seconds(-10 * 60),))

        # Enable LUXURYNEW -> only Alice (101) receives the push
        await self.db.execute("UPDATE promos SET enabled=1 WHERE code='LUXURYNEW'")
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 1)
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 0)

        sent_uid, sent_text = bot.send_message.await_args.args[:2]
        sent_kb = bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(sent_uid, 101)
        self.assertIn('Alice &lt;VIP&gt;', sent_text)
        self.assertIn('LUXURYNEW', sent_text)
        self.assertRegex(sent_kb[0][0].callback_data, r'^d:[0-9]+:claim_promo:LUXURYNEW$')

        # One-click claim activates LUXURYNEW and opens catalog
        claim_call = NS(
            data='claim_promo:LUXURYNEW',
            from_user=NS(id=101, username='alice', full_name='Alice'),
            message=NS(),
            answer=AsyncMock(),
        )
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as send_section:
            await public.claim_promo_one_click(claim_call, self.shop, self.db)
        buttons = [button for row in send_section.await_args.args[2].inline_keyboard for button in row]
        self.assertTrue(any('₽' in button.text for button in buttons))
        self.assertIn('LUXURYNEW', send_section.await_args.args[1]['value'])
        self.assertEqual((await self.shop.profile(101))['active_promo'], 'LUXURYNEW')

    async def test_promo_delay_uses_first_start_and_supports_more_than_two_days(self):
        await self.db.set_setting('promo_push_enabled', '1')
        await self.db.set_setting('promo_push_mode', 'personal')
        await self.db.set_setting('promo_push_delay_min', '4320')
        await self.db.register(404, name='Never started')
        await self.db.execute('UPDATE users SET created_at=? WHERE user_id IN (101, 404)',
                              (in_seconds(-4 * 86400),))
        bot = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 0)

        first_start = in_seconds(-3 * 86400 - 120)
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id=101', (first_start,))
        message = NS(text='/start', from_user=NS(id=101, username='alice', full_name='Alice'),
                     answer=AsyncMock(), answer_photo=AsyncMock(),
                     answer_video=AsyncMock(), answer_animation=AsyncMock())
        with patch.object(public, 'send_welcome_message', new_callable=AsyncMock):
            await public.welcome(message, self.db, self.shop, Config('123:mock', frozenset()), self.state)
        self.assertEqual((await self.shop.profile(101))['first_start_at'], first_start)
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 1)
        self.assertEqual([call.args[0] for call in bot.send_message.await_args_list], [101])

    async def test_promo_push_personal_mode_binds_owner_and_ttl(self):
        bot = NS(send_message=AsyncMock())
        await self.db.set_setting('promo_push_enabled', '1')
        await self.db.set_setting('promo_push_mode', 'personal')
        await self.db.set_setting('promo_push_prefix', 'PERS')
        await self.db.set_setting('promo_push_discount', '150')
        await self.db.set_setting('promo_push_ttl_hours', '24')
        await self.db.set_setting('promo_push_delay_min', '15')
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id=101', (in_seconds(-20 * 60),))

        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 1)
        user_row = await self.shop.profile(101)
        code = user_row['promo_push_code']
        self.assertTrue(code.startswith('PERS_'))

        promo = await self.db.read('SELECT * FROM promos WHERE code=?', (code,))
        self.assertEqual((promo['owner_user_id'], promo['discount'], promo['max_uses'], promo['is_personal']), (101, 150, 1, 1))
        self.assertIsNotNone(promo['expires_at'])

        # Another user cannot activate Alice's personal promo code
        with self.assertRaises(ShopError):
            await self.shop.activate_promo(202, code)

        # Alice can activate and create discounted order
        await self.shop.activate_promo(101, code)
        order = await self.shop.create_order(101, 1, 'stars')
        self.assertEqual(order['promo_code'], code)
        self.assertEqual(order['amount_rub'], 40)

        # Expired personal promo cannot be activated
        await self.db.execute('UPDATE promos SET expires_at=? WHERE code=?', (in_seconds(-60), code))
        with self.assertRaises(ShopError):
            await self.shop.activate_promo(101, code)

    async def test_personal_promo_cannot_back_two_payable_invoices(self):
        promo = await create_personal_promo(self.db, 101, 'VIP', 150, 1)
        await self.shop.activate_promo(101, promo['code'])
        external = await self.shop.create_order(101, 1, 'cryptopay')
        self.assertEqual(external['promo_code'], promo['code'])
        with self.assertRaises(ShopError):
            await self.shop.create_order(101, 2, '2328')

        stars = await self.shop.create_order(101, 2, 'stars')
        self.assertFalse(await self.shop.authorize_stars(stars['id'], 101, stars['amount_stars'], 'blocked'))
        await self.db.execute('UPDATE orders SET expires_at=? WHERE id=?', (in_seconds(-60), external['id']))
        self.assertTrue(await self.shop.authorize_stars(stars['id'], 101, stars['amount_stars'], 'allowed'))
        with self.assertRaises(ShopError):
            await self.shop.create_order(101, 2, '2328')

    async def test_review_screenshots_admin_upload_auto_retention_and_dedup(self):
        # Admin uploads 2 screenshots (one with custom caption, one using default template)
        await self.state.set_state(admin.AdminState.review_add)
        await admin.review_add_receive(
            self.admin_msg(photo=[NS(file_id='rev-pic-1')], caption='🔥 Всё чётко, {name}!'),
            self.db,
        )
        await admin.review_add_receive(
            self.admin_msg(photo=[NS(file_id='rev-pic-2')]),
            self.db,
        )
        reviews = await self.db.read('SELECT * FROM reviews ORDER BY id', many=True)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0]['caption'], '🔥 Всё чётко, {name}!')
        self.assertEqual(reviews[1]['caption'], '')

        # Configure auto review pushes for cold users: delay 30m, interval 12h, max 2
        await self.db.set_setting('review_push_enabled', '1')
        await self.db.set_setting('review_push_audience', 'cold')
        await self.db.set_setting('review_push_delay_min', '30')
        await self.db.set_setting('review_push_interval_hours', '12')
        await self.db.set_setting('review_push_max_per_user', '2')
        await self.db.set_setting('reviews_url', 'https://t.me/aurora_reviews')

        # Bob is a buyer (excluded in 'cold' audience); Alice started 45m ago
        ord_bob = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(ord_bob['id'], 'stars', charge_id='chg-bob-rev')
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id IN (101, 202)', (in_seconds(-45 * 60),))

        bot = NS(send_photo=AsyncMock(), send_message=AsyncMock())
        await self.db.set_setting('reviews_visible', '0')
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 0)
        bot.send_photo.assert_not_awaited()
        await self.db.set_setting('reviews_visible', '1')
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 1)
        self.assertEqual(bot.send_photo.await_count, 1)
        first_call = bot.send_photo.await_args
        self.assertEqual(first_call.args[:2], (101, 'rev-pic-1'))
        self.assertIn('Alice &lt;VIP&gt;', first_call.kwargs['caption'])
        btn_urls = [b.url for r in first_call.kwargs['reply_markup'].inline_keyboard for b in r if b.url]
        self.assertIn('https://t.me/aurora_reviews', btn_urls)

        # Immediate re-run respects 12h interval -> 0 sent
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 0)

        # Age last_review_push_at by 13h -> second screenshot arrives, never repeating #1
        await self.db.execute('UPDATE users SET last_review_push_at=? WHERE user_id=101', (in_seconds(-13 * 3600),))
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 1)
        self.assertEqual(bot.send_photo.await_args.args[:2], (101, 'rev-pic-2'))

        # Now switch audience to 'buyers' -> Bob gets retention push with buyer CTA
        await self.db.set_setting('review_push_audience', 'buyers')
        await self.db.execute("UPDATE orders SET paid_at=? WHERE id=?", (in_seconds(-40 * 60), ord_bob['id']))
        bot.send_photo.reset_mock()
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 1)
        self.assertEqual(bot.send_photo.await_args.args[0], 202)
        buyer_btn_texts = [b.text for r in bot.send_photo.await_args.kwargs['reply_markup'].inline_keyboard for b in r]
        self.assertTrue(any('апгрейд' in t.lower() for t in buyer_btn_texts))

    async def test_user_inbot_reviews_fallback_and_admin_manual_blasts(self):
        # Add 2 screenshots with no reviews_url -> user clicking contact:reviews views them in-bot
        await self.db.execute(
            "INSERT INTO reviews(media_id, media_type, caption, active, created_at) VALUES('p1','photo','Отзыв 1',1,?)",
            (in_seconds(-60),),
        )
        await self.db.execute(
            "INSERT INTO reviews(media_id, media_type, caption, active, created_at) VALUES('p2','photo','Отзыв 2',1,?)",
            (in_seconds(-30),),
        )
        bot = NS(send_photo=AsyncMock(), send_message=AsyncMock())
        u_call = NS(
            data='contact:reviews',
            from_user=NS(id=101, full_name='Alice'),
            bot=bot,
            message=NS(answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await public.reviews_fallback(u_call, self.db, self.shop)
        self.assertEqual(bot.send_photo.await_args.args[:2], (101, 'p1'))
        next_cb = bot.send_photo.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
        self.assertEqual(next_cb, 'review_view:1')

        # Manual promo blast sends to all non-buying cold users regardless of age
        await self.db.execute("UPDATE promos SET enabled=1 WHERE code='LUXURYNEW'")
        await self.db.set_setting('promo_push_mode', 'shared')
        await self.db.set_setting('promo_push_code', 'LUXURYNEW')
        ord_bob = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(ord_bob['id'], 'stars', charge_id='chg-bob-blast')

        blast_bot = NS(send_message=AsyncMock(), send_photo=AsyncMock())
        progress = NS(edit_text=AsyncMock())
        go_call = NS(
            data='adm:ppush:blastgo',
            from_user=NS(id=99, full_name='Admin'),
            message=NS(answer=AsyncMock(return_value=progress)),
            answer=AsyncMock(),
        )
        admin._ACTIVE_BROADCASTS.clear()
        await admin.promo_push_blast_go(go_call, self.db, blast_bot)
        if 99 in admin._ACTIVE_BROADCASTS:
            await asyncio.gather(*(t for t, _ in list(admin._ACTIVE_BROADCASTS.values())))
        sent_uids = [c.args[0] for c in blast_bot.send_message.await_args_list]
        self.assertEqual(sent_uids, [101, 303])

        # Manual review blast skips Bob's already-seen reviews if targeted
        r_go_call = NS(
            data='adm:rpush:blastgo:1:cold',
            from_user=NS(id=99, full_name='Admin'),
            message=NS(answer=AsyncMock(return_value=progress)),
            answer=AsyncMock(),
        )
        await admin.review_blast_go(r_go_call, self.db, self.shop, blast_bot)
        if 99 in admin._ACTIVE_BROADCASTS:
            await asyncio.gather(*(t for t, _ in list(admin._ACTIVE_BROADCASTS.values())))
        self.assertEqual([c.args[0] for c in blast_bot.send_photo.await_args_list], [101, 303])

    async def test_v22_to_current_migration(self):
        with TemporaryDirectory() as legacy_dir:
            db_path = Path(legacy_dir) / 'v22.sqlite3'
            con = sqlite3.connect(db_path)
            con.executescript(f"""
                CREATE TABLE users(
                    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, last_seen TEXT NOT NULL,
                    active_promo TEXT NOT NULL DEFAULT '', last_viewed_tariff_id INTEGER,
                    last_viewed_at TEXT, view_reminder_sent INTEGER NOT NULL DEFAULT 0,
                    broadcast_opt_in INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE promos(
                    code PRIMARY KEY, discount INTEGER NOT NULL,
                    max_uses INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE tariffs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', rub INTEGER NOT NULL,
                    stars INTEGER NOT NULL, duration_days INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1, channel_ids TEXT NOT NULL DEFAULT '',
                    media_id TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT '',
                    preview_description TEXT NOT NULL DEFAULT '',
                    preview_media_id TEXT NOT NULL DEFAULT '', preview_media_type TEXT NOT NULL DEFAULT '',
                    delivery_urls TEXT NOT NULL DEFAULT '[]', tier TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO tariffs(title, rub, stars, duration_days) VALUES('Existing Tariff', 500, 250, 0);
                INSERT INTO users(user_id, username, name, created_at, last_seen)
                    VALUES(101, 'u', 'U', '{in_seconds(-3600)}', '{in_seconds(-3600)}');
                INSERT INTO promos(code, discount, max_uses, enabled) VALUES('OLD100', 100, 10, 1);
                PRAGMA user_version=22;
            """)
            con.commit()
            con.close()

            migrated = Database(db_path)
            await migrated.init()
            self.assertEqual((await migrated.read('PRAGMA user_version'))['user_version'], 30)
            user_cols = {r['name'] for r in await migrated.read('PRAGMA table_info(users)', many=True)}
            promo_cols = {r['name'] for r in await migrated.read('PRAGMA table_info(promos)', many=True)}
            self.assertTrue({'promo_push_sent', 'promo_push_code', 'promo_push_sent_at', 'last_review_push_at'} <= user_cols)
            user = await migrated.read('SELECT * FROM users WHERE user_id=101')
            self.assertEqual(user['first_start_at'], user['created_at'])
            self.assertIsNone(user['promo_push_retry_at'])
            self.assertIsNone(user['review_push_retry_at'])
            self.assertTrue({'owner_user_id', 'expires_at', 'is_personal', 'discount_percent'} <= promo_cols)
            self.assertEqual((await migrated.setting('promo_push_enabled'))['value'], '0')
            self.assertEqual((await migrated.setting('review_push_enabled'))['value'], '0')
            self.assertIsNotNone(await migrated.read("SELECT name FROM sqlite_master WHERE type='table' AND name='reviews'"))
            self.assertIsNotNone(await migrated.read("SELECT name FROM sqlite_master WHERE type='table' AND name='user_review_pushes'"))

    async def test_admin_ui_screens_edits_and_promo_review_cooldown(self):
        # 1) Promo push admin flow: screen -> mode switch -> edit prefix/discount/delay/text -> toggle -> test
        await self.db.set_setting('promo_push_mode', 'shared')
        with patch.object(admin, 'screen', new_callable=AsyncMock) as scr:
            await admin.promo_push_home(self.admin_call('adm:ppush'), self.db)
        self.assertIn('Авто-пуш промокодом', scr.await_args.args[1])

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.promo_push_mode_switch(self.admin_call('adm:ppush:mode'), self.db)
        self.assertEqual((await self.db.setting('promo_push_mode'))['value'], 'personal')

        await self.state.set_state(admin.AdminState.promo_push_field)
        await self.state.update_data(ppush_field='prefix')
        await admin.promo_push_edit_save(self.admin_msg('hot'), self.state, self.db)
        self.assertEqual((await self.db.setting('promo_push_prefix'))['value'], 'HOT')

        await self.state.set_state(admin.AdminState.promo_push_field)
        await self.state.update_data(ppush_field='discount')
        await admin.promo_push_edit_save(self.admin_msg('250'), self.state, self.db)
        self.assertEqual((await self.db.setting('promo_push_discount'))['value'], '250')

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.promo_push_set_delay(self.admin_call('adm:ppush:delay:45'), self.db)
        self.assertEqual((await self.db.setting('promo_push_delay_min'))['value'], '45')

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.promo_push_toggle(self.admin_call('adm:ppush:toggle'), self.db)
        self.assertEqual((await self.db.setting('promo_push_enabled'))['value'], '1')

        test_call = self.admin_call('adm:ppush:test')
        await admin.promo_push_test(test_call, self.db)
        self.assertIn('HOT_DEMO99', test_call.bot.send_message.await_args.args[1])

        # 2) Review push admin flow: upload -> edit caption -> cycle audience/order -> toggle -> delete
        await self.state.set_state(admin.AdminState.review_add)
        await admin.review_add_receive(self.admin_msg(photo=[NS(file_id='r-ui-1')]), self.db)
        rid = (await self.db.read('SELECT id FROM reviews LIMIT 1'))['id']

        await self.state.set_state(admin.AdminState.review_caption)
        await self.state.update_data(review_id=rid)
        await admin.review_caption_save(self.admin_msg('<b>Топ отзыв</b> для {name}'), self.state, self.db)
        self.assertEqual((await self.db.read('SELECT caption FROM reviews WHERE id=?', (rid,)))['caption'], '<b>Топ отзыв</b> для {name}')

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.review_push_cycle_audience(self.admin_call('adm:rpush:aud'), self.db)
            self.assertEqual((await self.db.setting('review_push_audience'))['value'], 'buyers')
            await admin.review_push_cycle_audience(self.admin_call('adm:rpush:aud'), self.db)
            self.assertEqual((await self.db.setting('review_push_audience'))['value'], 'all')
            await admin.review_push_cycle_order(self.admin_call('adm:rpush:order'), self.db)
            self.assertEqual((await self.db.setting('review_push_order'))['value'], 'random')
            await admin.review_push_toggle(self.admin_call('adm:rpush:toggle'), self.db)
            self.assertEqual((await self.db.setting('review_push_enabled'))['value'], '1')

        # 3) Cooldown: user who received promo push 5m ago does not receive review push in the same wave
        await self.db.execute(
            'UPDATE users SET first_start_at=?, promo_push_sent=1, promo_push_sent_at=? WHERE user_id=101',
            (in_seconds(-3 * 3600), in_seconds(-5 * 60)),
        )
        await self.db.set_setting('review_push_audience', 'cold')
        await self.db.set_setting('review_push_delay_min', '30')
        bot = NS(send_photo=AsyncMock(), send_message=AsyncMock())
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 0)
        await self.db.execute('UPDATE users SET promo_push_sent_at=? WHERE user_id=101', (in_seconds(-20 * 60),))
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 1)

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.review_delete_execute(self.admin_call(f'adm:rpush:rdelok:{rid}'), self.db)
        self.assertIsNone(await self.db.read('SELECT 1 FROM reviews WHERE id=?', (rid,)))

    async def test_shared_promo_exhaustion_does_not_burn_flag(self):
        await self.db.execute("UPDATE promos SET enabled=1, max_uses=1 WHERE code='LUXURYNEW'")
        await self.shop.activate_promo(202, 'LUXURYNEW')
        ord_bob = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(ord_bob['id'], 'stars', charge_id='chg-exhaust-1')

        await self.db.set_setting('promo_push_enabled', '1')
        await self.db.set_setting('promo_push_mode', 'shared')
        await self.db.set_setting('promo_push_code', 'LUXURYNEW')
        await self.db.set_setting('promo_push_delay_min', '30')
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id=101', (in_seconds(-45 * 60),))

        bot = NS(send_message=AsyncMock())
        user_101 = await self.db.read('SELECT * FROM users WHERE user_id=?', (101,))
        cfg = await push_settings(self.db)
        self.assertEqual(await send_promo_push_to_user(bot, self.db, user_101, cfg), 'skipped')
        self.assertEqual((await self.db.read('SELECT promo_push_sent FROM users WHERE user_id=?', (101,)))['promo_push_sent'], 0)
        bot.send_message.assert_not_called()

        # Switching to personal mode can still reach user 101 because flag wasn't burned
        await self.db.set_setting('promo_push_mode', 'personal')
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 1)
        self.assertEqual((await self.db.read('SELECT promo_push_sent FROM users WHERE user_id=?', (101,)))['promo_push_sent'], 1)

    async def test_flood_wait_retry_for_text_and_media_pushes(self):
        bot = NS(
            send_message=AsyncMock(side_effect=[
                TelegramRetryAfter(method=SendMessage(chat_id=101, text='x'), message='flood', retry_after=0),
                None,
            ]),
            send_photo=AsyncMock(side_effect=[
                TelegramRetryAfter(method=SendPhoto(chat_id=101, photo='p'), message='flood', retry_after=0),
                None,
            ]),
        )
        await self.db.set_setting('promo_push_mode', 'personal')
        cfg = await push_settings(self.db)
        user_101 = await self.db.read('SELECT * FROM users WHERE user_id=?', (101,))

        self.assertEqual(await send_promo_push_to_user(bot, self.db, user_101, cfg, retry_flood=True), 'sent')
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual((await self.db.read('SELECT promo_push_sent FROM users WHERE user_id=?', (101,)))['promo_push_sent'], 1)

        self.assertTrue(await safe_send_media(bot, 101, 'file-p', 'photo', '<b>Hi</b>', retry_flood=True))
        self.assertEqual(bot.send_photo.await_count, 2)

    async def test_autopush_flood_does_not_count_as_delivery(self):
        await self.db.set_setting('promo_push_enabled', '1')
        await self.db.set_setting('promo_push_mode', 'personal')
        await self.db.set_setting('review_push_enabled', '1')
        await self.db.set_setting('review_push_interval_hours', '0')
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id=101',
                              (in_seconds(-5 * 3600),))
        await admin.review_add_receive(self.admin_msg(photo=[NS(file_id='review-photo')]), self.db)
        bot = NS(
            send_message=AsyncMock(side_effect=TelegramRetryAfter(
                method=SendMessage(chat_id=101, text='x'), message='flood', retry_after=90,
            )),
            send_photo=AsyncMock(side_effect=TelegramRetryAfter(
                method=SendPhoto(chat_id=101, photo='review-photo'), message='flood', retry_after=90,
            )),
        )
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 0)
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 0)
        user = await self.shop.profile(101)
        self.assertEqual((user['promo_push_sent'], user['promo_push_sent_at'], user['last_review_push_at']), (0, None, None))
        self.assertGreaterEqual(user['promo_push_retry_at'], in_seconds(80))
        self.assertGreaterEqual(user['review_push_retry_at'], in_seconds(80))
        self.assertIsNone(await self.db.read('SELECT * FROM user_review_pushes'))

        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 0)
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 0)
        bot.send_message.assert_awaited_once()
        bot.send_photo.assert_awaited_once()

        bot.send_message.side_effect = bot.send_photo.side_effect = None
        await self.db.execute(
            'UPDATE users SET promo_push_retry_at=?, review_push_retry_at=? WHERE user_id=101',
            (in_seconds(-60), in_seconds(-60)),
        )
        self.assertEqual(await dispatch_promo_pushes(bot, self.db, self.shop), 1)
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 0)
        await self.db.execute('UPDATE users SET promo_push_sent_at=? WHERE user_id=101',
                              (in_seconds(-16 * 60),))
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 1)
        self.assertEqual(await dispatch_review_pushes(bot, self.db, self.shop), 0)

    async def test_promo_claim_rechecks_purchase_and_prevents_parallel_send(self):
        await self.db.set_setting('promo_push_mode', 'personal')
        cfg = await push_settings(self.db)
        alice = await self.shop.profile(101)
        stale_bob = await self.shop.profile(202)
        order = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(order['id'], 'stars', charge_id='promo-race-bob')
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()

        bot = NS(send_message=AsyncMock(side_effect=send))
        first = asyncio.create_task(send_promo_push_to_user(bot, self.db, alice, cfg))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            self.assertEqual(await send_promo_push_to_user(bot, self.db, alice, cfg), 'skipped')
            self.assertEqual(await send_promo_push_to_user(bot, self.db, stale_bob, cfg), 'skipped')
        finally:
            release.set()
            self.assertEqual(await first, 'sent')
        bot.send_message.assert_awaited_once()

    async def test_long_review_caption_is_sent_in_chunks_with_buttons_on_photo(self):
        caption = '<b>' + 'А' * 4500 + '</b>'
        bot = NS(send_photo=AsyncMock(), send_message=AsyncMock())
        markup = public.kb((public.cb('Каталог', 'catalog'),))
        self.assertTrue(await safe_send_media(bot, 101, 'file-p', 'photo', caption, markup))
        bot.send_photo.assert_awaited_once_with(101, 'file-p', caption=None, reply_markup=markup)
        chunks = [call.args[1] for call in bot.send_message.await_args_list]
        self.assertEqual([len(chunk) for chunk in chunks], [4000, 500])
        self.assertEqual(''.join(chunks), 'А' * 4500)

    async def test_review_deleted_during_blast_does_not_abort_or_switch_media(self):
        for media_id in ('first-photo', 'other-photo'):
            await admin.review_add_receive(self.admin_msg(photo=[NS(file_id=media_id)]), self.db)
        review = await self.db.read('SELECT * FROM reviews ORDER BY id LIMIT 1')

        async def remove_review(*args, **kwargs):
            await self.db.execute('DELETE FROM reviews WHERE id=?', (review['id'],))

        bot = NS(send_photo=AsyncMock(side_effect=remove_review))
        users = await self.db.read('SELECT * FROM users ORDER BY user_id', many=True)
        progress = NS(edit_text=AsyncMock())
        await admin._run_review_blast(bot, self.db, self.shop, 99, review['id'], 'all',
                                     users, progress, asyncio.Event())
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_photo.await_args.args[:2], (101, 'first-photo'))
        self.assertIn('Пропущено <b>2</b>', progress.edit_text.await_args.args[0])

    async def test_viewer_next_empty_and_admin_quality_polish(self):
        # Empty active reviews -> alert instead of silent swallow
        call = NS(
            data='review_view:0',
            from_user=NS(id=101, full_name='Alice'),
            bot=NS(send_message=AsyncMock(), send_photo=AsyncMock()),
            message=self.admin_msg(),
            answer=AsyncMock(),
        )
        await public.reviews_viewer_next(call, self.db, self.shop)
        call.answer.assert_awaited_once_with('Отзывы временно недоступны.', show_alert=True)

        # Shared vs personal promo_card button for assigning to auto-push
        with patch.object(admin, 'screen', new_callable=AsyncMock) as mock_screen:
            await admin.promo_card(self.admin_call('adm:promo:card:LUXURYNEW'), self.db)
            shared_cbs = [
                btn.callback_data
                for row in mock_screen.await_args.kwargs['reply_markup'].inline_keyboard
                for btn in row
            ]
            self.assertIn('adm:ppush:setcode:LUXURYNEW', shared_cbs)

            personal = await create_personal_promo(self.db, 101, 'VIP', 150, 0)
            await admin.promo_card(self.admin_call(f"adm:promo:card:{personal['code']}"), self.db)
            personal_cbs = [
                btn.callback_data
                for row in mock_screen.await_args.kwargs['reply_markup'].inline_keyboard
                for btn in row
            ]
            self.assertFalse(any(c.startswith('adm:ppush:setcode:') for c in personal_cbs))

        # Long caption (>950 UTF-16) save shows split warning
        await self.db.execute(
            "INSERT INTO reviews(media_id, media_type, caption, active, created_at) VALUES('p1','photo','',1,?)",
            (in_seconds(-60),),
        )
        rid = (await self.db.read('SELECT id FROM reviews ORDER BY id DESC LIMIT 1'))['id']
        await self.state.set_state(admin.AdminState.review_caption)
        await self.state.update_data(review_id=rid)
        msg = self.admin_msg(text='А' * 960)
        await admin.review_caption_save(msg, self.state, self.db)
        self.assertIn('950', msg.answer.await_args.args[0])

        # Malformed int setting fallback + conversion count ignores pre-push paid order
        await self.db.set_setting('promo_push_discount', 'invalid')
        await self.db.execute(
            "UPDATE users SET promo_push_sent=1, promo_push_code='LUXURYNEW', promo_push_sent_at=? WHERE user_id=202",
            (in_seconds(-100),),
        )
        pre_ord = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(pre_ord['id'], 'stars', charge_id='chg-pre-1')
        await self.db.execute(
            'UPDATE orders SET paid_at=?, created_at=? WHERE id=?',
            (in_seconds(-200), in_seconds(-200), pre_ord['id']),
        )
        with patch.object(admin, 'screen', new_callable=AsyncMock) as mock_screen:
            await admin._render_promo_push_screen(self.admin_call('adm:ppush'), self.db)
            self.assertIn('Отправлено пушей: <b>1</b> · Купили с кодом: <b>0</b>', mock_screen.await_args.args[1])

            await self.db.execute("UPDATE orders SET paid_at=?,promo_code='LUXURYNEW' WHERE id=?", (in_seconds(-10), pre_ord['id']))
            await admin._render_promo_push_screen(self.admin_call('adm:ppush'), self.db)
            self.assertIn('Отправлено пушей: <b>1</b> · Купили с кодом: <b>1</b>', mock_screen.await_args.args[1])

    async def test_percentage_promos_and_pushes_and_v24_to_v25_migration(self):
        from aurora_bot import handlers_user as uhandlers
        from aurora_bot.pushes import prepare_promo_push
        from aurora_bot.service import price_with_promo, promo_discount_label

        self.assertEqual(price_with_promo(5, 100, discount_percent=10), (4, 90))
        self.assertEqual(price_with_promo(3, 100, discount_percent=10), (3, 90))

        # 1) Create and edit percentage promo via admin handler
        await self.state.set_state(admin.AdminState.promo_new)
        await admin.promo_new_save(self.admin_msg('vip20 20% 50'), self.state, self.db)
        row = await self.db.read("SELECT * FROM promos WHERE code='VIP20'")
        self.assertEqual((row['discount'], row['discount_percent'], row['max_uses']), (0, 20, 50))
        self.assertEqual(promo_discount_label(row), '20%')

        # Space before '%' is accepted when editing an existing promo
        await self.state.set_state(admin.AdminState.promo_new)
        await self.state.update_data(code='VIP20')
        await admin.promo_new_save(self.admin_msg('25 % 40'), self.state, self.db)
        row = await self.db.read("SELECT * FROM promos WHERE code='VIP20'")
        self.assertEqual((row['discount'], row['discount_percent'], row['max_uses']), (0, 25, 40))

        with patch.object(admin, 'screen', new_callable=AsyncMock) as pscr:
            await admin.promo_card(self.admin_call('adm:promo:card:VIP20'), self.db)
        self.assertIn('Скидка <b>25%</b>', pscr.await_args.args[1])

        # 2) Pricing + order settlement + user promo UI
        t1 = await self.shop.get_tariff(1)  # 190 RUB, 100 Stars
        expected_rub, expected_stars = price_with_promo(t1['rub'], t1['stars'], 0, 25)
        self.assertEqual(expected_rub, 142)  # 190 - 48 = 142
        self.assertGreater(expected_stars, 0)

        await self.shop.activate_promo(101, 'VIP20')
        rub_u, stars_u, code_u = await self.shop.user_price(101, t1)
        self.assertEqual((rub_u, stars_u, code_u), (expected_rub, expected_stars, 'VIP20'))

        order = await self.shop.create_order(101, 1, 'stars')
        self.assertEqual((order['amount_rub'], order['amount_stars'], order['promo_code']), (expected_rub, expected_stars, 'VIP20'))

        # User /promo activation message & catalog show discounted 25% price cleanly
        user303_msg = NS(
            text='VIP20',
            from_user=NS(id=303, username='charlie', full_name='Charlie'),
            answer=AsyncMock(),
        )
        await self.state.set_state(uhandlers.UserInput.promo)
        await uhandlers.promo_finish(user303_msg, self.state, self.shop, self.db)
        self.assertIn('<b>25%</b>', user303_msg.answer.await_args.args[0])

        await self.db.execute(
            "INSERT INTO tariffs(title, rub, stars, duration_days, active) VALUES('Мини-пак', 490, 250, 0, 1)"
        )
        with patch.object(uhandlers, 'send_section', new_callable=AsyncMock) as usend:
            cat_msg = NS(
                from_user=NS(id=303, username='charlie', full_name='Charlie'),
                message=NS(answer=AsyncMock()),
                answer=AsyncMock(),
            )
            await uhandlers.show_catalog(cat_msg, self.shop, self.db, page=0)
        cat_btns = [btn.text for row in usend.await_args.args[2].inline_keyboard for btn in row]
        self.assertTrue(any('₽' in b for b in cat_btns), cat_btns)
        self.assertIn('VIP20', usend.await_args.args[1]['value'])
        self.assertIn('25%', usend.await_args.args[1]['value'])

        # One-click claim alert shows '25%' (not '0 ₽')
        await self.db.execute("UPDATE users SET active_promo='' WHERE user_id=202")
        claim_call = self.admin_call('claim_promo:VIP20')
        claim_call.from_user = NS(id=202, username='bob', full_name='Bob')
        with patch.object(uhandlers, 'show_catalog', new_callable=AsyncMock):
            await uhandlers.claim_promo_one_click(claim_call, self.shop, self.db)
        self.assertIn('25%', claim_call.answer.await_args.args[0])

        # 3) Shared & personal percentage pushes format cleanly without '{discount} ₽'
        await self.db.set_setting('promo_push_mode', 'shared')
        await self.db.set_setting('promo_push_code', 'VIP20')
        user303 = {'user_id': 303, 'name': 'Test'}
        payload_shared = await prepare_promo_push(self.db, user303, dry_run=True)
        self.assertIsNotNone(payload_shared)
        text_s, markup_s, code_s, _ = payload_shared
        self.assertEqual(code_s, 'VIP20')
        self.assertIn('25%', text_s)
        self.assertNotIn('25% ₽', text_s)
        self.assertIn('25% · VIP20', markup_s.inline_keyboard[0][0].text)
        with patch('aurora_bot.pushes.push_settings', new_callable=AsyncMock) as settings:
            settings.return_value = {
                'promo_push_code': 'VIP20',
                'promo_push_text': 'Скидка <b>{discount}</b> ₽ на тариф',
            }
            text_s, _, _, _ = await prepare_promo_push(self.db, user303, dry_run=True)
        self.assertEqual(text_s, 'Скидка <b>25%</b> на тариф')

        # Personal % discount saved via promo_push_edit_save and rendered cleanly
        await self.db.set_setting('promo_push_mode', 'personal')
        await self.state.set_state(admin.AdminState.promo_push_field)
        await self.state.update_data(ppush_field='discount')
        await admin.promo_push_edit_save(self.admin_msg('15%'), self.state, self.db)
        self.assertEqual((await self.db.setting('promo_push_discount'))['value'], '15%')
        await self.state.set_state(admin.AdminState.promo_push_field)
        await self.state.update_data(ppush_field='discount')
        await admin.promo_push_edit_save(self.admin_msg('1 5%'), self.state, self.db)
        self.assertEqual((await self.db.setting('promo_push_discount'))['value'], '15%')
        await self.state.clear()
        payload_pers = await prepare_promo_push(self.db, user303, dry_run=False)
        text_p, markup_p, code_p, is_pers = payload_pers
        self.assertTrue(is_pers)
        self.assertIn('15%', text_p)
        self.assertNotIn('15% ₽', text_p)
        self.assertIn('15%', markup_p.inline_keyboard[0][0].text)
        pers_row = await self.db.read('SELECT * FROM promos WHERE code=?', (code_p,))
        self.assertEqual((pers_row['discount'], pers_row['discount_percent']), (0, 15))

        # 4) Explicit v24 -> v25 migration test
        with TemporaryDirectory() as tmp:
            v24_path = Path(tmp) / 'v24.sqlite3'
            seed_db = Database(v24_path)
            await seed_db.init()
            con = sqlite3.connect(v24_path)
            con.executescript("""
                DROP TABLE promos;
                CREATE TABLE promos(
                    code TEXT PRIMARY KEY, discount INTEGER NOT NULL CHECK(discount>=0),
                    max_uses INTEGER NOT NULL CHECK(max_uses>=0), used INTEGER NOT NULL DEFAULT 0 CHECK(used>=0),
                    enabled INTEGER NOT NULL DEFAULT 1, owner_user_id INTEGER, expires_at TEXT,
                    is_personal INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO promos(code, discount, max_uses, enabled) VALUES('RUB50', 50, 5, 1);
                PRAGMA user_version=24;
            """)
            con.commit()
            con.close()
            migrated = Database(v24_path)
            await migrated.init()
            self.assertEqual((await migrated.read('PRAGMA user_version'))['user_version'], 30)
            rub50 = await migrated.read("SELECT discount, discount_percent FROM promos WHERE code='RUB50'")
            self.assertEqual((rub50['discount'], rub50['discount_percent']), (50, 0))

    async def test_broadcast_buyers_cold_segments_and_media_group_album(self):
        # Make user 202 a paid buyer while users 101 and 303 stay non-buyers (cold)
        ord202 = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(ord202['id'], 'stars', charge_id='chg-bcast-buyer')

        # 1) Segment selector displays both cold and buyers categories and exact counts
        with patch.object(admin, 'screen', new_callable=AsyncMock) as scr:
            await admin.broadcast_start(self.admin_call('adm:broadcast'), self.state, self.db)
        btn_texts = [btn.text for row in scr.await_args.kwargs['reply_markup'].inline_keyboard for btn in row]
        self.assertTrue(any('Некупившие' in t and '· 2' in t for t in btn_texts))
        self.assertTrue(any('Купившие' in t and '· 1' in t for t in btn_texts))

        # 2) Album post (media_group_id) buffers items, previews with copy_messages, and broadcasts to buyers only
        await self.state.set_state(admin.AdminState.broadcast)
        await self.state.update_data(broadcast_segment='buyers')

        bcast_bot = NS(
            copy_message=AsyncMock(side_effect=lambda chat_id, from_id, mid: NS(message_id=mid + 1000)),
            copy_messages=AsyncMock(side_effect=lambda chat_id, from_id, mids: [NS(message_id=m + 1000) for m in mids]),
            send_message=AsyncMock(),
        )
        m1 = NS(chat=NS(id=99), from_user=NS(id=99), message_id=501, text=None, media_group_id='alb-99',
                answer=AsyncMock(return_value=NS(message_id=777)))
        m2 = NS(chat=NS(id=99), from_user=NS(id=99), message_id=502, text=None, media_group_id='alb-99',
                answer=AsyncMock(return_value=NS(message_id=777)))

        original_idle = admin._BROADCAST_ALBUM_IDLE_SEC
        admin._BROADCAST_ALBUM_IDLE_SEC = 0.05
        try:
            await admin.broadcast_preview(m1, self.state, self.db, bcast_bot)
            await admin.broadcast_preview(m2, self.state, self.db, bcast_bot)
            await asyncio.sleep(0.25)
        finally:
            admin._BROADCAST_ALBUM_IDLE_SEC = original_idle

        bcast_bot.copy_messages.assert_awaited_once_with(99, 99, [501, 502])
        self.assertEqual(await self.state.get_state(), admin.AdminState.broadcast_confirm.state)
        state_data = await self.state.get_data()
        self.assertEqual(state_data['broadcast_msg_ids'], [1501, 1502])

        # Send to buyers segment -> delivers [1501, 1502] to user 202 (buyer) via copy_messages
        confirm_call = NS(
            from_user=NS(id=99),
            data='adm:sendbroadcast',
            message=NS(message_id=777, answer=AsyncMock(return_value=NS(edit_text=AsyncMock()))),
            answer=AsyncMock(),
        )
        bcast_bot.copy_messages.reset_mock()
        await admin.broadcast_send(confirm_call, self.state, self.db, bcast_bot)
        task_entry = admin._ACTIVE_BROADCASTS.get(99)
        if task_entry:
            await task_entry[0]
        bcast_bot.copy_messages.assert_awaited_once_with(202, 99, [1501, 1502])

        # 3) Review blast segment picker also exposes 'buyers'
        await self.db.execute(
            "INSERT INTO reviews(media_id, media_type, caption, active, created_at) VALUES('rev-b','photo','',1,?)",
            (in_seconds(-10),),
        )
        rid = (await self.db.read('SELECT id FROM reviews ORDER BY id DESC LIMIT 1'))['id']
        with patch.object(admin, 'screen', new_callable=AsyncMock) as rscr:
            await admin.review_blast_choose_segment(self.admin_call(f'adm:rpush:blast:{rid}'), self.db)
        rcbs = [btn.callback_data for row in rscr.await_args.kwargs['reply_markup'].inline_keyboard for btn in row]
        self.assertIn(f'adm:rpush:blastgo:{rid}:buyers', rcbs)
        self.assertIn(f'adm:rpush:blastgo:{rid}:cold', rcbs)

    async def test_incomplete_album_is_rejected_in_preview_and_delivery(self):
        await self.state.set_state(admin.AdminState.broadcast)
        bot = NS(copy_messages=AsyncMock(return_value=[NS(message_id=1001)]))
        first = self.admin_msg()
        first.message_id, first.media_group_id = 1, 'incomplete'
        second = self.admin_msg()
        second.message_id, second.media_group_id = 2, 'incomplete'
        with patch.object(admin, '_BROADCAST_ALBUM_IDLE_SEC', 0.1):
            await admin.broadcast_preview(first, self.state, self.db, bot)
            await admin.broadcast_preview(second, self.state, self.db, bot)
            await admin._BROADCAST_ALBUMS[(99, 'incomplete')]['task']
        self.assertEqual(await self.state.get_state(), admin.AdminState.broadcast.state)
        self.assertNotIn('broadcast_confirmation_id', await self.state.get_data())
        first.answer.assert_awaited_once_with('Telegram не может скопировать этот альбом или тип медиа.')

        progress = NS(edit_text=AsyncMock())
        await admin._run_broadcast(bot, self.db, 99, 99, [1, 2], 'all',
                                   [{'user_id': 101}], progress, asyncio.Event())
        self.assertIn('Доставлено <b>0</b>', progress.edit_text.await_args.args[0])
        self.assertIn('Не доставлено <b>1</b>', progress.edit_text.await_args.args[0])

    async def test_album_late_item_replaces_confirmation_and_navigation_cancels_preview(self):
        await self.state.set_state(admin.AdminState.broadcast)
        await self.state.update_data(broadcast_segment='buyers')
        order = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(order['id'], 'stars', charge_id='album-late-buyer')
        bot = NS(
            copy_messages=AsyncMock(side_effect=lambda _chat, _source, ids: [
                NS(message_id=mid + 1000) for mid in ids
            ]),
        )
        first, second, third = [self.admin_msg() for _ in range(3)]
        for index, msg in enumerate((first, second, third), 1):
            msg.message_id, msg.media_group_id = index, 'late'
            msg.answer.return_value = NS(message_id=700 + index)
        with patch.object(admin, '_BROADCAST_ALBUM_IDLE_SEC', 0.1):
            await admin.broadcast_preview(first, self.state, self.db, bot)
            await admin.broadcast_preview(second, self.state, self.db, bot)
            await admin._BROADCAST_ALBUMS[(99, 'late')]['task']
            self.assertEqual((await self.state.get_data())['broadcast_confirmation_id'], 701)
            await admin.broadcast_preview(third, self.state, self.db, bot)
            await admin._BROADCAST_ALBUMS[(99, 'late')]['task']
        data = await self.state.get_data()
        self.assertEqual(data['broadcast_confirmation_id'], 703)
        self.assertEqual(data['broadcast_msg_ids'], [1001, 1002, 1003])

        await self.state.set_state(admin.AdminState.broadcast)
        started = asyncio.Event()

        async def copy(*args):
            started.set()
            await asyncio.Future()

        bot.copy_messages.side_effect = copy
        with patch.object(admin, '_BROADCAST_ALBUM_IDLE_SEC', 0.1):
            await admin.broadcast_preview(first, self.state, self.db, bot)
            await admin.broadcast_preview(second, self.state, self.db, bot)
            task = admin._BROADCAST_ALBUMS[(99, 'late')]['task']
            await asyncio.wait_for(started.wait(), timeout=5)
            with patch.object(admin, 'screen', new_callable=AsyncMock):
                await admin.broadcast_choose_segment(self.admin_call('adm:bcast:seg:cold'), self.state, self.db)
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await self.state.get_state(), admin.AdminState.broadcast.state)
        self.assertEqual((await self.state.get_data())['broadcast_segment'], 'cold')
