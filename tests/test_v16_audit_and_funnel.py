"""Regression tests for v16 bug fixes, concurrency/caching improvements, and funnel upgrades."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from aurora_bot.access import AccessManager, validate_sale_channels
from aurora_bot.alerts import (
    dispatch_abandoned_cart_reminders,
    dispatch_tariff_view_reminders,
    dispatch_trial_upsell_touchpoints,
    dispatch_whale_alerts,
    send_expiry_upsell_if_needed,
)
from aurora_bot.config import Config
from aurora_bot.db import Database, SEED_TARIFFS, in_seconds
from aurora_bot.handlers_admin import (
    _render_section_card,
    admin_stars_receipt,
    revoke_purchase,
)
from aurora_bot.handlers_user import (
    _render_tariff_view,
    check_payment,
    my_accesses,
    successful_stars,
    welcome,
)
from aurora_bot.live_stats import get_live_recent_purchases, get_live_tariff_buyers
from aurora_bot.payments import CryptoPay, GatewayError, Payments
from aurora_bot.service import Shop
from aurora_bot.ui import _utf16_len, product_text, validate_section_html
from aurora_bot.workers import reconcile_external, recover_crypto_invoices


class V16AuditAndFunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'v16.sqlite3')
        await self.db.init()
        self.shop = Shop(self.db)
        self.access = AccessManager(self.db, self.shop)
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id IN (1, 2, 3, 4, 5, 6)")
        await self.db.register(101, 'alice', 'Alice')
        await self.db.register(202, 'bob', 'Bob')

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_bug1_admin_stars_receipt_passes_access_manager(self):
        cfg = Config('123:mock', frozenset({101}))
        msg = NS(from_user=NS(id=101), successful_payment=None)
        with patch('aurora_bot.handlers_admin.successful_stars', new_callable=AsyncMock) as forward:
            await admin_stars_receipt(msg, self.shop, cfg, self.access)
            forward.assert_awaited_once_with(msg, self.shop, cfg, self.access)

    async def test_bug2_and_funnel_strikethrough_promo_vs_upgrade(self):
        # Standard price card displays crossed-out anchor & stays <=950 UTF-16 even with promo
        t1 = await self.shop.get_tariff(1)
        std_text = product_text(t1, t1['stars'], rub=t1['rub'])
        self.assertIn('190 ₽', std_text)
        validate_section_html(std_text)

        for idx, (title, desc, rub, stars, days, prev) in enumerate(SEED_TARIFFS, 1):
            row = {'id': idx, 'title': title, 'description': desc, 'preview_description': prev,
                   'rub': rub, 'stars': stars, 'duration_days': days}
            for b in (1200, None):
                rendered = product_text(row, max(stars - 75, 1), buyers=b, promo='LUXURYNEW', rub=max(rub - 150, 1))
                validate_section_html(rendered)
                self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 950)
                if idx in (5, 6):
                    up_rendered = product_text(
                        row, max(stars - 75, 1), buyers=b, promo='LUXURYNEW', rub=max(rub - 150, 1),
                        upgrade={'rub': max(rub - 240, 1), 'stars': max(stars - 125, 1), 'credit_rub': 240, 'saved_rub': 240},
                    )
                    validate_section_html(up_rendered)
                    self.assertLessEqual(len(up_rendered.encode('utf-16-le')) // 2, 950)

        # Base entry (240 RUB) -> ULTRA upgrade is 950 RUB; if promo makes base ULTRA 690 RUB, prefer promo!
        trial = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle(trial['id'], 'stars', charge_id='chg-b2')
        await self.db.execute("INSERT INTO promos(code, discount, max_uses, enabled) VALUES('MEGA500', 500, 10, 1)")
        await self.shop.activate_promo(101, 'MEGA500')

        call = NS(
            data='tariff:3',
            from_user=NS(id=101, username='alice', full_name='Alice'),
            answer=AsyncMock(),
            message=NS(),
        )
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await _render_tariff_view(call, self.shop, 3, is_preview=False)
            rows = sender.await_args.kwargs['markup'].inline_keyboard
            self.assertEqual(rows[0][0].callback_data, 'pay_options:3')
            self.assertIn('MEGA500', sender.await_args.args[1]['value'])

        # With smaller promo (e.g. 150 -> 840 RUB > 800 RUB upgrade), upgrade uses base tariff 990 RUB crossed out
        await self.db.execute("INSERT INTO promos(code, discount, max_uses, enabled) VALUES('SMALL150', 150, 10, 1)")
        await self.shop.activate_promo(101, 'SMALL150')
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await _render_tariff_view(call, self.shop, 3, is_preview=False)
            rows = sender.await_args.kwargs['markup'].inline_keyboard
            self.assertEqual(rows[0][0].callback_data, 'up_pay_options:3')
            self.assertIn('<s>499 Stars (~990 ₽)</s>', sender.await_args.args[1]['value'])

    async def test_bug4_revoked_credit_excluded(self):
        o1 = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle(o1['id'], 'stars', charge_id='chg-rev')
        p1 = await self.db.read('SELECT id FROM purchases WHERE order_id=?', (o1['id'],))
        self.assertTrue(await self.shop.upgrade_offers(101))

        rev_call = NS(data=f"adm:revokeok:{p1['id']}", from_user=NS(id=901), answer=AsyncMock(),
                      bot=NS(send_message=AsyncMock()),
                      message=NS(edit_text=AsyncMock(), answer=AsyncMock(), photo=None, video=None,
                                 animation=None, document=None))
        await revoke_purchase(rev_call, self.db, self.access)
        self.assertEqual((await self.db.read('SELECT revoked_by_admin FROM purchases WHERE id=?', (p1['id'],)))['revoked_by_admin'], 1)
        self.assertEqual(await self.shop.upgrade_offers(101), [])

    async def test_perf_channel_validation_cache_crypto_cutoff_and_settle_noop(self):
        bot = NS(
            id=777,
            get_chat=AsyncMock(return_value=NS(type='channel', username=None, active_usernames=[])),
            get_chat_member=AsyncMock(return_value=NS(status='administrator', can_invite_users=True, can_restrict_members=True)),
        )
        await validate_sale_channels(bot, [-100111])
        await validate_sale_channels(bot, [-100111])
        self.assertEqual(bot.get_chat.await_count, 1)

        # Old >3d cryptopay order without external_id does not trigger getInvoices polling
        stale = await self.shop.create_order(101, 1, 'cryptopay')
        await self.db.execute("UPDATE orders SET created_at=? WHERE id=?", (in_seconds(-5 * 86400), stale['id']))
        crypto = CryptoPay(Config('123:mock', frozenset({901}), cryptopay_token='cp-tok'))
        crypto.invoices = AsyncMock(return_value=[])
        await recover_crypto_invoices(self.db, Payments(Config('123:mock', frozenset({901})), self.shop), crypto)
        crypto.invoices.assert_not_awaited()

        # settle() avoids extra UPDATE when already paid with same paid_at
        o_star = await self.shop.create_order(101, 1, 'stars')
        paid_dt = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(await self.shop.settle(o_star['id'], 'stars', charge_id='chg-idem', paid_at=paid_dt))
        self.assertFalse(await self.shop.settle(o_star['id'], 'stars', charge_id='chg-idem', paid_at=paid_dt))

    async def test_funnel_view_reminder_and_trial_touchpoints(self):
        # 1) View reminder fires 12m+ after viewing tariff without creating an order
        call = NS(data='tariff:1', from_user=NS(id=202, username='bob', full_name='Bob'),
                  answer=AsyncMock(), message=NS())
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock):
            await _render_tariff_view(call, self.shop, 1, is_preview=False)
        await self.db.execute('UPDATE users SET last_viewed_at=? WHERE user_id=202', (in_seconds(-30 * 60),))
        bot = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_tariff_view_reminders(bot, self.db, self.shop), 1)
        self.assertEqual(await dispatch_tariff_view_reminders(bot, self.db, self.shop), 0)
        reminder_body = bot.send_message.await_args.args[1]
        reminder_btns = [b.text for r in bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard for b in r]
        self.assertIn('Пропуск', reminder_body)
        self.assertTrue(any('190 ₽' in t for t in reminder_btns))

        # 2a) Base entry post-join upgrade credit touchpoint
        trial_ord = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle(trial_ord['id'], 'stars', charge_id='chg-funnel-trial')
        purchase = await self.db.read('SELECT * FROM purchases WHERE order_id=?', (trial_ord['id'],))
        await self.db.execute(
            "UPDATE grants SET state='joined', joined_at=? WHERE purchase_id=?",
            (in_seconds(-25 * 60), purchase['id']),
        )
        bot.send_message.reset_mock()
        self.assertEqual(await dispatch_trial_upsell_touchpoints(bot, self.db, self.shop), 1)
        self.assertIn('зачт', bot.send_message.await_args.args[1])
        self.assertIn('800 ₽', bot.send_message.await_args.args[1])
        self.assertEqual(await dispatch_trial_upsell_touchpoints(bot, self.db, self.shop), 0)

        # 2b) Timed 24h tariff receives 3h pre-expiry loss-aversion alert
        await self.db.register(303, 'timed', 'Timed')
        await self.db.execute(
            "INSERT INTO tariffs(id,title,description,rub,stars,duration_days,active,channel_ids) VALUES(99,'Timed 24h','D',350,180,1,1,'-100111')"
        )
        timed_ord = await self.shop.create_order(303, 99, 'stars')
        await self.shop.settle(timed_ord['id'], 'stars', charge_id='chg-timed-24h')
        timed_purchase = await self.db.read('SELECT * FROM purchases WHERE order_id=?', (timed_ord['id'],))
        await self.db.execute(
            'UPDATE purchases SET post_join_upsell_sent=1, expires_at=? WHERE id=?',
            (in_seconds(90 * 60), timed_purchase['id']),
        )
        bot.send_message.reset_mock()
        self.assertEqual(await dispatch_trial_upsell_touchpoints(bot, self.db, self.shop), 1)
        self.assertIn('3 часа', bot.send_message.await_args.args[1])
        self.assertEqual(await dispatch_trial_upsell_touchpoints(bot, self.db, self.shop), 0)

    async def test_v16_migration_backfills_historical_purchases(self):
        with TemporaryDirectory() as legacy_tmp:
            legacy_path = Path(legacy_tmp) / 'v15.sqlite3'
            con = sqlite3.connect(legacy_path)
            con.executescript(f'''
                CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, last_seen TEXT NOT NULL,
                    active_promo TEXT NOT NULL DEFAULT '');
                CREATE TABLE tariffs(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', rub INTEGER NOT NULL, stars INTEGER NOT NULL,
                    duration_days INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                    channel_ids TEXT NOT NULL DEFAULT '', media_id TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL DEFAULT '', preview_description TEXT NOT NULL DEFAULT '',
                    delivery_urls TEXT NOT NULL DEFAULT '[]', tier TEXT NOT NULL DEFAULT '');
                CREATE TABLE orders(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, tariff_id INTEGER,
                    title TEXT NOT NULL, amount_rub INTEGER NOT NULL, amount_stars INTEGER NOT NULL,
                    duration_days INTEGER NOT NULL, channels TEXT NOT NULL, promo_code TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL, external_id TEXT NOT NULL DEFAULT '', payment_url TEXT NOT NULL DEFAULT '',
                    charge_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, paid_at TEXT,
                    notice_sent INTEGER NOT NULL DEFAULT 0, checkout_id TEXT NOT NULL DEFAULT '',
                    checkout_until TEXT, invoice_sent_at TEXT, invoice_sending_at TEXT,
                    refund_requested_at TEXT, notice_retry_at TEXT, reconcile_at TEXT,
                    delivery_urls TEXT NOT NULL DEFAULT '[]', greeting_event_id TEXT,
                    order_kind TEXT NOT NULL DEFAULT 'tariff', custom_invoice_id TEXT NOT NULL DEFAULT '',
                    whale_alert_sent INTEGER NOT NULL DEFAULT 0, cart_reminder_sent INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE purchases(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL, tariff_id INTEGER NOT NULL, title TEXT NOT NULL,
                    started_at TEXT NOT NULL, expires_at TEXT, expiry_notice_sent INTEGER NOT NULL DEFAULT 0);
                INSERT INTO users VALUES(101, 'u', 'U', '{in_seconds(-86400)}', '{in_seconds(-86400)}', '');
                INSERT INTO orders(id,user_id,tariff_id,title,amount_rub,amount_stars,duration_days,channels,provider,status,created_at,expires_at)
                    VALUES('ord-old', 101, 1, 'Old', 350, 180, 1, '[-100]', 'stars', 'paid', '{in_seconds(-10 * 3600)}', '{in_seconds(-9 * 3600)}');
                INSERT INTO purchases(order_id,user_id,tariff_id,title,started_at,expires_at,expiry_notice_sent)
                    VALUES('ord-old', 101, 1, 'Old', '{in_seconds(-10 * 3600)}', '{in_seconds(-3600)}', 1);
                PRAGMA user_version=15;
            ''')
            con.commit()
            con.close()

            migrated = Database(legacy_path)
            await migrated.init()
            self.assertEqual((await migrated.read('PRAGMA user_version'))['user_version'], 29)
            p = await migrated.read("SELECT * FROM purchases WHERE order_id='ord-old'")
            self.assertEqual((p['revoked_by_admin'], p['post_join_upsell_sent'], p['pre_expiry_upsell_sent']), (0, 1, 1))

    async def test_audit_revoked_purchase_suppresses_upsell_and_shows_revoked_status(self):
        order = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(order['id'], 'stars', charge_id='chg-revoked')
        purchase = await self.db.read('SELECT * FROM purchases WHERE order_id=?', (order['id'],))
        await self.db.execute('UPDATE purchases SET revoked_by_admin=1 WHERE id=?', (purchase['id'],))

        bot = NS(send_message=AsyncMock())
        self.assertFalse(await send_expiry_upsell_if_needed(bot, self.db, self.shop, purchase['id']))
        bot.send_message.assert_not_called()

        call = NS(
            data='accesses',
            from_user=NS(id=202, username='bob', full_name='Bob'),
            answer=AsyncMock(),
            message=NS(answer=AsyncMock()),
        )
        await my_accesses(call, self.shop)
        rendered = '\n'.join(c.args[0] for c in call.message.answer.await_args_list)
        self.assertIn('Отозван', rendered)
        self.assertNotIn('Активен', rendered)

    async def test_audit_live_recent_purchases_honors_explicit_tier(self):
        await self.db.execute("UPDATE tariffs SET tier='diamond', title='Базовый тест 24 часа' WHERE id=1")
        await self.db.execute('UPDATE tariffs SET active=0 WHERE id!=1')
        current = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
        diamond_feed = await get_live_recent_purchases(self.shop, self.db, current=current)
        await self.db.execute("UPDATE tariffs SET tier='trial' WHERE id=1")
        trial_feed = await get_live_recent_purchases(self.shop, self.db, current=current)
        self.assertEqual(diamond_feed['popular_category'], 'Базовый тест 24 часа')
        self.assertLess(diamond_feed['total_sales_today'], trial_feed['total_sales_today'])

    async def test_audit_reconcile_external_survives_crypto_batch_failure(self):
        cfg = Config('123:mock', frozenset({101}), cryptopay_token='tok-123', allow_external_methods=True)
        order = await self.shop.create_order(202, 1, 'cryptopay')
        await self.db.execute(
            "UPDATE orders SET external_id='ext-99', reconcile_at=NULL WHERE id=?",
            (order['id'],),
        )
        fake_crypto = NS(
            token=cfg.cryptopay_token,
            invoices=AsyncMock(side_effect=GatewayError('Crypto Pay API down')),
        )
        payments = NS(
            crypto_enabled=True,
            crypto=fake_crypto,
            reconcile=AsyncMock(return_value=False),
        )
        await reconcile_external(cfg, self.db, payments)
        payments.reconcile.assert_awaited_once()
        refreshed = await self.db.read('SELECT reconcile_at FROM orders WHERE id=?', (order['id'],))
        self.assertIsNotNone(refreshed['reconcile_at'])

    async def test_audit_section_card_bounds_escaped_pre_under_4096(self):
        payload = '"' * 2600 + ' КОНЕЦ'
        await self.db.set_setting('welcome', payload)
        with patch('aurora_bot.handlers_admin.screen', new_callable=AsyncMock) as mock_screen:
            await _render_section_card(NS(), self.db, 'welcome')
        rendered = mock_screen.await_args.args[1]
        self.assertLessEqual(len(rendered), 4096)
        self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 4096)
        self.assertIn('…показана часть шаблона', rendered)

    async def test_v19_conversion_traps_decoy_buttons_catalog_prices_and_v18_migration(self):
        from aurora_bot.db import DEFAULT_SETTINGS, V18_SEED_UPGRADES
        from aurora_bot.handlers_user import show_catalog

        # 1) Catalog buttons omit prices to reduce upfront friction; titles reveal all 7 tiers
        call_cat = NS(from_user=NS(id=101, full_name='Alice'), message=NS())
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await show_catalog(call_cat, self.shop, self.db)
            btn_texts = [row[0].text for row in sender.await_args.args[2].inline_keyboard]
            self.assertTrue(all('₽' in txt or '⭐' in txt for txt in btn_texts))
            self.assertTrue(any('TikTok' in txt for txt in btn_texts))
            self.assertTrue(any('ВСЁ СРАЗУ' in txt for txt in btn_texts))
            self.assertTrue(any('VIP MAX' in txt for txt in btn_texts))

        # 2) Decoy +400 RUB upsell button shows inside 590 RUB card when Ultra VIP is deliverable, and hides otherwise
        call = NS(data='tariff:2', from_user=NS(id=101, username='alice', full_name='Alice'),
                  answer=AsyncMock(), message=NS())
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await _render_tariff_view(call, self.shop, 2, is_preview=False)
            decoy_btns = [b for row in sender.await_args.kwargs['markup'].inline_keyboard for b in row]
            self.assertTrue(any('+400 ₽' in b.text and b.callback_data == 'tariff:3' for b in decoy_btns))

        await self.db.execute("UPDATE tariffs SET channel_ids='', delivery_urls='[]' WHERE id=3")
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await _render_tariff_view(call, self.shop, 2, is_preview=False)
            no_decoy_cbs = [b.callback_data for row in sender.await_args.kwargs['markup'].inline_keyboard for b in row]
            self.assertNotIn('tariff:3', no_decoy_cbs)

        # 3) Static delivery_urls Base buyer (no grants rows) receives 20m post-purchase upgrade nudge
        await self.db.execute("UPDATE tariffs SET duration_days=0, channel_ids='', delivery_urls='[\"https://t.me/+BaseLink\"]' WHERE id=1")
        await self.db.execute("UPDATE tariffs SET channel_ids='-100555' WHERE id=3")
        await self.db.register(404, 'static', 'Static')
        s_ord = await self.shop.create_order(404, 1, 'stars')
        await self.shop.settle(s_ord['id'], 'stars', charge_id='chg-static-base')
        await self.db.execute('UPDATE purchases SET started_at=? WHERE order_id=?', (in_seconds(-25 * 60), s_ord['id']))
        bot = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_trial_upsell_touchpoints(bot, self.db, self.shop), 1)
        self.assertIn('800 ₽', bot.send_message.await_args.args[1])

        # 4) Realistic v18 -> v26 upgrade preserves static delivery_urls, custom #2 price, and uploaded media_id
        with TemporaryDirectory() as tmp:
            v18_path = Path(tmp) / 'v18_live_like.sqlite3'
            v18_db = Database(v18_path)
            await v18_db.init()
            old_catalog_v18 = (
                "🔞 <b>Закрытые каналы и архивы {project_name}</b>\n\n"
                "В каждом канале закреплён навигатор по именам и трендам. Личная ссылка на вход "
                "генерируется ботом автоматически в ту же секунду после оплаты.\n\n"
                "💡 <b>С чего лучше начать:</b>\n"
                "• <b>Хочешь затестить базу?</b> Возьми <b>Тест-драйв на 24 часа (350 ₽)</b> — "
                "при переходе на вечный VIP мы полностью вычтем эти 350 ₽ из стоимости.\n"
                "• <b>Хочешь забрать всё?</b> 8 из 10 участников сразу берут <b>«⭐️ Ultra VIP: 5 каналов»</b> — "
                "доплата минимальная, а доступ ко всем 5 каналам остаётся навсегда.\n\n"
                "🔽 <b>Выбери нужный раздел ниже:</b>"
            )
            await v18_db.set_setting('catalog', old_catalog_v18, media_id='cat_cover', media_type='photo')
            await v18_db.execute('DELETE FROM tariffs WHERE id=7')
            for tid, row in enumerate(V18_SEED_UPGRADES, 1):
                await v18_db.execute(
                    'UPDATE tariffs SET title=?,description=?,rub=?,stars=?,duration_days=?,preview_description=? WHERE id=?',
                    (*row, tid),
                )
            await v18_db.execute(
                "UPDATE tariffs SET duration_days=0,delivery_urls='[\"https://t.me/+LiveBase\"]',media_id='t1_pic',media_type='photo' WHERE id=1"
            )
            await v18_db.execute('UPDATE tariffs SET rub=777,stars=400 WHERE id=2')
            await v18_db.execute('PRAGMA user_version=18')

            await v18_db.init()
            self.assertEqual((await v18_db.read('PRAGMA user_version'))['user_version'], 29)
            cat_row = await v18_db.setting('catalog')
            self.assertEqual(cat_row['value'], DEFAULT_SETTINGS['catalog'])
            self.assertEqual((cat_row['media_id'], cat_row['media_type']), ('cat_cover', 'photo'))
            migrated_tariffs = await v18_db.read('SELECT * FROM tariffs ORDER BY id', many=True)
            self.assertIn(len(migrated_tariffs), (7, 8))
            self.assertEqual((migrated_tariffs[0]['rub'], migrated_tariffs[0]['stars'], migrated_tariffs[0]['duration_days']), (190, 99, 3))
            self.assertEqual((migrated_tariffs[0]['delivery_urls'], migrated_tariffs[0]['media_id']), ('["https://t.me/+LiveBase"]', 't1_pic'))
            self.assertEqual((migrated_tariffs[1]['rub'], migrated_tariffs[1]['stars']), (777, 400))
            self.assertEqual((migrated_tariffs[4]['rub'], migrated_tariffs[6]['rub']), (1190, 4990))

    async def test_v19_conversion_audit_bugfixes_and_utf16_budgets(self):
        # 1) All 7 seeded tariffs preserve conversion nudges visibly (<= 930 UTF-16) on card & preview (normal & promo)
        for tid in range(1, 5):
            row = await self.shop.get_tariff(tid)
            for promo_code, disc in (('', 0), ('LUXURYNEW', 150)):
                rub = max(10, row['rub'] - disc)
                for is_prev in (False, True):
                    txt = product_text(row, row['stars'], buyers=None if is_prev else 120, promo=promo_code, rub=rub)
                    self.assertLessEqual(_utf16_len(txt), 930, f'T{tid} prev={is_prev} promo={bool(promo_code)}')
                    self.assertIn('Стоимость:', txt)

        # 2) Promo-adjusted reminders quote discounted RUB + offer Ultra contrast on mid-tier view
        await self.db.register(501, 'promo_view', 'PromoViewer')
        await self.db.execute("INSERT INTO promos(code, discount, max_uses, enabled) VALUES('TEST150', 150, 10, 1)")
        await self.shop.activate_promo(501, 'TEST150')
        await self.db.execute(
            'UPDATE users SET last_viewed_tariff_id=1, last_viewed_at=?, view_reminder_sent=0 WHERE user_id=501',
            (in_seconds(-1800),),
        )
        bot = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_tariff_view_reminders(bot, self.db, self.shop), 1)
        view_btns = [b.text for r in bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard for b in r]
        self.assertTrue(any('40 ₽' in t for t in view_btns))
        self.assertTrue(any('все 4 канала' in t for t in view_btns))

        # Abandoned cart reminder on base tariff #1 uses honest copy + promo-free price when no cheaper entry exists
        await self.db.register(502, 'cart_base', 'CartBase')
        o_base = await self.shop.create_order(502, 1, 'stars')
        await self.db.execute("UPDATE orders SET created_at=? WHERE id=?", (in_seconds(-1800), o_base['id']))
        bot_cart = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_abandoned_cart_reminders(bot_cart, self.db, self.shop), 1)
        cart_msg = bot_cart.send_message.await_args.args[1]
        self.assertNotIn('старший пакет', cart_msg)

        # 3) Existing Ultra owner does not get Ultra decoy button or upgrade nudge on junior tariff #2
        await self.db.register(503, 'ultra_owner', 'UltraOwner')
        o_u = await self.shop.create_order(503, 3, 'stars')
        await self.shop.settle(o_u['id'], 'stars', charge_id='chg-ultra-own')
        call_u = NS(data='tariff:2', from_user=NS(id=503, username='ultra_owner', full_name='UltraOwner'),
                    answer=AsyncMock(), message=NS())
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await _render_tariff_view(call_u, self.shop, 2, is_preview=False)
            btn_cbs = [b.callback_data for r in sender.await_args.kwargs['markup'].inline_keyboard for b in r]
            self.assertNotIn('tariff:3', btn_cbs)

        # 4) Base (#1 = 190) buyer viewing #2 (590) sees accurate Ultra upgrade price (800 RUB), not misleading +400 RUB
        await self.db.register(504, 'base_owner', 'BaseOwner')
        o_b = await self.shop.create_order(504, 1, 'stars')
        await self.shop.settle(o_b['id'], 'stars', charge_id='chg-base-own')
        call_b = NS(data='tariff:2', from_user=NS(id=504, username='base_owner', full_name='BaseOwner'),
                    answer=AsyncMock(), message=NS())
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await _render_tariff_view(call_b, self.shop, 2, is_preview=False)
            btn_texts = [b.text for r in sender.await_args.kwargs['markup'].inline_keyboard for b in r]
            self.assertTrue(any('800 ₽' in t for t in btn_texts))
            self.assertFalse(any('+400 ₽' in t for t in btn_texts))

        # 5) Black VIP owner (4990 RUB) neither receives expiry nag for old 30d purchase nor sees upgrade bullet in my_accesses
        await self.db.execute("UPDATE tariffs SET active=1, duration_days=0, delivery_urls='[\"https://t.me/+BlackVip\"]' WHERE id=7")
        await self.db.register(505, 'black_vip', 'BlackVIP')
        o_30 = await self.shop.create_order(505, 2, 'stars')
        await self.shop.settle(o_30['id'], 'stars', charge_id='chg-30d')
        p_30 = await self.db.read('SELECT id FROM purchases WHERE order_id=?', (o_30['id'],))
        await self.db.execute("UPDATE grants SET state='expired' WHERE purchase_id=?", (p_30['id'],))
        o_blk = await self.shop.create_order(505, 7, 'stars')
        await self.shop.settle(o_blk['id'], 'stars', charge_id='chg-black')
        bot_exp = NS(send_message=AsyncMock())
        self.assertFalse(await send_expiry_upsell_if_needed(bot_exp, self.db, self.shop, p_30['id']))
        bot_exp.send_message.assert_not_awaited()

        call_acc = NS(data='accesses:0', from_user=NS(id=505, username='black_vip', full_name='BlackVIP'),
                      answer=AsyncMock(), message=NS(answer=AsyncMock()))
        await my_accesses(call_acc, self.shop)
        answered_texts = [c.args[0] for c in call_acc.message.answer.await_args_list if c.args]
        joined_texts = '\n'.join(answered_texts)
        self.assertNotIn('VIP-привилегии участника', joined_texts)
        self.assertNotIn('Апгрейд до', joined_texts)

        # 6) Heavily promo-discounted high tier (<1000 paid RUB) is excluded from entry 20m upsell; Diamond fallback has accurate copy
        await self.db.register(506, 'promo_mid', 'PromoMid')
        await self.db.execute("INSERT INTO promos(code, discount, max_uses, enabled) VALUES('HUGE500', 500, 10, 1)")
        await self.shop.activate_promo(506, 'HUGE500')
        o_mid = await self.shop.create_order(506, 3, 'stars')  # 990 - 500 = 490 < 1000, but tariff #3 is Ultra (rub=990)
        await self.shop.settle(o_mid['id'], 'stars', charge_id='chg-huge-promo')
        p_mid = await self.db.read('SELECT id FROM purchases WHERE order_id=?', (o_mid['id'],))
        await self.db.execute("UPDATE grants SET state='joined', joined_at=? WHERE purchase_id=?", (in_seconds(-1800), p_mid['id']))
        bot_up = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_trial_upsell_touchpoints(bot_up, self.db, self.shop), 0)

        await self.db.execute('UPDATE tariffs SET active=0 WHERE id=3')
        await self.db.register(507, 'base_diamond', 'BaseDiamond')
        o_bd = await self.shop.create_order(507, 1, 'stars')
        await self.shop.settle(o_bd['id'], 'stars', charge_id='chg-base-diam')
        p_bd = await self.db.read('SELECT id FROM purchases WHERE order_id=?', (o_bd['id'],))
        await self.db.execute("UPDATE grants SET state='joined', joined_at=? WHERE purchase_id=?", (in_seconds(-1800), p_bd['id']))
        bot_diam = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_trial_upsell_touchpoints(bot_diam, self.db, self.shop), 1)
        diam_msg = bot_diam.send_message.await_args.args[1]
        self.assertNotIn('ещё 4 канала', diam_msg)
        self.assertIn('VIP MAX', diam_msg)

        # 7) 2026 impulse timing works (12m view reminder, 4m cart reminder, 20m post-join)
        await self.db.execute('UPDATE tariffs SET active=1 WHERE id=3')
        await self.db.register(508, 'fast_view', 'FastView')
        await self.db.execute(
            'UPDATE users SET last_viewed_tariff_id=4, last_viewed_at=?, view_reminder_sent=0 WHERE user_id=508',
            (in_seconds(-13 * 60),),
        )
        bot_fast = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_tariff_view_reminders(bot_fast, self.db, self.shop), 1)

        o_fast = await self.shop.create_order(508, 4, 'stars')
        await self.db.execute('UPDATE orders SET created_at=? WHERE id=?', (in_seconds(-5 * 60), o_fast['id']))
        self.assertEqual(await dispatch_abandoned_cart_reminders(bot_fast, self.db, self.shop), 1)

        await self.db.register(509, 'fast_buy', 'FastBuy')
        o_fb = await self.shop.create_order(509, 1, 'stars')
        await self.shop.settle(o_fb['id'], 'stars', charge_id='chg-fast-buy')
        p_fb = await self.db.read('SELECT id FROM purchases WHERE order_id=?', (o_fb['id'],))
        await self.db.execute("UPDATE grants SET state='joined', joined_at=? WHERE purchase_id=?", (in_seconds(-21 * 60), p_fb['id']))
        self.assertEqual(await dispatch_trial_upsell_touchpoints(bot_fast, self.db, self.shop), 1)

    async def test_v26_upgrade_and_cro_storefront_contracts(self):
        from aurora_bot.db import DEFAULT_SETTINGS, SEED_TARIFFS, V25_SECTION_DIGESTS, V25_SEED_UPGRADES

        # 1) All v25 digest keys exist, use valid SHA-256 hex, and no public copy mentions "без звёзд"
        self.assertEqual(set(V25_SECTION_DIGESTS), {
            'join_welcome', 'welcome', 'welcome_back', 'catalog', 'help', 'info', 'promo_push_text', 'review_push_text',
        })
        for digest in V25_SECTION_DIGESTS.values():
            self.assertIsNotNone(re.fullmatch(r'[0-9a-f]{64}', digest))
        for key in V25_SECTION_DIGESTS:
            rendered = DEFAULT_SETTINGS[key].format(
                channel='Test', name='User', project_name='Aurora Access',
                bot_username='aurora_bot', code='PASS_1', discount='20', ttl_note=' (12 ч.)',
            )
            validate_section_html(rendered)
            self.assertLessEqual(_utf16_len(rendered), 950, f'v26 section {key} > 950 UTF-16')
            self.assertNotIn('без звёзд', rendered.lower())

        # 2) v25 -> v26 upgrade preserves admin edits, media_id, and tariff #1 delivery_urls
        with TemporaryDirectory() as tmp:
            v25_path = Path(tmp) / 'v25_live.sqlite3'
            v25_db = Database(v25_path)
            await v25_db.init()
            # Rewind DB back to unedited v25 values matching live snapshot
            for tid, row in enumerate(V25_SEED_UPGRADES, 1):
                await v25_db.execute(
                    'UPDATE tariffs SET title=?,description=?,rub=?,stars=?,duration_days=?,preview_description=? WHERE id=?',
                    (*row, tid),
                )
            await v25_db.execute(
                "UPDATE tariffs SET delivery_urls='[\"https://t.me/+KeepInvite\"]', media_id='t1media', media_type='photo' WHERE id=1"
            )
            await v25_db.execute("UPDATE tariffs SET rub=777, stars=400 WHERE id=2")
            await v25_db.set_setting('welcome', 'Custom welcome text {name}', media_id='w_cov', media_type='photo')
            await v25_db.set_setting('info', 'ℹ️ <b>О базе {project_name} 🔞</b>\n\nЗакрытый архив работает с 2024 года — внутри <b>65 000+ оригиналов в Full HD / 4K</b> со звуком и без водяных знаков.\n\n🔥 <b>Свежие заливы каждый день:</b> TikTok, Альтушки, OnlyFans и лички.\n⚡️ <b>Автовыдача 24/7:</b> ссылка приходит в бота за 3 секунды, оплата строго разовая.\n📊 В «🔥 Live-покупки» видна реальная лента последних входов.\n\nОткрывай «🔞 Каталог фуллов» в меню.')
            await v25_db.execute('PRAGMA user_version=25')

            await v25_db.init()
            self.assertEqual((await v25_db.read('PRAGMA user_version'))['user_version'], 29)
            self.assertEqual((await v25_db.setting('welcome'))['value'], 'Custom welcome text {name}')
            self.assertEqual((await v25_db.setting('welcome'))['media_id'], 'w_cov')
            self.assertEqual((await v25_db.setting('info'))['value'], DEFAULT_SETTINGS['info'])
            t1 = await v25_db.read('SELECT * FROM tariffs WHERE id=1')
            t2 = await v25_db.read('SELECT * FROM tariffs WHERE id=2')
            self.assertEqual((t1['title'], t1['rub'], t1['stars'], t1['duration_days']),
                             (SEED_TARIFFS[0][0], SEED_TARIFFS[0][2], SEED_TARIFFS[0][3], SEED_TARIFFS[0][4]))
            self.assertEqual((t1['delivery_urls'], t1['media_id'], t1['media_type']),
                             ('["https://t.me/+KeepInvite"]', 't1media', 'photo'))
            self.assertEqual((t2['rub'], t2['stars']), (777, 400))

        # 2b) v26 -> v27 upgrade removes personal search / order desk copy, model_url, and custom order tables while preserving #8
        with TemporaryDirectory() as tmp27:
            import sqlite3
            v26_path = Path(tmp27) / 'v26_live.sqlite3'
            v26_db = Database(v26_path)
            await v26_db.init()
            v26_db_conn = sqlite3.connect(v26_path)
            v26_db_conn.executescript("""
                CREATE TABLE custom_invoices(id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open');
                CREATE TABLE custom_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new');
                ALTER TABLE orders ADD COLUMN custom_invoice_id TEXT NOT NULL DEFAULT '';
            """)
            v26_db_conn.close()
            await v26_db.set_setting('model_url', 'https://t.me/old_model')
            await v26_db.set_setting(
                'catalog',
                '📂 <b>Закрытый каталог {project_name}</b>\n\n'
                'Выбирай нужную тему — внутри каждой карточки описание, превью и <b>моментальная выдача ссылки в чат</b> (оплата разовая, без подписок).\n\n'
                '🔥 <b>Выбор 8 из 10:</b> «Ultra VIP» сразу открывает <b>все 5 каналов навсегда</b> + закрытый «Стол заказов».\n\n'
                '👇 <b>Открой раздел, чтобы увидеть подробности:</b>',
            )
            await v26_db.set_setting(
                'help',
                '❓ <b>Частые вопросы · {project_name}</b>\n\n'
                '• <b>Когда придёт ссылка на канал?</b>\n'
                'Моментально после оплаты прямо в этот чат. Ссылка также навсегда сохранится в «👤 Профиль / Промокод» → «🔓 Мои доступы».\n\n'
                '• <b>Почему это платно, а не в открытом канале?</b>\n'
                'Открытые каналы сносят по жалобам за сутки. Здесь закрытый резерв с защищёнными зеркалами и 24/7 автовыдачей.\n\n'
                '• <b>Будут ли повторные списания?</b>\n'
                'Нет. Оплата строго разовая — без подписок и автопродлений (Telegram Stars или доступные способы).\n\n'
                '• <b>Как найти конкретное видео или модель?</b>\n'
                'После любого входа в профиле откроется «🕵️\u200d♂️ Стол заказов» под личный поиск.\n\n'
                'По оплате: /paysupport\n'
                '📄 <a href="https://t.me/{bot_username}?start=agreement">Условия покупки</a> · <a href="https://t.me/{bot_username}?start=privacy">Конфиденциальность</a>',
            )
            await v26_db.execute(
                "UPDATE tariffs SET title='🖤 Black VIP: Всё + Личный поиск', "
                "description='<b>Максимальный ALL-IN-ONE + Личный поиск моделей 💎</b>' WHERE id=7"
            )
            await v26_db.execute(
                "INSERT INTO tariffs(id,title,description,rub,stars,duration_days,active) VALUES(8,'Три сестры 🐤','Описание #8',500,250,0,1)"
            )
            await v26_db.execute('PRAGMA user_version=26')

            await v26_db.init()
            self.assertEqual((await v26_db.read('PRAGMA user_version'))['user_version'], 29)
            self.assertIsNone(await v26_db.setting('model_url'))
            self.assertIsNone(await v26_db.read("SELECT 1 FROM sqlite_master WHERE type='table' AND name='custom_invoices'"))
            self.assertIsNone(await v26_db.read("SELECT 1 FROM sqlite_master WHERE type='table' AND name='custom_requests'"))
            order_cols = {r['name'] for r in await v26_db.read('PRAGMA table_info(orders)', many=True)}
            self.assertNotIn('custom_invoice_id', order_cols)
            self.assertEqual((await v26_db.setting('catalog'))['value'], DEFAULT_SETTINGS['catalog'])
            self.assertEqual((await v26_db.setting('help'))['value'], DEFAULT_SETTINGS['help'])
            t7 = await v26_db.read('SELECT * FROM tariffs WHERE id=7')
            t8 = await v26_db.read('SELECT * FROM tariffs WHERE id=8')
            self.assertEqual(t7['title'], SEED_TARIFFS[6][0])
            self.assertEqual(t8['title'], 'Три сестры 🐤')
            for sec_key in ('catalog', 'help'):
                sec_val = DEFAULT_SETTINGS[sec_key].lower()
                self.assertNotIn('личный поиск', sec_val)
                self.assertNotIn('стол заказов', sec_val)
                self.assertNotIn('консьерж', sec_val)
            self.assertNotIn('поиск', SEED_TARIFFS[6][0].lower())
            self.assertNotIn('поиск', SEED_TARIFFS[6][1].lower())
            self.assertNotIn('консьерж', SEED_TARIFFS[6][1].lower())

        from aiogram.types import Message

        # 3) Product card preserves Telegram Stars + -35% fake strikethrough anchor & /start sends catalog automatically
        t1 = await self.shop.get_tariff(1)
        card_text = product_text(t1, t1['stars'], buyers=100, rub=t1['rub'])
        self.assertIn('190 ₽', card_text)
        self.assertIn('99 ⭐', card_text)
        self.assertNotIn('<s>', card_text)
        start_msg = Message.model_validate({
            'message_id': 77,
            'date': 1794220000,
            'from': {'id': 888, 'is_bot': False, 'first_name': 'TikTokLead', 'username': 'tt'},
            'chat': {'id': 888, 'type': 'private'},
            'text': '/start',
        })
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await welcome(start_msg, self.db, self.shop, Config('123:mock', frozenset()), NS(clear=AsyncMock()))
        # 2026 CRO: welcome + catalog are unified into 1 clean screen
        self.assertEqual(sender.await_count, 1)
        cat_btns = [b.text for row in sender.await_args_list[0].args[2].inline_keyboard for b in row]
        self.assertTrue(any('190 ₽' in txt for txt in cat_btns))


if __name__ == '__main__':
    unittest.main()
