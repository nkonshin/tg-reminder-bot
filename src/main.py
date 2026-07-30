import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.calendar_client import CalendarClient
from src.config import Config
from src.db import Database
from src.flow import Deps, run_scheduler
from src.handlers import router
from src.handlers_monitor import router as monitor_router
from src.monitor.capture import MonitorDeps
from src.monitor.retention import run_sweeper
from src.monitor.store import MonitorStore

log = logging.getLogger(__name__)


def _start_background_tasks(deps: Deps, monitor: MonitorDeps, cfg: Config) -> list:
    """The scheduler always runs; the sweeper only when the monitor itself is
    switched on. It deletes rows and unlinks media files past their retention
    window, so leaving it running with monitor_enabled=False would keep
    pruning the archive even though the feature that's supposed to be "off"
    is meant to do nothing."""
    tasks = [asyncio.create_task(run_scheduler(deps))]
    if cfg.monitor_enabled:
        tasks.append(asyncio.create_task(run_sweeper(monitor)))
    return tasks


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    db = Database(cfg.db_path)
    await db.init()
    bot = Bot(cfg.bot_token)
    deps = Deps(bot=bot, db=db, cal=CalendarClient(cfg), cfg=cfg)

    monitor_store = MonitorStore(cfg.db_path)
    await monitor_store.init()
    monitor = MonitorDeps(bot=bot, store=monitor_store, cfg=cfg)

    dp = Dispatcher()
    dp["deps"] = deps
    dp["monitor"] = monitor
    dp.include_router(monitor_router)   # first: it filters adm: callbacks
    dp.include_router(router)

    background_tasks = _start_background_tasks(deps, monitor, cfg)
    try:
        log.info("starting polling")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
