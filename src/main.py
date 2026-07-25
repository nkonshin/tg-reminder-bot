import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.calendar_client import CalendarClient
from src.config import Config
from src.db import Database
from src.flow import Deps, run_scheduler
from src.handlers import router

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    db = Database(cfg.db_path)
    await db.init()
    bot = Bot(cfg.bot_token)
    deps = Deps(bot=bot, db=db, cal=CalendarClient(cfg), cfg=cfg)

    dp = Dispatcher()
    dp["deps"] = deps
    dp.include_router(router)

    scheduler = asyncio.create_task(run_scheduler(deps))
    try:
        log.info("starting polling")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
