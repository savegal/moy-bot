from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import tempfile
from time import monotonic
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
import os
import sqlite3

from aurora_bot.access import AccessManager
from aurora_bot.alerts import dispatch_whale_alerts, send_expiry_upsell_if_needed
from aurora_bot.analytics import product_sales, record_engagement
from aurora_bot.config import Config, _load_dotenv
from aurora_bot.db import Database, DEFAULT_SETTINGS, V10_SECTION_DIGESTS, V11_SECTION_DIGESTS, now, in_seconds
from aurora_bot.live_stats import _tariff_profile, get_live_recent_purchases
from aurora_bot.payments import Gateway2328
from aurora_bot.service import Shop, ShopError, UPGRADE_FLOOR_RUB, scale_stars


class ConfigurationRegressionTests(unittest.TestCase):
    def test_paths_are_anchored_to_project_and_windows_bom_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('BOT_TOKEN=123:mock\nADMIN_IDS=99\nTELEGRAM_TEST_MODE=1\n',
                                       encoding='utf-8-sig')
            with patch.dict(os.environ, {}, clear=True), patch('aurora_bot.config.PROJECT_ROOT', root):
                config = Config.from_env()
            self.assertEqual(config.db_path, str((root / 'data/bot-test.sqlite3').resolve()))
            self.assertEqual(config.admin_ids, frozenset({99}))

    def test_environment_takes_precedence_and_quotes_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('BOT_TOKEN=456:file\nKEY="value # literal"\nPORT=8080 # comment\n', encoding='utf-8')
            with patch.dict(os.environ, {'BOT_TOKEN': '123:environment'}, clear=True):
                _load_dotenv(path)
                self.assertEqual(os.environ['BOT_TOKEN'], '123:environment')
                self.assertEqual(os.environ['KEY'], 'value # literal')
                self.assertEqual(os.environ['PORT'], '8080')

    def test_invalid_config_is_rejected_before_startup(self):
        for invalid in ({'ADMIN_IDS': '-1'}, {'ADMIN_IDS': 'not an id'}, {'HTTP_PORT': '0'},
                        {'HTTP_PORT': '65536'}, {'BOT_TOKEN': 'abc:token'}, {'DB_PATH': ''}):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {
                'BOT_TOKEN': '123:mock', 'ADMIN_IDS': '99', **invalid}, clear=True), \
                    patch('aurora_bot.config._load_dotenv'):
                with self.assertRaises(ValueError):
                    Config.from_env()

    def test_invalid_webhook_base_never_enables_gateway(self):
        for url in ('https://', 'https://user:pass@example.com', 'https://example.com?q=x',
                    'https://example.com#fragment', 'https://example.com:invalid'):
            with self.subTest(url=url):
                self.assertFalse(Config('123:mock', frozenset({99}), allow_external_methods=True,
                    project_2328='project', key_2328='secret', public_base_url=url).gateway_enabled)

    def test_gateway_2328_url_env_validation(self):
        with patch.dict(os.environ, {
            'BOT_TOKEN': '123:mock', 'ADMIN_IDS': '99', 'GATEWAY_2328_URL': 'https://api.1fin.example/api/',
        }, clear=True), patch('aurora_bot.config._load_dotenv'):
            config = Config.from_env()
        self.assertEqual(config.gateway_2328_url, 'https://api.1fin.example/api')
        for bad_url in ('http://api.1fin.example/api', 'https://example.com?q=1', 'https://user:pass@example.com'):
            with self.subTest(bad_url=bad_url), patch.dict(os.environ, {
                'BOT_TOKEN': '123:mock', 'ADMIN_IDS': '99', 'GATEWAY_2328_URL': bad_url,
            }, clear=True), patch('aurora_bot.config._load_dotenv'):
                with self.assertRaises(ValueError):
                    Config.from_env()


class DomainRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / 'shop.sqlite3'))
        await self.db.init()
        await self.db.register(101, 'old_username', 'Buyer')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111',duration_days=30 WHERE id=1")
        self.shop = Shop(self.db)
        self.access = AccessManager(self.db, self.shop)
        self.bot = NS(create_chat_invite_link=AsyncMock(return_value=NS(invite_link='https://t.me/+new')),
                      revoke_chat_invite_link=AsyncMock(), send_message=AsyncMock(),
                      get_chat_member=AsyncMock(return_value=NS(status='member')), ban_chat_member=AsyncMock())

    async def paid(self, **kwargs):
        order = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(order['id'], 101, order['amount_stars'], order['id'], **kwargs)
        return order

    async def test_removed_username_is_cleared_but_registration_without_identity_preserves_it(self):
        await self.db.register(101)
        self.assertEqual((await self.shop.profile(101))['username'], 'old_username')
        await self.db.register(101, '', 'Buyer')
        self.assertEqual((await self.shop.profile(101))['username'], '')

    async def test_precheckout_deadline_does_not_reserve_after_database_wait(self):
        await self.db.execute("UPDATE promos SET enabled=1 WHERE code='LUXURYNEW'")
        await self.shop.activate_promo(101, 'LUXURYNEW')
        order = await self.shop.create_order(101, 1, 'stars')
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold_lock():
            with self.db.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                held.set()
                await release.wait()
                con.rollback()

        holder = asyncio.create_task(hold_lock())
        await held.wait()
        task = asyncio.create_task(self.shop.authorize_stars(order['id'], 101, order['amount_stars'],
                                 'late-query', deadline=monotonic() + 0.02))
        await asyncio.sleep(0.05)
        release.set()
        await holder
        self.assertFalse(await task)
        self.assertEqual((await self.shop.order(order['id']))['checkout_id'], '')

    async def test_refresh_failure_keeps_old_invite_usable(self):
        await self.paid()
        await self.access.provision(self.bot)
        grant = await self.db.read('SELECT * FROM grants')
        self.bot.create_chat_invite_link.side_effect = TimeoutError('Temporary error')
        self.assertFalse(await self.access.refresh_user(self.bot, 101, grant['id']))
        saved = await self.db.read('SELECT * FROM grants')
        self.assertEqual(saved['state'], 'ready')
        self.assertEqual(saved['invite_url'], grant['invite_url'])
        request = NS(invite_link=NS(invite_link=grant['invite_url']), chat=NS(id=-100111),
                     from_user=NS(id=101), approve=AsyncMock())
        self.assertEqual(await self.access.on_join_request(request), 'approved')
        request.approve.assert_awaited_once()

    async def test_expired_invite_is_renewed_automatically_before_notice(self):
        order = await self.paid()
        await self.access.provision(self.bot)
        await self.db.execute('UPDATE grants SET invite_expires_at=?', (in_seconds(-1),))
        await self.db.execute('UPDATE orders SET notice_sent=0 WHERE id=?', (order['id'],))
        self.bot.create_chat_invite_link.return_value = NS(invite_link='https://t.me/+renewed')
        await self.access.provision(self.bot)
        self.assertEqual(self.bot.create_chat_invite_link.await_count, 2)
        grant = await self.db.read('SELECT * FROM grants')
        self.assertEqual(grant['invite_url'], 'https://t.me/+renewed')
        self.assertGreater(grant['invite_expires_at'], now())
        self.assertEqual((await self.shop.order(order['id']))['notice_sent'], 1)

    async def test_failed_expired_invite_renewal_does_not_send_ready_notice(self):
        order = await self.paid()
        await self.access.provision(self.bot)
        self.bot.send_message.reset_mock()
        await self.db.execute('UPDATE grants SET invite_expires_at=?', (in_seconds(-1),))
        await self.db.execute('UPDATE orders SET notice_sent=0 WHERE id=?', (order['id'],))
        self.bot.create_chat_invite_link.side_effect = TimeoutError('Temporary error')
        await self.access.provision(self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_failed_member_removal_is_recorded_and_cleared_on_retry(self):
        await self.paid()
        await self.access.provision(self.bot)
        await self.db.execute('UPDATE purchases SET expires_at=?', (in_seconds(-1),))
        self.bot.ban_chat_member.side_effect = RuntimeError('Missing permission')
        await self.access.revoke_expired(self.bot)
        grant = await self.db.read('SELECT * FROM grants')
        self.assertEqual(grant['last_error'], 'Missing permission')
        self.assertIsNotNone(grant['revoke_retry_at'])
        self.bot.ban_chat_member.side_effect = None
        await self.db.execute('UPDATE grants SET revoke_retry_at=NULL')
        await self.access.revoke_expired(self.bot)
        grant = await self.db.read('SELECT * FROM grants')
        self.assertEqual(grant['state'], 'expired')
        self.assertEqual(grant['last_error'], '')

    async def test_administrator_membership_is_reported_as_failed_removal(self):
        await self.paid()
        await self.access.provision(self.bot)
        await self.db.execute('UPDATE purchases SET expires_at=?', (in_seconds(-1),))
        for status in ('administrator', 'creator'):
            with self.subTest(status=status):
                self.bot.get_chat_member.return_value = NS(status=status)
                await self.db.execute('UPDATE grants SET revoke_retry_at=NULL')
                await self.access.revoke_expired(self.bot)
                grant = await self.db.read('SELECT * FROM grants')
                self.assertNotEqual(grant['state'], 'expired')
                self.assertIsNotNone(grant['revoke_retry_at'])
                self.assertIn('администратора', grant['last_error'])
                self.bot.ban_chat_member.assert_not_awaited()

    async def test_refund_pause_is_excluded_from_active_grants(self):
        order = await self.paid()
        await self.access.provision(self.bot)
        self.assertEqual((await self.shop.statistics())['grants'], 1)
        await self.db.execute('UPDATE orders SET refund_requested_at=? WHERE id=?', (now(), order['id']))
        self.assertEqual((await self.shop.statistics())['grants'], 0)

    async def test_recovered_payment_uses_transaction_date_for_sales_but_starts_access_now(self):
        paid_at = datetime.now(timezone.utc) - timedelta(days=10)
        order = await self.paid(paid_at=paid_at)
        current = await self.shop.order(order['id'])
        self.assertEqual(current['paid_at'], paid_at.isoformat(timespec='seconds'))
        self.assertEqual(await product_sales(self.db, 7, 10, 0), [])
        self.assertEqual(len(await product_sales(self.db, 30, 10, 0)), 1)
        purchase = (await self.shop.purchases(101))[0]
        self.assertGreater(purchase['started_at'], current['paid_at'])
        self.assertGreater(purchase['expires_at'], in_seconds(29 * 86400))

    async def test_recent_orders_break_same_second_ties_by_newest_order(self):
        for index in range(4):
            await self.db.execute('UPDATE tariffs SET title=? WHERE id=1', (f'Product {index}',))
            await self.paid(paid_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([title for title, _ in await self.shop.get_recent_purchases()],
                         ['Product 3', 'Product 2', 'Product 1'])

    async def test_ledger_corrects_existing_payment_date_without_duplicate_access(self):
        order = await self.paid()
        purchase = (await self.shop.purchases(101))[0]
        historical = datetime.now(timezone.utc) - timedelta(days=10)
        self.assertFalse(await self.shop.settle_stars(order['id'], 101, order['amount_stars'], order['id'],
                                                     paid_at=historical))
        self.assertEqual((await self.shop.order(order['id']))['paid_at'], historical.isoformat(timespec='seconds'))
        self.assertEqual(await self.shop.purchases(101), [purchase])
        self.assertEqual(await product_sales(self.db, 7, 10, 0), [])

    async def test_v5_migration_preserves_custom_sections_prices_and_media(self):
        legacy = ('ℹ️ <b>О проекте {project_name}</b>\n\n'
            '{project_name} — крупнейшая сеть закрытых медиаканалов с 2024 года.\n\n'
            '✅ <b>100% реальный контент:</b> никаких пустых каналов, битых ссылок и фейков.\n'
            '✅ <b>Ежедневный залив:</b> самые свежие сливы и приватные архивы 24/7.\n'
            '✅ <b>Честная статистика:</b> все данные в разделе «Статистика» фиксируются в реальном времени.\n\n'
            'Актуальные цены и тарифы доступны в разделе «🔐 Доступ к контенту».')
        await self.db.set_setting('info', legacy, media_id='cover', media_type='photo')
        await self.db.set_setting('welcome', 'My custom welcome')
        await self.db.execute('UPDATE tariffs SET rub=777,stars=333 WHERE id=1')
        await self.db.execute('PRAGMA user_version=4')
        await self.db.init()
        self.assertEqual((await self.db.setting('info'))['value'], DEFAULT_SETTINGS['info'])
        self.assertEqual((await self.db.setting('info'))['media_id'], 'cover')
        self.assertEqual((await self.db.setting('welcome'))['value'], 'My custom welcome')
        self.assertEqual((await self.shop.get_tariff(1))['stars'], 333)

    async def test_recent_interaction_keeps_attribution_from_old_delivered_greeting(self):
        event_id = 'a' * 32
        await self.db.execute("""INSERT INTO greeting_events(id,chat_id,user_id,requested_at,sent_at,
            engaged_at,touched_at) VALUES(?,?,?,?,?,?,?)""", (event_id, -100999, 101,
            int((datetime.now(timezone.utc) - timedelta(days=31)).timestamp()),
            in_seconds(-31 * 86400), in_seconds(-2 * 86400), in_seconds(-2 * 86400)))
        order = await self.shop.create_order(101, 1, 'stars')
        self.assertEqual(order['greeting_event_id'], event_id)

    async def test_click_on_old_delivered_greeting_can_start_new_attribution_window(self):
        event_id = 'b' * 32
        await self.db.execute("""INSERT INTO greeting_events(id,chat_id,user_id,requested_at,sent_at)
            VALUES(?,?,?,?,?)""", (event_id, -100999, 101,
            int((datetime.now(timezone.utc) - timedelta(days=60)).timestamp()), in_seconds(-60 * 86400)))
        await record_engagement(self.db, 101, 'g_' + event_id)
        event = await self.db.read('SELECT * FROM greeting_events WHERE id=?', (event_id,))
        self.assertIsNotNone(event['engaged_at'])
        order = await self.shop.create_order(101, 1, 'stars')
        self.assertEqual(order['greeting_event_id'], event_id)

    async def test_gateway_2328_routes_to_default_and_custom_1fin_url(self):
        default_cfg = Config('123:mock', frozenset({99}), project_2328='proj', key_2328='sec')
        custom_cfg = Config('123:mock', frozenset({99}), project_2328='proj', key_2328='sec',
                            gateway_2328_url='https://api.1fin.example/api/')
        with patch('aurora_bot.payments._json_post', new=AsyncMock(return_value={'state': 0, 'result': {'ok': 1}})) as post:
            await Gateway2328(default_cfg).api('v1/payment', {'a': 1})
            self.assertEqual(post.await_args.args[0], 'https://api.2328.io/api/v1/payment')
            await Gateway2328(custom_cfg).api('v1/payment', {'a': 1})
            self.assertEqual(post.await_args.args[0], 'https://api.1fin.example/api/v1/payment')

    async def test_v11_upgrade_updates_unedited_sections_and_promo_without_touching_custom_data(self):
        self.assertEqual(set(V10_SECTION_DIGESTS), {'join_welcome', 'welcome', 'welcome_back', 'catalog', 'help', 'info'})
        for digest in V10_SECTION_DIGESTS.values():
            self.assertIsNotNone(re.fullmatch(r'[0-9a-f]{64}', digest))

        legacy_welcome = 'Legacy v10 welcome text'
        await self.db.set_setting('welcome', legacy_welcome, media_id='wcover', media_type='photo')
        await self.db.set_setting('info', 'My custom info', media_id='icover', media_type='photo')
        await self.db.execute("UPDATE promos SET discount=200 WHERE code='LUXURYNEW'")
        await self.db.execute("INSERT INTO promos(code,discount,max_uses) VALUES('KEEP200',200,10)")
        await self.db.execute('UPDATE tariffs SET rub=777,stars=333 WHERE id=1')
        await self.db.execute('PRAGMA user_version=10')

        patched_digests = {'welcome': hashlib.sha256(legacy_welcome.encode('utf8')).hexdigest(), 'info': '0' * 64}
        with patch.dict('aurora_bot.db.V10_SECTION_DIGESTS', patched_digests, clear=True):
            await self.db.init()

        self.assertEqual((await self.db.setting('welcome'))['value'], DEFAULT_SETTINGS['welcome'])
        self.assertEqual((await self.db.setting('welcome'))['media_id'], 'wcover')
        self.assertEqual((await self.db.setting('info'))['value'], 'My custom info')
        self.assertEqual((await self.db.setting('info'))['media_id'], 'icover')
        self.assertEqual((await self.db.read("SELECT discount FROM promos WHERE code='LUXURYNEW'"))['discount'], 150)
        self.assertEqual((await self.db.read("SELECT discount FROM promos WHERE code='KEEP200'"))['discount'], 200)
        self.assertEqual((await self.shop.get_tariff(1))['stars'], 333)
        self.assertEqual((await self.db.read('PRAGMA user_version'))['user_version'], 28)

        await self.db.execute("UPDATE promos SET discount=250 WHERE code='LUXURYNEW'")
        await self.db.execute('PRAGMA user_version=10')
        await self.db.init()
        self.assertEqual((await self.db.read("SELECT discount FROM promos WHERE code='LUXURYNEW'"))['discount'], 250)

    async def test_v15_upgrade_updates_unedited_v12_sections_and_seed_tariffs_preserving_custom_data(self):
        from aurora_bot.db import SEED_TARIFFS, V12_SECTION_DIGESTS, V12_SEED_UPGRADES, V18_SEED_UPGRADES
        from aurora_bot.ui import product_text, validate_section_html

        self.assertEqual(set(V12_SECTION_DIGESTS), {'join_welcome', 'welcome', 'welcome_back', 'catalog', 'help'})
        for digest in V12_SECTION_DIGESTS.values():
            self.assertIsNotNone(re.fullmatch(r'[0-9a-f]{64}', digest))
        self.assertEqual(len(V12_SEED_UPGRADES), len(V18_SEED_UPGRADES))
        self.assertEqual(len(SEED_TARIFFS), 7)

        for key in V12_SECTION_DIGESTS:
            rendered = DEFAULT_SETTINGS[key].format(
                channel='Test Channel',
                name='Tester',
                project_name='Aurora Access',
                bot_username='aurora_bot',
            )
            validate_section_html(rendered)
            self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 950, f'{key} exceeds 950 UTF-16 units')

        for idx, (title, desc, rub, stars, days, prev) in enumerate(SEED_TARIFFS, 1):
            base = {
                'id': idx,
                'title': title,
                'description': desc,
                'preview_description': prev,
                'rub': rub,
                'stars': stars,
                'duration_days': days,
            }
            for body_key, buyers_arg in (('description', 1200), ('preview', None)):
                txt = product_text(
                    base,
                    max(stars - 75, 1),
                    buyers=buyers_arg,
                    promo='LUXURYNEW',
                    rub=max(rub - 150, 1),
                )
                validate_section_html(txt)
                self.assertLessEqual(len(txt.encode('utf-16-le')) // 2, 950, f'tariff {idx} {body_key} > 950')

        legacy_welcome = 'Legacy v12 welcome text'
        await self.db.set_setting('project_name', 'Aurora Access')
        await self.db.set_setting('welcome', legacy_welcome, media_id='wcover', media_type='photo')
        await self.db.set_setting('help', 'My custom help', media_id='hcover', media_type='photo')
        t1_old_title, t1_old_prev = V12_SEED_UPGRADES[0]
        t2_old_title, _ = V12_SEED_UPGRADES[1]
        t5_old_title, t5_old_prev = V12_SEED_UPGRADES[4]
        await self.db.execute(
            "UPDATE tariffs SET title=?,preview_description=?,channel_ids='[-100999]',media_id='t1media',media_type='photo' WHERE id=1",
            (t1_old_title, t1_old_prev),
        )
        await self.db.execute(
            'UPDATE tariffs SET title=?,preview_description=?,rub=777,stars=333 WHERE id=2',
            (t2_old_title, 'Custom preview'),
        )
        await self.db.execute(
            'UPDATE tariffs SET title=?,preview_description=? WHERE id=5',
            (t5_old_title, t5_old_prev),
        )
        await self.db.execute('PRAGMA user_version=14')

        patched = {'welcome': hashlib.sha256(legacy_welcome.encode('utf8')).hexdigest(), 'help': '0' * 64}
        with patch.dict('aurora_bot.db.V12_SECTION_DIGESTS', patched, clear=True):
            await self.db.init()

        self.assertEqual((await self.db.setting('project_name'))['value'], 'Aurora Access')
        self.assertEqual((await self.db.setting('welcome'))['value'], DEFAULT_SETTINGS['welcome'])
        self.assertEqual((await self.db.setting('welcome'))['media_id'], 'wcover')
        self.assertEqual((await self.db.setting('help'))['value'], 'My custom help')
        self.assertEqual((await self.db.setting('help'))['media_id'], 'hcover')

        t1 = await self.shop.get_tariff(1)
        t2 = await self.shop.get_tariff(2)
        t5 = await self.shop.get_tariff(5)
        self.assertEqual(t1['title'], SEED_TARIFFS[0][0])
        self.assertEqual(t1['preview_description'], SEED_TARIFFS[0][5])
        self.assertEqual((t1['channel_ids'], t1['media_id'], t1['media_type']), ('[-100999]', 't1media', 'photo'))
        self.assertEqual(t2['title'], SEED_TARIFFS[1][0])
        self.assertEqual((t2['preview_description'], t2['rub'], t2['stars']), ('Custom preview', 777, 333))
        self.assertEqual((t5['title'], t5['preview_description'], t5['tier']), (SEED_TARIFFS[4][0], SEED_TARIFFS[4][5], 'ultra'))
        self.assertEqual((await self.db.read('PRAGMA user_version'))['user_version'], 28)

    async def test_v12_upgrade_updates_hybrid_sections_seed_prices_and_disables_default_promo(self):
        self.assertEqual(set(V11_SECTION_DIGESTS), {'join_welcome', 'welcome', 'welcome_back', 'catalog'})
        for digest in V11_SECTION_DIGESTS.values():
            self.assertIsNotNone(re.fullmatch(r'[0-9a-f]{64}', digest))

        legacy_welcome = 'Legacy v11 welcome text'
        await self.db.set_setting('welcome', legacy_welcome, media_id='wcover', media_type='photo')
        await self.db.set_setting('catalog', 'My custom catalog', media_id='ccover', media_type='photo')
        await self.db.execute(
            "UPDATE tariffs SET title='🔥 ТОТ САМЫЙ ФУЛЛ ИЗ TIKTOK + ХИТЫ [24 ЧАСА] 🔞',rub=440,stars=230 WHERE id=1"
        )
        await self.db.execute(
            "UPDATE tariffs SET title='🎀 СОЧНЫЕ АЛЬТУШКИ, СТРИМЕРШИ & TIKTOK [18+]',rub=777,stars=333 WHERE id=2"
        )
        await self.db.execute(
            "UPDATE tariffs SET title='⭐️ 🏆 ULTRA VIP: ВСЕ 5 КАНАЛОВ СРАЗУ [ТОП]',rub=1190,stars=620 WHERE id=5"
        )
        await self.db.execute("UPDATE promos SET enabled=1 WHERE code='LUXURYNEW'")
        await self.db.execute("INSERT INTO promos(code,discount,max_uses,enabled) VALUES('HOT300',300,15,1)")
        await self.db.execute('PRAGMA user_version=11')

        patched = {'welcome': hashlib.sha256(legacy_welcome.encode('utf8')).hexdigest(), 'catalog': '0' * 64}
        with patch.dict('aurora_bot.db.V11_SECTION_DIGESTS', patched, clear=True):
            await self.db.init()

        self.assertEqual((await self.db.setting('welcome'))['value'], DEFAULT_SETTINGS['welcome'])
        self.assertEqual((await self.db.setting('welcome'))['media_id'], 'wcover')
        self.assertEqual((await self.db.setting('catalog'))['value'], 'My custom catalog')
        self.assertEqual((await self.db.setting('catalog'))['media_id'], 'ccover')
        self.assertEqual(((await self.shop.get_tariff(1))['rub'], (await self.shop.get_tariff(1))['stars']), (240, 125))
        self.assertEqual(((await self.shop.get_tariff(2))['rub'], (await self.shop.get_tariff(2))['stars']), (777, 333))
        self.assertEqual(((await self.shop.get_tariff(5))['rub'], (await self.shop.get_tariff(5))['stars']), (1190, 620))
        self.assertEqual((await self.db.read("SELECT enabled FROM promos WHERE code='LUXURYNEW'"))['enabled'], 0)
        self.assertEqual((await self.db.read("SELECT enabled FROM promos WHERE code='HOT300'"))['enabled'], 1)
        self.assertEqual((await self.db.read('PRAGMA user_version'))['user_version'], 28)

        await self.db.execute("UPDATE promos SET discount=200,enabled=1 WHERE code='LUXURYNEW'")
        await self.db.execute('PRAGMA user_version=11')
        await self.db.init()
        self.assertEqual((await self.db.read("SELECT enabled FROM promos WHERE code='LUXURYNEW'"))['enabled'], 1)

    async def test_v13_upgrade_rebuilds_orders_table_with_nullable_tariff_id(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = str(Path(directory) / 'legacy_v12.sqlite3')
            con = sqlite3.connect(legacy_path)
            con.executescript('''
                PRAGMA foreign_keys=ON;
                CREATE TABLE users(
                    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, last_seen TEXT NOT NULL,
                    active_promo TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE tariffs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', rub INTEGER NOT NULL,
                    stars INTEGER NOT NULL, duration_days INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1, channel_ids TEXT NOT NULL DEFAULT '',
                    media_id TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE orders(
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, tariff_id INTEGER NOT NULL,
                    title TEXT NOT NULL, amount_rub INTEGER NOT NULL, amount_stars INTEGER NOT NULL,
                    duration_days INTEGER NOT NULL, channels TEXT NOT NULL,
                    promo_code TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL, external_id TEXT NOT NULL DEFAULT '',
                    payment_url TEXT NOT NULL DEFAULT '', charge_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, paid_at TEXT,
                    notice_sent INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(user_id),
                    FOREIGN KEY(tariff_id) REFERENCES tariffs(id)
                );
                CREATE TABLE purchases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL, tariff_id INTEGER NOT NULL,
                    title TEXT NOT NULL, started_at TEXT NOT NULL, expires_at TEXT,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );
                CREATE TABLE grants(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, purchase_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    invite_url TEXT NOT NULL DEFAULT '', invite_expires_at TEXT,
                    state TEXT NOT NULL DEFAULT 'need_link',
                    last_error TEXT NOT NULL DEFAULT '', joined_at TEXT, processing_at TEXT,
                    UNIQUE(purchase_id,chat_id),
                    FOREIGN KEY(purchase_id) REFERENCES purchases(id)
                );
                INSERT INTO users(user_id, username, name, created_at, last_seen)
                    VALUES(505, 'legacy', 'Legacy', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
                INSERT INTO tariffs(id, title, rub, stars, duration_days, channel_ids)
                    VALUES(1, 'VIP Legacy', 500, 250, 30, '-100111');
                INSERT INTO orders(id, user_id, tariff_id, title, amount_rub, amount_stars, duration_days, channels, provider, status, created_at, expires_at, paid_at)
                    VALUES('ord1', 505, 1, 'VIP Legacy', 500, 250, 30, '[-100111]', 'stars', 'paid', '2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00', '2026-01-01T00:05:00+00:00');
                INSERT INTO purchases(id, order_id, user_id, tariff_id, title, started_at, expires_at)
                    VALUES(1, 'ord1', 505, 1, 'VIP Legacy', '2026-01-01T00:05:00+00:00', '2026-02-01T00:05:00+00:00');
                INSERT INTO grants(purchase_id, user_id, chat_id, state) VALUES(1, 505, -100111, 'joined');
                PRAGMA user_version=12;
            ''')
            con.close()

            migrated = Database(legacy_path)
            await migrated.init()
            self.assertEqual((await migrated.read('PRAGMA user_version'))['user_version'], 28)
            self.assertIsNone(await migrated.setting('model_url'))
            self.assertIsNone(await migrated.read("SELECT 1 FROM sqlite_master WHERE type='table' AND name='custom_invoices'"))
            self.assertIsNone(await migrated.read("SELECT 1 FROM sqlite_master WHERE type='table' AND name='custom_requests'"))
            cols = {row['name']: row for row in await migrated.read('PRAGMA table_info(orders)', many=True)}
            self.assertEqual(cols['tariff_id']['notnull'], 0)
            self.assertIn('order_kind', cols)
            self.assertNotIn('custom_invoice_id', cols)
            self.assertIn('whale_alert_sent', cols)
            self.assertEqual((await migrated.read("SELECT title FROM orders WHERE id='ord1'"))['title'], 'VIP Legacy')
            self.assertEqual((await migrated.read("SELECT whale_alert_sent FROM orders WHERE id='ord1'"))['whale_alert_sent'], 1)
            self.assertEqual(await migrated.read('PRAGMA foreign_key_check', many=True), [])

    async def test_upgrade_offers_credit_floor_and_ownership_rules(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='-100555' WHERE id IN (2, 3, 4, 5, 6, 7)")
        await self.db.register(303, 'free', 'Admin Grant')
        admin_order = await self.shop.create_order(303, 1, 'admin', duration_days=7)
        await self.shop.settle(admin_order['id'], 'admin')
        self.assertEqual(await self.shop.upgrade_offers(303), [])
        self.assertFalse(await self.shop.is_paying_user(303))

        # Base entry (240 RUB) -> ULTRA VIP = 950 RUB, DIAMOND CLUB = 2250 RUB
        trial = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle(trial['id'], 'stars', charge_id='chg-trial')
        offers = await self.shop.upgrade_offers(101)
        by_kind = {item['kind']: item for item in offers}
        self.assertEqual(set(by_kind), {'ULTRA', 'DIAMOND'})
        self.assertEqual(by_kind['ULTRA']['rub'], 1190 - 240)
        self.assertEqual(by_kind['ULTRA']['stars'], scale_stars(1190, 620, 950))
        self.assertEqual(by_kind['DIAMOND']['rub'], 2490 - 240)

        # Upgrade to ULTRA VIP -> only DIAMOND remains for 1300 RUB (credit = 240 + 950 = 1190)
        up_ultra = await self.shop.create_upgrade_order(101, by_kind['ULTRA']['tariff']['id'], '2328')
        await self.shop.set_external(up_ultra['id'], '2328', 'ext-ultra', 'https://gw/ultra')
        self.assertTrue(await self.shop.settle(up_ultra['id'], '2328', external_id='ext-ultra'))
        after_ultra = {item['kind']: item for item in await self.shop.upgrade_offers(101)}
        self.assertEqual(set(after_ultra), {'DIAMOND'})
        self.assertEqual(after_ultra['DIAMOND']['rub'], 2490 - 1190)

        # Black VIP high-anchor buyer never receives nonsensical lower-tier "upgrade" offers
        await self.db.register(505, 'black', 'Black')
        black_ord = await self.shop.create_order(505, 7, 'stars')
        await self.shop.settle(black_ord['id'], 'stars', charge_id='chg-black')
        self.assertEqual(await self.shop.upgrade_offers(505), [])

        # Whale paying multiple 30d tariffs hits the 300 RUB upgrade floor
        await self.db.register(404, 'whale', 'Whale')
        for idx, tid in enumerate((2, 3, 4)):
            ord_row = await self.shop.create_order(404, tid, 'stars')
            await self.shop.settle(ord_row['id'], 'stars', charge_id=f'w-{idx}')
        whale_offers = {item['kind']: item for item in await self.shop.upgrade_offers(404)}
        self.assertEqual(whale_offers['ULTRA']['rub'], UPGRADE_FLOOR_RUB)
        self.assertEqual(whale_offers['DIAMOND']['rub'], UPGRADE_FLOOR_RUB)

        # Buying DIAMOND removes all upgrade offers; stale open Stars upgrade invoice cannot authorize after ownership
        stale_ultra = await self.shop.create_upgrade_order(404, 5, 'stars')
        up_diamond = await self.shop.create_upgrade_order(404, whale_offers['DIAMOND']['tariff']['id'], 'stars')
        await self.shop.settle(up_diamond['id'], 'stars', charge_id='w-diamond')
        self.assertEqual(await self.shop.upgrade_offers(404), [])
        self.assertFalse(await self.shop.authorize_stars(stale_ultra['id'], 404, stale_ultra['amount_stars'], 'chk-stale-up'))

    async def test_v14_tier_migration_keeps_renamed_ultra_diamond_and_promo_holds_dedup_user(self):
        # Fresh DB sets explicit flagship tier on seeded #5 ('ultra') and #6 ('diamond')
        t5 = await self.shop.get_tariff(5)
        t6 = await self.shop.get_tariff(6)
        self.assertEqual((t5['tier'], t6['tier']), ('ultra', 'diamond'))

        # Renaming #5 without 'ULTRA' in the title preserves upgrade offers, whale radar, expiry suppression & stats profile
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id IN (1, 5, 6)")
        await self.db.execute("UPDATE tariffs SET title='🔥 КЛУБ НАВСЕГДА [ТОП]' WHERE id=5")
        renamed = await self.shop.get_tariff(5)
        self.assertEqual(_tariff_profile(renamed)[0], 'ultra:5')

        o_trial = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle(o_trial['id'], 'stars', charge_id='chg-ren-trial')
        offers = {item['kind']: item for item in await self.shop.upgrade_offers(101)}
        self.assertIn('ULTRA', offers)
        self.assertEqual(offers['ULTRA']['tariff']['id'], 5)

        o_up = await self.shop.create_upgrade_order(101, 5, 'stars')
        await self.shop.settle(o_up['id'], 'stars', charge_id='chg-ren-ultra')
        # Whale radar flags renamed #5 payment as VIP-тариф
        cfg = Config('123:mock', frozenset({901}))
        bot = NS(send_message=AsyncMock())
        self.assertEqual(await dispatch_whale_alerts(bot, self.db, cfg), 1)
        self.assertIn('VIP-тариф', bot.send_message.await_args.args[1])

        # Expiring trial purchase does not nag user who owns renamed tier='ultra'
        purchase = await self.db.read('SELECT id FROM purchases WHERE order_id=?', (o_trial['id'],))
        await self.db.execute("UPDATE grants SET state='expired' WHERE purchase_id=?", (purchase['id'],))
        self.assertFalse(await send_expiry_upsell_if_needed(bot, self.db, self.shop, purchase['id']))

        # Deduplicated promo holds: 1 user with 2 pending external drafts consumes 1 slot (not 2)
        await self.db.register(202)
        await self.db.execute("INSERT INTO promos(code,discount,max_uses,enabled) VALUES('LIMITED2',100,2,1)")
        await self.shop.activate_promo(101, 'LIMITED2')
        ext1 = await self.shop.create_order(101, 1, '2328')
        ext2 = await self.shop.create_order(101, 1, 'cryptopay')
        await self.db.execute(
            "UPDATE orders SET external_id='e1', payment_url='https://pay.example/1' WHERE id=?",
            (ext1['id'],),
        )
        await self.db.execute(
            "UPDATE orders SET external_id='e2', payment_url='https://pay.example/2' WHERE id=?",
            (ext2['id'],),
        )
        await self.shop.activate_promo(202, 'LIMITED2')
        st2 = await self.shop.create_order(202, 1, 'stars')
        self.assertTrue(await self.shop.authorize_stars(st2['id'], 202, st2['amount_stars'], 'chk-dedup'))

        # v14 migration marks historical pre-v14 expired/paid orders with cart_reminder_sent=2 so new carts remain eligible
        await self.db.execute("UPDATE orders SET created_at=?, status='expired', cart_reminder_sent=0 WHERE id=?",
                              (in_seconds(-10 * 3600), ext1['id']))
        await self.db.execute('PRAGMA user_version=13')
        await self.db.init()
        self.assertEqual((await self.db.read('SELECT cart_reminder_sent FROM orders WHERE id=?', (ext1['id'],)))['cart_reminder_sent'], 2)

    async def test_v22_upgrade_updates_unedited_v21_join_welcome_and_preserves_custom(self):
        from aurora_bot.db import DEFAULT_SETTINGS

        old_v21_join_welcome = (
            "🔞 <b>Доступ к «{channel}» закрыт</b>\n\n"
            "👤 Заявка от <b>{name}</b> · <b>оригиналы Full HD со звуком</b> уже в базе ✅\n\n"
            "Открытые каналы банят за сутки, поэтому фуллы из TikTok и приватные сливы "
            "мы перенесли в закрытого бота с автовыдачей.\n\n"
            "👇 <b>Подтверди 18+, чтобы открыть доступ:</b>"
        )
        await self.db.set_setting('join_welcome', old_v21_join_welcome, media_id='jcover', media_type='photo')
        await self.db.execute('PRAGMA user_version=21')
        await self.db.init()
        self.assertEqual((await self.db.read('PRAGMA user_version'))['user_version'], 28)
        self.assertEqual((await self.db.setting('join_welcome'))['value'], DEFAULT_SETTINGS['join_welcome'])
        self.assertEqual((await self.db.setting('join_welcome'))['media_id'], 'jcover')

        await self.db.set_setting('join_welcome', 'Custom join welcome {channel}', media_id='jcover', media_type='photo')
        await self.db.execute('PRAGMA user_version=21')
        await self.db.init()
        self.assertEqual((await self.db.setting('join_welcome'))['value'], 'Custom join welcome {channel}')
        self.assertEqual((await self.db.setting('join_welcome'))['media_id'], 'jcover')

    async def test_v28_upgrade_updates_unedited_v27_join_welcome_and_preserves_custom(self):
        from aurora_bot.db import DEFAULT_SETTINGS, V27_SECTION_DIGESTS

        self.assertEqual(set(V27_SECTION_DIGESTS), {'join_welcome'})
        old_v27_join_welcome = (
            "🛡 <b>Резервное зеркало для «{channel}» готово</b>\n\n"
            "👤 <b>{name}</b>, твоя заявка зафиксирована ✅\n\n"
            "Открытые каналы сносят по жалобам за сутки — чтобы не потерять базу, все оригиналы из TikTok "
            "и приватные сливы со звуком <b>выгружены в закрытого бота с автовыдачей</b>.\n\n"
            "👇 <b>Забирай вход в закрытый резерв, пока зеркало активно:</b>"
        )
        self.assertEqual(
            hashlib.sha256(old_v27_join_welcome.encode('utf8')).hexdigest(),
            V27_SECTION_DIGESTS['join_welcome'],
        )
        await self.db.set_setting('join_welcome', old_v27_join_welcome, media_id='jcover27', media_type='photo')
        await self.db.execute('PRAGMA user_version=27')
        await self.db.init()
        self.assertEqual((await self.db.read('PRAGMA user_version'))['user_version'], 28)
        self.assertEqual((await self.db.setting('join_welcome'))['value'], DEFAULT_SETTINGS['join_welcome'])
        self.assertEqual((await self.db.setting('join_welcome'))['media_id'], 'jcover27')

        await self.db.set_setting('join_welcome', 'Custom v27 join welcome', media_id='jcover27', media_type='photo')
        await self.db.execute('PRAGMA user_version=27')
        await self.db.init()
        self.assertEqual((await self.db.setting('join_welcome'))['value'], 'Custom v27 join welcome')
        self.assertEqual((await self.db.setting('join_welcome'))['media_id'], 'jcover27')


if __name__ == '__main__':
    unittest.main()
