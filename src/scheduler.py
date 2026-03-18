"""
BotScheduler — lightweight scheduling engine using `schedule` library.
Runs as a background daemon thread within the bot process.
"""

import threading
import logging
import schedule
import time

logger = logging.getLogger(__name__)


class BotScheduler:
    """Schedule periodic tasks in a background daemon thread."""

    def __init__(self):
        self._scheduler = schedule.Scheduler()
        self._thread: threading.Thread | None = None
        self._running = False

    _WEEKDAY_MAP = {
        "mon": "monday", "tue": "tuesday", "wed": "wednesday",
        "thu": "thursday", "fri": "friday", "sat": "saturday", "sun": "sunday",
    }

    def add_daily(self, time_str: str, func, *args, tz: str | None = None, **kwargs):
        """Schedule a function to run daily at HH:MM (24h format).

        Args:
            time_str: Time in "HH:MM" format (e.g., "07:00")
            func: Callable to run
            tz: Optional timezone string (e.g., "Asia/Seoul")
        """
        job = self._scheduler.every().day.at(time_str, tz=tz)
        job.do(func, *args, **kwargs)
        logger.info(f"Scheduled daily job at {time_str} (tz={tz}): {func.__name__}")

    def add_weekdays(self, days: list[str], time_str: str, func, *args,
                     tz: str | None = None, **kwargs):
        """Schedule a function to run on specific weekdays at HH:MM.

        Args:
            days: List of weekday abbreviations, e.g. ["mon", "wed", "fri", "sun"]
            time_str: Time in "HH:MM" format
            func: Callable to run
            tz: Optional timezone string
        """
        for day in days:
            day_attr = self._WEEKDAY_MAP.get(day.lower())
            if not day_attr:
                logger.error(f"Unknown weekday: {day}")
                continue
            job = getattr(self._scheduler.every(), day_attr).at(time_str, tz=tz)
            job.do(func, *args, **kwargs)
            logger.info(f"Scheduled {day_attr} job at {time_str} (tz={tz}): {func.__name__}")

    def add_interval(self, minutes: int, func, *args, **kwargs):
        """Schedule a function to run every N minutes.

        Args:
            minutes: Interval in minutes
            func: Callable to run
        """
        self._scheduler.every(minutes).minutes.do(func, *args, **kwargs)
        logger.info(f"Scheduled interval job every {minutes}m: {func.__name__}")

    def _run_loop(self):
        """Background thread loop that checks and runs pending jobs."""
        while self._running:
            self._scheduler.run_pending()
            time.sleep(30)  # Check every 30 seconds

    def start(self):
        """Start the scheduler in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Scheduler already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"Scheduler started ({len(self._scheduler.jobs)} jobs)")

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Scheduler stopped")

    @property
    def jobs(self):
        return self._scheduler.jobs
