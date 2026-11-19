from __future__ import annotations

import asyncio
import unittest

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import User

from main import poll_until_stopped


class OfflineSession(AiohttpSession):
    def __init__(self):
        super().__init__()
        self.fetching = asyncio.Event()
        self.fetch_stopped = asyncio.Event()

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == 'getMe':
            return User(id=123, is_bot=True, first_name='Test', username='test_bot')
        if method.__api_method__ == 'getUpdates':
            self.fetching.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.fetch_stopped.set()
        raise AssertionError(method.__api_method__)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_stops_getupdates_before_session_cleanup(self):
        session = OfflineSession()
        bot = Bot('123:mock', session=session)
        dp = Dispatcher()
        try:
            running = asyncio.create_task(poll_until_stopped(dp, bot))
            await asyncio.wait_for(session.fetching.wait(), timeout=3)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(running, timeout=3)
            self.assertTrue(session.fetch_stopped.is_set())
            self.assertFalse(dp._running_lock.locked())
        finally:
            await dp.fsm.close()
            await session.close()

    async def test_real_startup_error_is_not_hidden(self):
        class FailingDispatcher:
            def resolve_used_update_types(self):
                return []

            async def start_polling(self, *args, **kwargs):
                raise RuntimeError('Startup failed')

        with self.assertRaisesRegex(RuntimeError, 'Startup failed'):
            await poll_until_stopped(FailingDispatcher(), None)


if __name__ == '__main__':
    unittest.main()
