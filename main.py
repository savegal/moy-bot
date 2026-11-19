"""Run with `python main.py` using Python 3.11+ and aiogram 3."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TEST
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import Update

from aurora_bot.config import Config
from aurora_bot.db import Database
from aurora_bot.service import Shop
from aurora_bot.payments import Payments
from aurora_bot.access import AccessManager
from aurora_bot.workers import dispatch_marketing, maintain_access, reconcile_external, reconcile_stars, run_periodically
from aurora_bot.server import start_server
from aurora_bot.greetings import retry_welcome_approvals
from aurora_bot.handlers_admin import router as admin_router
from aurora_bot.handlers_user import router as user_router


def build_dispatcher(**workflow_data) -> Dispatcher:
    dp = Dispatcher(events_isolation=SimpleEventIsolation(), **workflow_data)
    dp.update.outer_middleware.unregister(dp.fsm)

    @dp.update.outer_middleware()
    async def isolate_stateful_updates(handler, update: Update, data):
        # Pre-checkout has a Telegram deadline and does not read or write FSM state.
        if update.pre_checkout_query is not None:
            return await handler(update, data)
        return await dp.fsm(handler, update, data)

    dp.include_router(admin_router)
    dp.include_router(user_router)
    return dp


async def poll_until_stopped(dp: Dispatcher, bot: Bot):
    # On Windows asyncio.run handles Ctrl+C by cancelling the main task.
    # Keep aiogram alive long enough to stop its getUpdates task before the
    # owning finally block closes the HTTP session.
    polling = asyncio.create_task(dp.start_polling(
        bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True,
        close_bot_session=False, tasks_concurrency_limit=50))
    try:
        await asyncio.shield(polling)
    except asyncio.CancelledError:
        stopping = asyncio.create_task(dp.stop_polling())
        try:
            await asyncio.wait((polling, stopping), return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Also handle cancellation during startup, or polling that failed
            # before stop_polling could observe a running dispatcher.
            for task in (stopping, polling):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stopping, polling, return_exceptions=True)
        raise


async def main():
    logging.basicConfig(format='%(asctime)s %(levelname)s %(name)s %(message)s',level=logging.INFO)
    config=Config.from_env()
    db=Database(config.db_path)
    await db.init()
    shop=Shop(db)
    payments=Payments(config,shop)
    access=AccessManager(db,shop)
    session = AiohttpSession(api=TEST) if config.telegram_test_mode else None
    bot = Bot(config.token, session=session, default=DefaultBotProperties(parse_mode='HTML'))
    dp = build_dispatcher(db=db, shop=shop, payments=payments, config=config, access=access)
    runner = None
    workers = []
    try:
        runner = await start_server(config, shop, payments)
        await bot.delete_webhook(drop_pending_updates=False)
        jobs = [
            ('access', 5, maintain_access, bot, db, access, config),
            ('marketing', 30, dispatch_marketing, bot, db, shop),
            ('stars', config.poll_seconds, reconcile_stars, bot, db, shop),
            ('welcome_approvals', 5, retry_welcome_approvals, bot, db),
            ('external', config.poll_seconds, reconcile_external, config, db, payments),
        ]
        workers = [asyncio.create_task(run_periodically(*job), name=job[0]) for job in jobs]
        await poll_until_stopped(dp, bot)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if runner:
            await runner.cleanup()
        await payments.close()
        await bot.session.close()


if __name__=='__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info('Бот остановлен пользователем.')
