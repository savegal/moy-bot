from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from aurora_bot import handlers_admin as admin
from aurora_bot.access import AccessManager
from aurora_bot.config import Config
from aurora_bot.db import Database, in_seconds
from aurora_bot.handlers_user import (
    external_pay_invoice,
    my_orders,
    upgrade_external_pay_invoice,
)
from aurora_bot.payments import GatewayError, Payments
from aurora_bot.service import Shop, ShopError


class AuditFixRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / 'audit_fixes.sqlite3'))
        await self.db.init()
        await self.db.set_setting('support_url', 'https://t.me/support_bot')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id=1")
        await self.db.register(101, 'alice', 'Alice', via_start=True)
        await self.db.register(202, 'bob', 'Bob', via_start=True)
        self.shop = Shop(self.db)
        self.access = AccessManager(self.db, self.shop)
        self.storage = MemoryStorage()
        self.addAsyncCleanup(self.storage.close)
        self.state = FSMContext(self.storage, StorageKey(bot_id=123, chat_id=99, user_id=99))
        self.cfg = Config(
            '123:mock',
            frozenset({99}),
            allow_external_methods=True,
            project_2328='proj',
            key_2328='sec',
            public_base_url='https://pay.example.com',
            cryptopay_token='123:crypto',
        )
        self.bot = NS(
            id=123,
            get_chat=AsyncMock(return_value=NS(type='channel', username=None, active_usernames=None)),
            get_chat_member=AsyncMock(
                return_value=NS(status='administrator', can_invite_users=True, can_restrict_members=True)
            ),
            create_chat_invite_link=AsyncMock(
                side_effect=lambda **kw: NS(invite_link=f"https://t.me/+inv-{kw['chat_id']}-1")
            ),
            create_invoice_link=AsyncMock(return_value='https://t.me/$stars_inv'),
            send_message=AsyncMock(),
        )

    def _user_call(self, data: str, user_id: int = 101, username: str = 'alice', name: str = 'Alice'):
        return NS(
            data=data,
            from_user=NS(id=user_id, username=username, full_name=name),
            answer=AsyncMock(),
            message=NS(answer=AsyncMock()),
        )

    async def test_failed_external_creation_does_not_lock_promos_or_clutter_my_orders(self):
        # Personal promo must remain usable after a failed external gateway call.
        await self.db.execute(
            """INSERT INTO promos(code, discount, max_uses, enabled, owner_user_id, is_personal)
               VALUES('PERS100', 100, 1, 1, 101, 1)"""
        )
        await self.shop.activate_promo(101, 'PERS100')
        payments = Payments(self.cfg, self.shop)

        with patch.object(payments.crypto, 'create', new=AsyncMock(side_effect=GatewayError('down'))):
            c_fail = self._user_call('ext_pay:cryptopay:1', 101)
            await external_pay_invoice(c_fail, self.bot, self.shop, self.db, payments, self.cfg)
            self.assertTrue(c_fail.answer.await_args.kwargs['show_alert'])

        # Failed unissued order must not appear in my_orders or render broken Check buttons.
        orders_call = self._user_call('my_orders', 101)
        await my_orders(orders_call, self.db)
        orders_text = orders_call.message.answer.await_args.args[0]
        orders_cbs = [
            b.callback_data
            for row in orders_call.message.answer.await_args.kwargs['reply_markup'].inline_keyboard
            for b in row
            if b.callback_data
        ]
        self.assertIn('Покупок пока нет', orders_text)
        self.assertFalse(any(cb_data.startswith('check:') for cb_data in orders_cbs))

        # Switching to CryptoPay succeeds immediately; Stars checkout also works.
        with patch.object(
            payments.crypto, 'create', new=AsyncMock(return_value=('cp-pers-1', 'https://t.me/CryptoBot?start=cp1'))
        ):
            c_ok = self._user_call('ext_pay:cryptopay:1', 101)
            await external_pay_invoice(c_ok, self.bot, self.shop, self.db, payments, self.cfg)
            self.assertIn('Перейти к оплате', c_ok.message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text)

        # Even if a leftover unissued pending external order exists in DB, my_orders hides its Check button
        # and authorize_stars ignores it for shared promo capacity.
        await self.db.execute(
            """INSERT INTO promos(code, discount, max_uses, enabled, is_personal)
               VALUES('SHARED1', 100, 1, 1, 0)"""
        )
        await self.shop.activate_promo(202, 'SHARED1')
        unissued = await self.shop.create_order(202, 1, '2328')
        await self.db.register(303, 'carol', 'Carol', via_start=True)
        await self.shop.activate_promo(303, 'SHARED1')
        stars_303 = await self.shop.create_order(303, 1, 'stars')
        self.assertTrue(await self.shop.authorize_stars(stars_303['id'], 303, stars_303['amount_stars'], 'ck-303'))

        call_202_orders = self._user_call('my_orders', 202)
        await my_orders(call_202_orders, self.db)
        cbs_202 = [
            b.callback_data
            for row in call_202_orders.message.answer.await_args.kwargs['reply_markup'].inline_keyboard
            for b in row
            if b.callback_data
        ]
        self.assertNotIn(f"check:{unissued['id']}", cbs_202)

    async def test_reusable_external_invoice_last_10_minutes_for_personal_promo(self):
        payments = Payments(self.cfg, self.shop)

        # Regular tariff with a personal promo inside last 10 minutes must reuse instead of failing.
        await self.db.execute(
            """INSERT INTO promos(code, discount, max_uses, enabled, owner_user_id, is_personal)
               VALUES('PERS10M', 100, 1, 1, 202, 1)"""
        )
        await self.shop.activate_promo(202, 'PERS10M')
        with patch.object(
            payments.crypto, 'create', new=AsyncMock(return_value=('cp-pers-10m', 'https://t.me/CryptoBot?start=p10m'))
        ) as cp_create:
            await external_pay_invoice(
                self._user_call('ext_pay:cryptopay:1', 202, 'bob', 'Bob'),
                self.bot, self.shop, self.db, payments, self.cfg,
            )
            cp_create.assert_awaited_once()
            await self.db.execute("UPDATE orders SET expires_at=? WHERE user_id=202", (in_seconds(5 * 60),))

            reuse_tariff = self._user_call('ext_pay:cryptopay:1', 202, 'bob', 'Bob')
            await external_pay_invoice(reuse_tariff, self.bot, self.shop, self.db, payments, self.cfg)
            cp_create.assert_awaited_once()
            self.assertEqual(
                reuse_tariff.message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url,
                'https://t.me/CryptoBot?start=p10m',
            )

    async def test_extend_reopened_purchase_notifies_user_and_clears_revoke_errors(self):
        # 1. Expired purchase with expired grants -> extend resets notice_sent & grants, provision delivers new link.
        order = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(order['id'], 101, order['amount_stars'], 'chg-ext-1')
        purchase = (await self.shop.purchases(101))[0]
        await self.db.execute(
            """UPDATE purchases
                  SET expires_at=?, expiry_notice_sent=1, pre_expiry_upsell_sent=1
                WHERE id=?""",
            (in_seconds(-3600), purchase['id']),
        )
        await self.db.execute(
            """UPDATE grants
                  SET state='expired', invite_url='https://t.me/+oldlink',
                      revoke_retry_at=?, last_error='Old removal error'
                WHERE purchase_id=?""",
            (in_seconds(120), purchase['id']),
        )
        await self.db.execute("UPDATE orders SET notice_sent=1 WHERE id=?", (order['id'],))

        await self.state.set_state(admin.AdminState.extension)
        await self.state.update_data(pid=purchase['id'])
        ext_msg = NS(text='15', from_user=NS(id=99), chat=NS(id=99), answer=AsyncMock())
        await admin.extend(ext_msg, self.state, self.db, self.access)
        self.assertIn('перевыпустит ссылки и уведомит покупателя', ext_msg.answer.await_args.args[0])

        updated_purchase = (await self.shop.purchases(101))[0]
        self.assertEqual(updated_purchase['expiry_notice_sent'], 0)
        self.assertEqual(updated_purchase['revoked_by_admin'], 0)
        updated_grant = await self.db.read("SELECT * FROM grants WHERE purchase_id=?", (purchase['id'],))
        self.assertEqual(updated_grant['state'], 'need_link')
        self.assertIsNone(updated_grant['revoke_retry_at'])
        self.assertEqual(updated_grant['last_error'], '')
        self.assertEqual((await self.shop.order(order['id']))['notice_sent'], 0)

        self.bot.send_message.reset_mock()
        await self.access.provision(self.bot)
        self.bot.create_chat_invite_link.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.args[0], 101)
        self.assertEqual((await self.shop.order(order['id']))['notice_sent'], 1)

        # 2. Extending an already-active purchase must NOT resend the initial purchase confirmation.
        self.bot.send_message.reset_mock()
        await self.state.set_state(admin.AdminState.extension)
        await self.state.update_data(pid=purchase['id'])
        ext_active = NS(text='5', from_user=NS(id=99), chat=NS(id=99), answer=AsyncMock())
        await admin.extend(ext_active, self.state, self.db, self.access)
        self.assertEqual((await self.shop.order(order['id']))['notice_sent'], 1)
        await self.access.provision(self.bot)
        self.bot.send_message.assert_not_awaited()

        # 3. Extending a closed purchase whose grant is in 'error' (not 'expired') clears processing_at & notifies user.
        await self.db.execute("UPDATE tariffs SET channel_ids='-100222' WHERE id=2")
        err_ord = await self.shop.create_order(202, 2, 'admin', duration_days=30)
        await self.shop.settle(err_ord['id'], 'admin')
        err_purchase = (await self.shop.purchases(202))[0]
        await self.db.execute("UPDATE orders SET notice_sent=1 WHERE id=?", (err_ord['id'],))
        await self.db.execute(
            "UPDATE purchases SET expires_at=?, revoked_by_admin=1 WHERE id=?",
            (in_seconds(-10), err_purchase['id']),
        )
        await self.db.execute(
            """UPDATE grants
                  SET state='error',
                      revoke_retry_at=?,
                      processing_at=?,
                      last_error='transient ban error'
                WHERE purchase_id=?""",
            (in_seconds(120), in_seconds(0), err_purchase['id']),
        )
        await self.state.set_state(admin.AdminState.extension)
        await self.state.update_data(pid=err_purchase['id'])
        await admin.extend(NS(text='10', from_user=NS(id=99), chat=NS(id=99), answer=AsyncMock()), self.state, self.db, self.access)
        reopened_grant = await self.db.read("SELECT * FROM grants WHERE purchase_id=?", (err_purchase['id'],))
        self.assertEqual(reopened_grant['state'], 'need_link')
        self.assertIsNone(reopened_grant['revoke_retry_at'])
        self.assertIsNone(reopened_grant['processing_at'])
        self.assertEqual(reopened_grant['last_error'], '')
        self.assertEqual((await self.shop.order(err_ord['id']))['notice_sent'], 0)
        self.bot.create_chat_invite_link.reset_mock()
        self.bot.send_message.reset_mock()
        await self.access.provision(self.bot)
        self.bot.create_chat_invite_link.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.args[0], 202)

        # Even without revoke_retry_at, extend resets an 'error' grant to 'need_link' for immediate retry.
        await self.db.execute(
            "UPDATE grants SET state='error', processing_at=?, last_error='invite failed' WHERE purchase_id=?",
            (in_seconds(0), err_purchase['id']),
        )
        await self.state.set_state(admin.AdminState.extension)
        await self.state.update_data(pid=err_purchase['id'])
        await admin.extend(NS(text='5', from_user=NS(id=99), chat=NS(id=99), answer=AsyncMock()), self.state, self.db, self.access)
        retried_grant = await self.db.read("SELECT * FROM grants WHERE purchase_id=?", (err_purchase['id'],))
        self.assertEqual(retried_grant['state'], 'need_link')
        self.assertIsNone(retried_grant['processing_at'])
        self.assertEqual(retried_grant['last_error'], '')

    async def test_crypto_upgrade_works_with_hidden_contacts_and_cards_are_disabled(self):
        paid = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(paid['id'], 101, paid['amount_stars'], 'chg-up-sup')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100555', active=1 WHERE id=5")
        await self.db.set_setting('support_url', '')
        await self.db.set_setting('contacts_visible', '0')

        payments = Payments(self.cfg, self.shop)
        payments.gateway.create = AsyncMock()
        payments.crypto.create = AsyncMock(return_value=('crypto-upgrade', 'https://t.me/CryptoBot?start=upgrade'))

        old_card = self._user_call('ext_pay:2328:1', 101)
        await external_pay_invoice(old_card, self.bot, self.shop, self.db, payments, self.cfg)
        self.assertTrue(old_card.answer.await_args.kwargs['show_alert'])
        payments.gateway.create.assert_not_awaited()

        call = self._user_call('up_ext_pay:cryptopay:5', 101)
        await upgrade_external_pay_invoice(call, self.bot, self.shop, self.db, payments, self.cfg)
        payments.crypto.create.assert_awaited_once()
        call.message.answer.assert_awaited_once()

    async def test_reject_manual_marks_order_failed_and_notifies_user(self):
        manual_order = await self.shop.create_order(101, 1, 'manual')
        self.assertTrue(await self.shop.manual_review(manual_order['id'], 101))

        with patch.object(admin, 'order_details', new_callable=AsyncMock):
            await admin.reject_manual(
                NS(data=f"adm:reject:{manual_order['id']}", from_user=NS(id=99), answer=AsyncMock(), message=NS()),
                self.db,
                self.bot,
            )

        rejected = await self.shop.order(manual_order['id'])
        self.assertEqual(rejected['status'], 'failed')
        self.bot.send_message.assert_awaited()


if __name__ == '__main__':
    unittest.main()
