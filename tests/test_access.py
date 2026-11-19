from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import RefundStarPayment

from aurora_bot.access import AccessManager
from aurora_bot.alerts import dispatch_abandoned_cart_reminders, dispatch_whale_alerts
from aurora_bot.config import Config
from aurora_bot.db import Database, in_seconds
from aurora_bot.service import Shop
from aurora_bot.workers import maintain_access


class FakeBot:
    def __init__(self):
        self.created=[]
        self.revoke=[]
        self.messages=[]
        self.banned=[]
        self.unbanned=[]
        self.member_status = 'member'
        self.fail_invites=False

    async def create_chat_invite_link(self, *, chat_id, name, expire_date, creates_join_request):
        if self.fail_invites:
            raise RuntimeError('not admin')
        self.created.append((chat_id,name,expire_date,creates_join_request))
        return NS(invite_link=f'https://t.me/+test-{chat_id}-{len(self.created)}')

    async def send_message(self,uid,text):
        self.messages.append((uid,text))

    async def revoke_chat_invite_link(self,chat_id,url):
        self.revoke.append((chat_id,url))

    async def get_chat_member(self, chat_id, uid):
        return NS(status=self.member_status)

    async def ban_chat_member(self,chat_id,uid, *, until_date):
        self.banned.append((chat_id,uid))

    async def unban_chat_member(self,chat_id,uid,only_if_banned):
        self.unbanned.append((chat_id,uid))


class FakeRequest:
    def __init__(self, uid, chat, link):
        self.from_user=NS(id=uid)
        self.chat=NS(id=chat)
        self.invite_link=NS(invite_link=link)
        self.approved=self.declined=False
    async def approve(self): self.approved=True
    async def decline(self): self.declined=True


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database(str(Path(self.temp.name)/'db.sqlite3'))
        await self.db.init()
        await self.db.register(101,'user','Test')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111',duration_days=1 WHERE id=1")
        self.shop=Shop(self.db)
        self.bot=FakeBot()
        self.access=AccessManager(self.db,self.shop)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def paid(self):
        order=await self.shop.create_order(101,1,'stars')
        await self.shop.settle(order['id'],'stars',charge_id=order['id'])
        return order

    async def test_provision_and_secure_join(self):
        order=await self.paid()
        await self.access.provision(self.bot)
        self.assertEqual(len(self.bot.created),1)
        self.assertTrue(self.bot.created[0][3])
        self.assertEqual(len(self.bot.messages),1)
        await self.access.provision(self.bot)
        self.assertEqual(len(self.bot.messages),1)
        purchase=(await self.shop.purchases(101))[0]
        grant=(await self.shop.purchase_grants(purchase['id']))[0]
        wrong=FakeRequest(202,grant['chat_id'],grant['invite_url'])
        self.assertEqual(await self.access.on_join_request(wrong),'denied')
        self.assertTrue(wrong.declined)
        correct=FakeRequest(101,grant['chat_id'],grant['invite_url'])
        self.assertEqual(await self.access.on_join_request(correct),'approved')
        self.assertTrue(correct.approved)
        joined=(await self.shop.purchase_grants(purchase['id']))[0]
        self.assertEqual(joined['state'],'joined')

    async def test_failed_invite_retry(self):
        await self.paid()
        self.bot.fail_invites=True
        await self.access.provision(self.bot)
        self.assertEqual(len(self.bot.messages),0)
        state=(await self.db.read('SELECT state FROM grants'))['state']
        self.assertEqual(state,'error')
        # Simulate the two-minute cooldown having passed.
        await self.db.execute('UPDATE grants SET processing_at=?',((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(),))
        self.bot.fail_invites=False
        await self.access.provision(self.bot)
        self.assertEqual(len(self.bot.messages),1)

    async def test_expiry_removes_membership(self):
        await self.paid()
        await self.access.provision(self.bot)
        purchase=(await self.shop.purchases(101))[0]
        grant=(await self.shop.purchase_grants(purchase['id']))[0]
        await self.access.on_join_request(FakeRequest(101,grant['chat_id'],grant['invite_url']))
        await self.db.execute('UPDATE purchases SET expires_at=? WHERE id=?',
           ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),purchase['id']))
        await self.access.revoke_expired(self.bot)
        self.assertEqual(self.bot.banned,[(-100111,101)])
        self.assertEqual(self.bot.unbanned,[])
        self.assertEqual((await self.shop.purchase_grants(purchase['id']))[0]['state'],'expired')

    async def test_overlapping_active_grants_prevent_kick(self):
        await self.paid()
        await self.paid()
        await self.access.provision(self.bot)
        purchases=await self.shop.purchases(101)
        earlier=purchases[-1]
        await self.db.execute('UPDATE purchases SET expires_at=? WHERE id=?',
           ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),earlier['id']))
        await self.access.revoke_expired(self.bot)
        self.assertEqual(self.bot.banned,[])
        self.assertEqual((await self.shop.purchase_grants(earlier['id']))[0]['state'],'expired')

    async def test_refresh_joined_access(self):
        await self.paid()
        await self.access.provision(self.bot)
        purchase=(await self.shop.purchases(101))[0]
        grant=(await self.shop.purchase_grants(purchase['id']))[0]
        await self.access.on_join_request(FakeRequest(101,grant['chat_id'],grant['invite_url']))
        self.assertTrue(await self.access.refresh_user(self.bot,101,grant['id']))
        self.assertEqual(len(self.bot.created),2)
        new=(await self.shop.purchase_grants(purchase['id']))[0]
        self.assertNotEqual(new['invite_url'],grant['invite_url'])

    async def test_refresh_near_expiry_preserves_removal_job(self):
        await self.paid()
        await self.access.provision(self.bot)
        grant = await self.db.read('SELECT * FROM grants')
        await self.access.on_join_request(FakeRequest(101, grant['chat_id'], grant['invite_url']))
        await self.db.execute('UPDATE purchases SET expires_at=?', (in_seconds(10),))
        self.assertFalse(await self.access.refresh_user(self.bot, 101, grant['id']))
        await self.db.execute('UPDATE purchases SET expires_at=?', (in_seconds(-10),))
        await self.access.revoke_expired(self.bot)
        self.assertEqual(self.bot.banned, [(-100111, 101)])

    async def test_failed_notification_does_not_block_next_order(self):
        first = await self.paid()
        await self.db.register(202)
        second = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(second['id'], 'stars', charge_id=second['id'])
        await self.db.execute('UPDATE orders SET paid_at=? WHERE id=?', (in_seconds(-60), first['id']))
        send = self.bot.send_message

        async def fail_first(uid, text):
            if uid == 101:
                raise RuntimeError('buyer blocked bot')
            await send(uid, text)

        self.bot.send_message = fail_first
        await self.access.provision(self.bot, limit=1)
        await self.access.provision(self.bot, limit=1)
        self.assertEqual([uid for uid, _ in self.bot.messages], [202])

    async def test_expiry_does_not_lift_a_moderator_ban(self):
        await self.paid()
        self.bot.member_status = 'kicked'
        await self.db.execute('UPDATE purchases SET expires_at=?', (in_seconds(-1),))
        await self.access.revoke_expired(self.bot)
        self.assertEqual(self.bot.banned, [])
        self.assertEqual(self.bot.unbanned, [])
        self.assertEqual((await self.db.read('SELECT state FROM grants'))['state'], 'expired')

    async def test_delivery_rechecks_refund_after_batch_selection(self):
        await self.db.execute("""UPDATE tariffs SET channel_ids='',duration_days=0,
            delivery_urls='["https://t.me/+TestInvite"]' WHERE id=1""")
        first = await self.paid()
        await self.db.register(202)
        second = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(second['id'], 'stars', charge_id=second['id'])
        await self.db.execute('UPDATE orders SET paid_at=? WHERE id=?', (in_seconds(-60), first['id']))
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def send(uid, text, **kwargs):
            sent.append(uid)
            if uid == 101:
                entered.set()
                await release.wait()

        delivery = asyncio.create_task(self.access.provision(NS(send_message=send)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        refund = asyncio.create_task(self.shop.refund_stars(second['id'], second['amount_stars'], order_id=second['id']))
        await asyncio.sleep(0)
        release.set()
        _, changed = await asyncio.gather(delivery, refund)
        self.assertTrue(changed)
        self.assertEqual(sent, [101])
        self.assertEqual((await self.shop.order(second['id']))['notice_sent'], 0)

    async def test_ambiguous_refund_pauses_delivery_until_reconciled(self):
        await self.db.execute("""UPDATE tariffs SET channel_ids='',duration_days=0,
            delivery_urls='["https://t.me/+TestInvite"]' WHERE id=1""")
        order = await self.paid()
        with self.assertRaises(TimeoutError):
            await self.shop.refund_stars(order['id'], order['amount_stars'], user_id=101, order_id=order['id'],
                                         request_refund=AsyncMock(side_effect=TimeoutError))
        sender = NS(send_message=AsyncMock())
        await self.access.provision(sender)
        sender.send_message.assert_not_awaited()
        self.assertTrue((await self.shop.order(order['id']))['refund_requested_at'])
        self.assertTrue(await self.shop.refund_stars(order['id'], order['amount_stars'], order_id=order['id']))
        self.assertIsNone((await self.shop.order(order['id']))['refund_requested_at'])

    async def test_rejected_refund_preserves_delivery(self):
        order = await self.paid()
        method = RefundStarPayment(user_id=101, telegram_payment_charge_id=order['id'])
        for error in (TelegramBadRequest(method=method, message='BALANCE_TOO_LOW'),
                      TelegramForbiddenError(method=method, message='Forbidden'),
                      TelegramRetryAfter(method=method, message='Retry later', retry_after=10)):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    await self.shop.refund_stars(order['id'], order['amount_stars'], user_id=101,
                        order_id=order['id'], request_refund=AsyncMock(side_effect=error))
                self.assertIsNone((await self.shop.order(order['id']))['refund_requested_at'])
        self.assertFalse(await self.shop.refund_stars(order['id'], order['amount_stars'], user_id=101,
            order_id=order['id'], request_refund=AsyncMock(return_value=False)))
        await self.access.provision(self.bot)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_rejected_retry_does_not_clear_ambiguous_refund(self):
        order = await self.paid()
        with self.assertRaises(TimeoutError):
            await self.shop.refund_stars(order['id'], order['amount_stars'], user_id=101,
                order_id=order['id'], request_refund=AsyncMock(side_effect=TimeoutError))
        previous = (await self.shop.order(order['id']))['refund_requested_at']
        self.assertFalse(await self.shop.refund_stars(order['id'], order['amount_stars'], user_id=101,
            order_id=order['id'], request_refund=AsyncMock(return_value=False)))
        self.assertEqual((await self.shop.order(order['id']))['refund_requested_at'], previous)
        await self.access.provision(self.bot)
        self.assertEqual(self.bot.messages, [])

    async def test_already_refunded_response_closes_access(self):
        order = await self.paid()
        method = RefundStarPayment(user_id=101, telegram_payment_charge_id=order['id'])
        self.assertTrue(await self.shop.refund_stars(order['id'], order['amount_stars'], user_id=101,
            order_id=order['id'], request_refund=AsyncMock(side_effect=TelegramBadRequest(
                method=method, message='Bad Request: CHARGE_ALREADY_REFUNDED'))))
        self.assertEqual((await self.shop.order(order['id']))['status'], 'refunded')
        await self.access.provision(self.bot)
        self.assertEqual(self.bot.messages, [])

    async def test_expiry_upsell_push_sent_once_and_skipped_when_ultra_owned(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111,-100222' WHERE id=3")
        order = await self.paid()
        await self.access.provision(self.bot)
        self.bot.messages.clear()
        await self.db.execute('UPDATE purchases SET expires_at=?', (in_seconds(-1),))
        await self.access.revoke_expired(self.bot)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn('190 ₽', self.bot.messages[0][1])
        self.assertIn('800 ₽', self.bot.messages[0][1])

        await self.access.revoke_expired(self.bot)
        self.assertEqual(len(self.bot.messages), 1)

        # User who owns ULTRA VIP forever does NOT receive expired notice for old 24h access
        await self.db.register(202, 'vip', 'VIP')
        o_trial = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle(o_trial['id'], 'stars', charge_id='vip-1')
        o_ultra = await self.shop.create_order(202, 3, 'stars')
        await self.shop.settle(o_ultra['id'], 'stars', charge_id='vip-2')
        self.bot.messages.clear()
        await self.db.execute("UPDATE purchases SET expires_at=? WHERE order_id=?", (in_seconds(-1), o_trial['id']))
        await self.access.revoke_expired(self.bot)
        self.assertEqual(self.bot.messages, [])

        # 30-day buyer receives "Срок твоего доступа завершён:" rather than misleading "24ч" headline
        await self.db.execute("UPDATE tariffs SET channel_ids='-100333' WHERE id=2")
        await self.db.register(303, 'monthly', 'Monthly')
        o_m = await self.shop.create_order(303, 2, 'stars')
        await self.shop.settle(o_m['id'], 'stars', charge_id='m-30d')
        await self.access.provision(self.bot)
        self.bot.messages.clear()
        await self.db.execute("UPDATE purchases SET expires_at=? WHERE order_id=?", (in_seconds(-1), o_m['id']))
        await self.access.revoke_expired(self.bot)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn('Срок твоего доступа завершён:', self.bot.messages[0][1])
        self.assertNotIn('24ч доступ', self.bot.messages[0][1])

    async def test_whale_radar_alerts_vip_high_ticket_custom_and_ltv_once(self):
        cfg = Config('123:mock', frozenset({901, 902}))
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id IN (2, 3)")

        # Low-ticket 190 RUB order: no alert
        small = await self.paid()
        await maintain_access(self.bot, self.db, self.access, cfg)
        self.assertEqual([uid for uid, _ in self.bot.messages if uid in cfg.admin_ids], [])
        self.assertEqual((await self.db.read('SELECT whale_alert_sent FROM orders WHERE id=?', (small['id'],)))['whale_alert_sent'], 1)

        # Cumulative LTV crosses 1500 RUB (190 + 590 + 590 + 590 = 1960) -> alerts both admins once
        o2 = await self.shop.create_order(101, 2, 'stars')
        await self.shop.settle(o2['id'], 'stars', charge_id='w-ltv-2')
        o3 = await self.shop.create_order(101, 2, 'stars')
        await self.shop.settle(o3['id'], 'stars', charge_id='w-ltv-3')
        o4 = await self.shop.create_order(101, 2, 'stars')
        await self.shop.settle(o4['id'], 'stars', charge_id='w-ltv-4')
        self.bot.messages.clear()
        sent = await dispatch_whale_alerts(self.bot, self.db, cfg)
        self.assertEqual(sent, 1)
        admin_recipients = sorted(uid for uid, _ in self.bot.messages if uid in cfg.admin_ids)
        self.assertEqual(admin_recipients, [901, 902])
        self.assertIn('РАДАР КИТОВ', self.bot.messages[0][1])
        self.assertEqual(await dispatch_whale_alerts(self.bot, self.db, cfg), 0)

        # High-ticket Diamond tariff purchase triggers admin whale alert too
        await self.db.register(303, 'rich', 'Rich')
        await self.db.execute("UPDATE tariffs SET channel_ids='-1003' WHERE id=4")
        hi_ord = await self.shop.create_order(303, 4, 'cryptopay')
        await self.shop.set_external(hi_ord['id'], 'cryptopay', 'cp-rich', 'https://t.me/CryptoBot?start=Rich')
        await self.shop.settle(hi_ord['id'], 'cryptopay', external_id='cp-rich')
        self.bot.messages.clear()
        sent2 = await dispatch_whale_alerts(self.bot, self.db, cfg)
        self.assertEqual(sent2, 1)
        self.assertEqual(sorted(uid for uid, _ in self.bot.messages), [901, 902])
        self.assertEqual(await dispatch_whale_alerts(self.bot, self.db, cfg), 0)

    async def test_send_notice_includes_channel_invite_buttons_for_channel_ids_tariff(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='-1001,-1002' WHERE id=1")
        await self.paid()
        sent_calls = []

        class MarkupBot(FakeBot):
            async def send_message(self, uid, text, reply_markup=None):
                sent_calls.append((uid, text, reply_markup))

        markup_bot = MarkupBot()
        await self.access.provision(markup_bot)
        self.assertEqual(len(sent_calls), 1)
        uid, text, markup = sent_calls[0]
        self.assertEqual(uid, 101)
        urls = [b.url for row in markup.inline_keyboard for b in row if getattr(b, 'url', None)]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(u.startswith('https://t.me/+test-') for u in urls))

    async def test_abandoned_cart_reminder_fires_once_between_20m_and_4h_only_for_non_buyers(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id IN (1, 2)")
        await self.db.register(202, 'abandoned', 'Abandoned')
        await self.db.register(303, 'fresh', 'Fresh')
        await self.db.register(404, 'ancient', 'Ancient')

        # User 202: two unpaid checkouts for #2 (690 RUB) aged 35 min -> 1 reminder with #2 + trial fallback
        o_old = await self.shop.create_order(202, 2, '2328')
        o_recent = await self.shop.create_order(202, 2, 'cryptopay')
        await self.db.execute(
            'UPDATE orders SET created_at=?, expires_at=? WHERE id IN (?, ?)',
            (in_seconds(-35 * 60), in_seconds(-5 * 60), o_old['id'], o_recent['id']),
        )
        # User 101: has an aged unpaid cart, BUT already has a paid order -> skipped
        await self.paid()
        o_buyer = await self.shop.create_order(101, 2, 'stars')
        await self.db.execute('UPDATE orders SET created_at=? WHERE id=?', (in_seconds(-30 * 60), o_buyer['id']))
        # User 303: pending only 5 min -> too early
        o_fresh = await self.shop.create_order(303, 2, 'stars')
        await self.db.execute('UPDATE orders SET created_at=? WHERE id=?', (in_seconds(-2 * 60), o_fresh['id']))
        # User 404: pending 6 hours ago -> stale, outside 4h window
        o_ancient = await self.shop.create_order(404, 2, 'stars')
        await self.db.execute('UPDATE orders SET created_at=? WHERE id=?', (in_seconds(-6 * 3600), o_ancient['id']))

        sent_calls = []

        class MarkupBot(FakeBot):
            async def send_message(self, uid, text, reply_markup=None):
                sent_calls.append((uid, text, reply_markup))

        markup_bot = MarkupBot()
        count = await dispatch_abandoned_cart_reminders(markup_bot, self.db, self.shop)
        self.assertEqual(count, 1)
        self.assertEqual([uid for uid, _, _ in sent_calls], [202])
        cbs = [b.callback_data.split(':', 2)[2] for row in sent_calls[0][2].inline_keyboard for b in row]
        self.assertIn('pay_options:2', cbs)
        self.assertIn('pay_options:1', cbs)
        self.assertIn('catalog', cbs)
        # Re-running does not send duplicate reminder
        self.assertEqual(await dispatch_abandoned_cart_reminders(markup_bot, self.db, self.shop), 0)


if __name__ == '__main__': unittest.main()
