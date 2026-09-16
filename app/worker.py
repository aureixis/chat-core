from __future__ import annotations

import asyncio
import logging
import time

from .config import get_settings
from .database import SessionLocal
from .services.agents import (
    execute_next_action,
    recover_expired_actions,
    schedule_due_robots,
    schedule_pending_vectors,
)
from .services.audit_export import export_pending_audit_events
from .services.tools import expire_tool_invocations
from .tenancy import apply_worker_context

logger = logging.getLogger(__name__)


def run_worker_iteration() -> int:
    settings = get_settings()
    settings.validate_vector_configuration()
    database = SessionLocal()
    apply_worker_context(database)
    processed = 0
    try:
        recover_expired_actions(database)
        expire_tool_invocations(database)
        export_pending_audit_events(database)
        schedule_pending_vectors(database)
        schedule_due_robots(database)
        database.commit()
        for _ in range(settings.agent_batch_size):
            action = execute_next_action(database)
            if not action:
                break
            processed += 1
    except Exception:
        database.rollback()
        logger.exception("Agent worker iteration failed")
    finally:
        database.close()
    return processed


async def agent_worker_loop() -> None:
    settings = get_settings()
    while True:
        await asyncio.to_thread(run_worker_iteration)
        await asyncio.sleep(settings.agent_poll_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    while True:
        run_worker_iteration()
        time.sleep(settings.agent_poll_seconds)


if __name__ == "__main__":
    main()
