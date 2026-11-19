from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import Message

from aurora_bot.db import Database, DEFAULT_SETTINGS
from aurora_bot.service import Shop
from aurora_bot.legal import get_terms_of_service, get_privacy_policy
from aurora_bot.ui import (
    button_tariff_name, menu, duration, product_text, rendered_section, validate_section_html,
    MENU_CATALOG, MENU_PROFILE, MENU_HELP, MENU_INFO,
)
from aurora_bot.handlers_user import (
    get_live_recent_purchases, info_message, promo_finish, render_help_message,
    show_catalog, tariff_card, tariff_preview,
)
from aurora_bot.live_stats import get_live_tariff_buyers


class LegalDocumentTests(unittest.TestCase):
    def test_terms_of_service(self):
        tos = get_terms_of_service("MySuperProject")
        self.assertIn("Пользовательское соглашение MySuperProject", tos)
        self.assertIn("сервиса MySuperProject", tos)
        self.assertIn("Ограничение ответственности", tos)
        self.assertIn("💬 Помощь / Отзывы", tos)

        tos_hidden = get_terms_of_service("MySuperProject", help_label=None)
        self.assertNotIn("в нижнем меню бота", tos_hidden)
        self.assertIn("/paysupport", tos_hidden)

    def test_privacy_policy(self):
        pp = get_privacy_policy("MySuperProject")
        self.assertIn("Политика конфиденциальности", pp)
        self.assertIn("сервиса MySuperProject", pp)
        self.assertIn("Сбор информации", pp)


class MenuAndUITests(unittest.TestCase):
    def test_menu_structure_and_emojis(self):
        kb = menu("LuxuryHub")
        buttons = [[b.text for b in row] for row in kb.keyboard]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0], [MENU_CATALOG, MENU_PROFILE])
        self.assertEqual(buttons[1], [MENU_HELP, MENU_INFO])
        self.assertEqual(buttons[2], ["🔥 Live-покупки LuxuryHub"])
        self.assertEqual([[b.style for b in row] for row in kb.keyboard],
                         [['success', None], [None, None], [None]])

        kb_no_help = menu("LuxuryHub", show_help=False)
        self.assertEqual(
            [[b.text for b in row] for row in kb_no_help.keyboard],
            [[MENU_CATALOG, MENU_PROFILE], [MENU_INFO], ["🔥 Live-покупки LuxuryHub"]],
        )

    def test_duration_forever(self):
        self.assertEqual(duration(0), "НАВСЕГДА")
        self.assertEqual(duration(30), "30 дн.")


class RecentPurchasesAndStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "test.sqlite3")
        self.db = Database(self.path)
        await self.db.init()
        self.shop = Shop(self.db)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_tariffs_have_emojis_and_luxurynew_promo(self):
        tariffs = await self.shop.catalogs()
        self.assertEqual(len(tariffs), 7)
        for t in tariffs:
            self.assertTrue(any(ch in t["title"] for ch in ("⚡", "🔥", "🎀", "🍑", "😈", "⭐️", "🏆", "👑", "🖤")))
        self.assertEqual([(t["rub"], t["stars"], t["duration_days"]) for t in tariffs], [
            (240, 125, 0),
            (690, 360, 30),
            (790, 410, 30),
            (890, 460, 30),
            (1190, 620, 0),
            (2490, 1290, 0),
            (4990, 2590, 0),
        ])
        self.assertIn('Ultra VIP', tariffs[4]["title"])
        self.assertIn('[ХИТ]', tariffs[4]["title"])
        self.assertIn('Diamond Club', tariffs[5]["title"])
        self.assertIn('Black VIP', tariffs[6]["title"])
        for t in tariffs:
            self.assertLessEqual(len(t["title"]), 38)

        # Check dormant broadcast promo stays in DB disabled on entry
        promo = await self.db.read("SELECT * FROM promos WHERE code='LUXURYNEW'")
        self.assertIsNotNone(promo)
        self.assertEqual(promo["discount"], 150)
        self.assertEqual(promo["enabled"], 0)

    def message(self):
        return Message.model_validate({'message_id': 1, 'date': 1794220000,
            'from': {'id': 101, 'is_bot': False, 'first_name': '<Alice>'},
            'chat': {'id': 101, 'type': 'private'}, 'text': 'menu'})

    async def test_public_sections_render_project_name_and_keep_media(self):
        await self.db.set_setting('project_name', 'Project <A&B>')
        await self.db.set_setting('info', media_id='cover', media_type='photo')
        await self.db.set_setting('catalog', 'Catalog {project_name}, {name}')
        message = self.message()
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='test_bot')))
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as send:
            await info_message(message, self.db)
            row = send.call_args.args[1]
            self.assertIn('Project &lt;A&amp;B&gt;', row['value'])
            self.assertNotIn('{project_name}', row['value'])
            self.assertEqual(row['media_id'], 'cover')
            await render_help_message(message, self.db, bot)
            self.assertIn('Project &lt;A&amp;B&gt;', send.call_args.args[1]['value'])
            self.assertIn('https://t.me/test_bot?start=agreement', send.call_args.args[1]['value'])
            await show_catalog(message, self.shop, self.db)
            self.assertEqual(send.call_args.args[1]['value'], 'Catalog Project &lt;A&amp;B&gt;, &lt;Alice&gt;')

    async def test_template_values_are_not_interpreted_as_more_variables(self):
        await self.db.set_setting('project_name', 'Store')
        await self.db.set_setting('join_welcome', '{name} / {project_name} / {channel}')
        row = await rendered_section(self.db, 'join_welcome', name='{project_name}<user>', channel='A&B')
        self.assertEqual(row['value'], '{project_name}&lt;user&gt; / Store / A&amp;B')

    async def test_product_and_preview_show_discounted_prices(self):
        await self.db.register(101)
        await self.db.execute("UPDATE promos SET enabled=1 WHERE code='LUXURYNEW'")
        await self.shop.activate_promo(101, 'LUXURYNEW')
        message = self.message()
        call = SimpleNamespace(data='tariff:1', from_user=message.from_user, message=message, answer=AsyncMock())
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as send:
            for handler in (tariff_card, tariff_preview):
                await handler(call, self.shop)
                content = send.call_args.args[1]['value']
                self.assertIn('47 Stars', content)
                self.assertIn('<b>90 ₽</b>', content)
                self.assertIn('<s>240 ₽</s>', content)
                self.assertNotIn('(~240 ₽)', content)

    async def test_statistics_exclude_legacy_fake_sales_and_manual_grants(self):
        await self.db.execute("INSERT INTO simulated_sales(title,timestamp) VALUES('legacy',0)")
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id=3")
        await self.db.register(1)
        grant = await self.shop.create_order(1, 3, 'admin')
        await self.shop.settle(grant['id'], 'admin')
        data = await self.shop.get_stats_data()
        self.assertEqual(data["days_working"], 0)
        self.assertEqual(data["new_users_today"], 1)
        self.assertEqual(data["recent_purchases"], [])
        self.assertEqual(await self.shop.tariff_buyers_count(3), 0)

        for charge in ('one', 'two'):
            order = await self.shop.create_order(1, 3, 'stars')
            await self.shop.settle(order['id'], 'stars', charge_id=charge)
        self.assertEqual(await self.shop.tariff_buyers_count(3), 1)
        self.assertEqual(await self.shop.get_recent_purchases(), [(order['title'], 'только что')] * 2)

    async def test_default_sections_valid_html_and_caption_budget(self):
        for key in ('join_welcome', 'welcome', 'welcome_back', 'catalog', 'help', 'info'):
            validate_section_html(DEFAULT_SETTINGS[key])
        for key in ('join_welcome', 'welcome', 'welcome_back'):
            rendered = await rendered_section(self.db, key, name='Пользователь', channel='Секретные Фуллы')
            self.assertLessEqual(len(rendered['value'].encode('utf-16-le')) // 2, 950)
        message = self.message()
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as send:
            for tariff in await self.shop.catalogs():
                call = SimpleNamespace(
                    data=f"tariff:{tariff['id']}",
                    from_user=message.from_user,
                    message=message,
                    answer=AsyncMock(),
                )
                await tariff_card(call, self.shop)
                rendered = send.call_args.args[1]['value']
                validate_section_html(rendered)
                self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 950)

    async def test_live_recent_purchases_monotonic_and_full_across_midnight(self):
        msk = timezone(timedelta(hours=3))
        start = datetime(2026, 11, 11, 0, 1, tzinfo=msk)
        prev_sales = -1
        prev_users = -1
        for step in range(0, 24 * 60, 10):
            ts = start + timedelta(minutes=step)
            live = await get_live_recent_purchases(self.shop, self.db, current=ts)
            self.assertEqual(len(live['recent_purchases']), 5)
            self.assertGreaterEqual(live['total_sales_today'], prev_sales)
            self.assertGreaterEqual(live['users_today'], prev_users)
            prev_sales = live['total_sales_today']
            prev_users = live['users_today']
        self.assertGreater(prev_sales, 40)

    async def test_promo_finish_response_uses_valid_html(self):
        await self.db.execute("UPDATE promos SET enabled=1 WHERE code='LUXURYNEW'")
        msg = Message.model_validate({
            'message_id': 2, 'date': 1794220000,
            'from': {'id': 101, 'is_bot': False, 'first_name': 'Alice'},
            'chat': {'id': 101, 'type': 'private'}, 'text': 'LUXURYNEW',
        })
        state = SimpleNamespace(clear=AsyncMock())
        with patch.object(Message, 'answer', new_callable=AsyncMock) as answer:
            await promo_finish(msg, state, self.shop, self.db)
        validate_section_html(answer.call_args.args[0])

    async def test_live_tariff_buyers_tiered_and_custom_tariff_isolated(self):
        msk = timezone(timedelta(hours=3))
        ts = datetime(2026, 11, 11, 15, 0, tzinfo=msk)
        tariffs = await self.db.read("SELECT * FROM tariffs WHERE active=1 ORDER BY id", many=True)
        counts = {}
        for t in tariffs:
            title = t['title'].upper()
            c = await get_live_tariff_buyers(self.shop, self.db, dict(t), current=ts)
            if 'BLACK' in title:
                counts['black'] = c
                self.assertGreater(c, 200)
                continue
            self.assertGreater(c, 1000)
            if 'ULTRA' in title:
                counts['ultra'] = c
            elif 'DIAMOND' in title:
                counts['diamond'] = c
            elif 'ONLYFANS' in title:
                counts['onlyfans'] = c
            elif 'БАЗОВ' in title or '24 ЧАСА' in title or '24Ч' in title or 'ТЕСТ-ДРАЙВ' in title or 'ПРОБН' in title:
                counts['base'] = c
            elif 'АЛЬТУШК' in title:
                counts['altushki'] = c
            elif 'СКРЫТЫЙ АРХИВ' in title or 'TABOO' in title:
                counts['taboo'] = c
        self.assertGreater(counts['ultra'], counts['base'])
        self.assertGreater(counts['base'], counts['onlyfans'])
        self.assertGreater(counts['onlyfans'], counts['altushki'])
        self.assertGreater(counts['altushki'], counts['taboo'])
        self.assertGreater(counts['taboo'], counts['black'])
        self.assertGreater(counts['diamond'], 1000)

        await self.db.execute(
            "INSERT INTO tariffs(title,description,rub,stars,duration_days,active,delivery_urls) VALUES(?,?,?,?,?,1,?)",
            ('Custom Secret Pack', 'Desc', 500, 250, 0, '["https://t.me/+CustomInvite"]'),
        )
        custom = await self.db.read("SELECT * FROM tariffs WHERE title='Custom Secret Pack'")
        custom_count = await get_live_tariff_buyers(self.shop, self.db, custom, current=ts)
        self.assertGreater(custom_count, 100)
        ultra_row = next(dict(t) for t in tariffs if 'ULTRA' in t['title'].upper())
        self.assertEqual(await get_live_tariff_buyers(self.shop, self.db, ultra_row, current=ts), counts['ultra'])

    async def test_live_tariff_buyers_mixes_real_and_ignores_admin_grants(self):
        msk = timezone(timedelta(hours=3))
        ts = datetime(2026, 11, 11, 18, 30, tzinfo=msk)
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id=3")
        tariff = await self.shop.get_tariff(3)
        baseline = await get_live_tariff_buyers(self.shop, self.db, tariff, current=ts)

        await self.db.register(701)
        await self.db.register(702)
        grant = await self.shop.create_order(701, 3, 'admin')
        await self.shop.settle(grant['id'], 'admin')
        self.assertEqual(await get_live_tariff_buyers(self.shop, self.db, tariff, current=ts), baseline)

        live_before = await get_live_recent_purchases(self.shop, self.db, current=ts)
        o1 = await self.shop.create_order(701, 3, 'stars')
        await self.shop.settle(o1['id'], 'stars', charge_id='c701_1')
        paid_utc = ts.astimezone(timezone.utc).isoformat(timespec='seconds')
        await self.db.execute("UPDATE orders SET paid_at=? WHERE id=?", (paid_utc, o1['id']))
        self.assertEqual(await get_live_tariff_buyers(self.shop, self.db, tariff, current=ts), baseline + 1)

        live_after = await get_live_recent_purchases(self.shop, self.db, current=ts)
        self.assertEqual(live_after['total_sales_today'], live_before['total_sales_today'] + 1)
        self.assertEqual(live_after['recent_purchases'][0], (tariff['title'], 'только что 🔥'))

        o2 = await self.shop.create_order(702, 3, 'stars')
        await self.shop.settle(o2['id'], 'stars', charge_id='c702_1')
        self.assertEqual(await get_live_tariff_buyers(self.shop, self.db, tariff, current=ts), baseline + 2)

        o3 = await self.shop.create_order(701, 3, 'stars')
        await self.shop.settle(o3['id'], 'stars', charge_id='c701_2')
        self.assertEqual(await get_live_tariff_buyers(self.shop, self.db, tariff, current=ts), baseline + 2)

    async def test_live_tariff_buyers_monotonic_across_midnight(self):
        msk = timezone(timedelta(hours=3))
        tariff = await self.shop.get_tariff(5)
        start = datetime(2026, 11, 11, 23, 30, tzinfo=msk)
        prev = -1
        for step in range(0, 120, 5):
            ts = start + timedelta(minutes=step)
            val = await get_live_tariff_buyers(self.shop, self.db, tariff, current=ts)
            self.assertGreaterEqual(val, prev)
            prev = val
        next_day = datetime(2026, 11, 12, 23, 55, tzinfo=msk)
        self.assertGreater(await get_live_tariff_buyers(self.shop, self.db, tariff, current=next_day), prev)

    async def test_live_stats_counts_real_users_and_cumulative_cache_matches(self):
        msk = timezone(timedelta(hours=3))
        ts = datetime(2026, 11, 11, 16, 0, tzinfo=msk)
        before = await get_live_recent_purchases(self.shop, self.db, current=ts)
        await self.db.register(703)
        await self.db.execute(
            "UPDATE users SET created_at=? WHERE user_id=703",
            (ts.astimezone(timezone.utc).isoformat(timespec='seconds'),),
        )
        after = await get_live_recent_purchases(self.shop, self.db, current=ts)
        self.assertEqual(after['users_today'], before['users_today'] + 1)

        # Verify O(1) cumulative cache produces identical buyers after stepping across midnight.
        tariff = await self.shop.get_tariff(1)
        next_day = datetime(2026, 11, 12, 16, 0, tzinfo=msk)
        b1 = await get_live_tariff_buyers(self.shop, self.db, tariff, current=ts)
        b2 = await get_live_tariff_buyers(self.shop, self.db, tariff, current=next_day)
        self.assertGreater(b2, b1)
        self.assertEqual(await get_live_tariff_buyers(self.shop, self.db, tariff, current=next_day), b2)

    async def test_clean_upsell_buttons_and_formatted_card_prices(self):
        from aurora_bot.alerts import _ultra_upsell_button, _upgrade_offer_buttons
        from aurora_bot.handlers_user import _render_tariff_view

        self.assertEqual(button_tariff_name('⭐️ Ultra VIP: 4 канала [ХИТ]', 'Ultra VIP'), 'Ultra VIP: 4 канала')
        self.assertEqual(button_tariff_name('⚡ Базовый · Фуллы из TikTok', 'Базовый'), 'Базовый · Фуллы из TikTok')

        await self.db.execute("UPDATE tariffs SET title='⭐️ Ultra VIP: 4 канала [ХИТ]', channel_ids='-100999' WHERE id=5")
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111', duration_days=0 WHERE id=1")
        await self.db.execute("UPDATE tariffs SET channel_ids='-100222', duration_days=0 WHERE id=2")

        for tid in (1, 2):
            call = SimpleNamespace(
                data=f'tariff:{tid}',
                from_user=SimpleNamespace(id=101, full_name='Alice', username='alice'),
                answer=AsyncMock(),
                message=SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock(), answer_video=AsyncMock(), answer_animation=AsyncMock()),
            )
            with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
                await _render_tariff_view(call, self.shop, tid, is_preview=False)
                btn_texts = [b.text for row in sender.await_args.kwargs['markup'].inline_keyboard for b in row]
            upsell = [t for t in btn_texts if 'Ultra VIP' in t][0]
            self.assertNotIn('⭐️ ⭐️', upsell)
            self.assertNotIn('[ХИТ]', upsell)
            self.assertNotIn('(1 канал)', upsell)

        ultra = await self.shop.get_tariff(5)
        card = product_text(ultra, ultra['stars'], buyers=50, rub=ultra['rub'])
        self.assertIn('<s>1 830 ₽</s> → <b>1 190 ₽</b>', card)
        alert_btn = _ultra_upsell_button(ultra, ultra['rub'], diff=950)[0]
        self.assertNotIn('⭐️ ⭐️', alert_btn.text)
        self.assertNotIn('[ХИТ]', alert_btn.text)
        self.assertIn('1 190 ₽', alert_btn.text)

        up_btn = _upgrade_offer_buttons([{'kind': 'ULTRA', 'tariff': ultra, 'rub': 950, 'saved_rub': 240}])[0][0]
        self.assertNotIn('⚡ ⭐️', up_btn.text)
        self.assertNotIn('[ХИТ]', up_btn.text)

    def test_remaining_time_label_formats_cleanly(self):
        from datetime import datetime, timedelta, timezone
        from aurora_bot.ui import remaining_time_label

        anchor = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(remaining_time_label(None, current=anchor), '')
        self.assertEqual(remaining_time_label((anchor - timedelta(minutes=1)).isoformat(), current=anchor), '')
        self.assertEqual(remaining_time_label((anchor + timedelta(minutes=30)).isoformat(), current=anchor), '30 мин.')
        self.assertEqual(
            remaining_time_label((anchor + timedelta(hours=2, minutes=15)).isoformat(), current=anchor),
            '2 ч. 15 мин.',
        )
        self.assertEqual(remaining_time_label((anchor + timedelta(days=2, hours=1)).isoformat(), current=anchor), '~2 дн.')

    def test_seed_copy_has_no_inflated_media_counts(self):
        import re
        from aurora_bot.db import SEED_TARIFFS

        bad_re = re.compile(r'(?:\b(?:4\s*200|18\s*[04]00|20\s*[05]00|25\s*000|65\s*000)\+?|\b40\+)')
        for key in ('welcome', 'welcome_back', 'catalog', 'info'):
            self.assertIsNone(bad_re.search(DEFAULT_SETTINGS[key]), f'Unexpected media count in DEFAULT_SETTINGS[{key!r}]')
        for idx, (title, desc, _, _, _, prev) in enumerate(SEED_TARIFFS, 1):
            for text in (title, desc, prev):
                self.assertIsNone(bad_re.search(text), f'Unexpected media count in SEED_TARIFFS[{idx}]')

    async def test_paying_perk_rows_prefers_cheaper_active_promo_and_view_reminder_syncs_ultra_promo(self):
        from aurora_bot.alerts import dispatch_tariff_view_reminders, paying_perk_rows
        from aurora_bot.db import in_seconds

        await self.db.execute("UPDATE tariffs SET channel_ids='-100111', duration_days=0 WHERE id IN (1, 4, 5)")
        await self.db.register(701, 'u701', 'Buyer', via_start=True)
        o1 = await self.shop.create_order(701, 1, 'stars')
        await self.shop.settle(o1['id'], 'stars', charge_id='chg-perk-1')

        # Trade-In without promo -> 950 RUB (up_pay_options:5)
        rows_tradein = await paying_perk_rows(self.shop, self.db, 701)
        self.assertEqual(rows_tradein[0][0].callback_data, 'up_pay_options:5')
        self.assertIn('950 ₽', rows_tradein[0][0].text)

        # Activate promo making Ultra 690 RUB (< 950 RUB) -> switches CTA to pay_options:5
        await self.db.execute("INSERT INTO promos(code, discount, max_uses, enabled) VALUES('BIG500', 500, 5, 1)")
        await self.shop.activate_promo(701, 'BIG500')
        rows_promo = await paying_perk_rows(self.shop, self.db, 701)
        self.assertEqual(rows_promo[0][0].callback_data, 'pay_options:5')
        self.assertIn('690 ₽', rows_promo[0][0].text)

        # View reminder with unapplied personal 20% promo adjusts Ultra diff (952 - 712 = +240) and jumps via claim_tariff:5
        await self.db.register(702, 'u702', 'Viewer', via_start=True)
        await self.db.execute(
            "INSERT INTO promos(code, discount, discount_percent, max_uses, enabled, owner_user_id, is_personal) "
            "VALUES('VIEW20', 0, 20, 1, 1, 702, 1)"
        )
        await self.db.execute(
            "UPDATE users SET promo_push_code='VIEW20', last_viewed_tariff_id=4, last_viewed_at=? WHERE user_id=702",
            (in_seconds(-1800),),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        self.assertEqual(await dispatch_tariff_view_reminders(bot, self.db, self.shop), 1)
        btns = [b for row in bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        ultra_btn = next(b for b in btns if 'Ultra VIP' in b.text)
        self.assertIn('claim_tariff:5:VIEW20', ultra_btn.callback_data)
        self.assertIn('+240 ₽', ultra_btn.text)
