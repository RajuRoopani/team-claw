"""Container entrypoint — configures logging and starts the agent event loop."""
import asyncio
import logging
import os
import signal
import sys


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    role = os.environ.get("ROLE", "unknown")
    logging.basicConfig(
        level=level,
        format=f"%(asctime)s  [{role}]  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


async def main() -> None:
    configure_logging()
    from agent import Agent  # imported here so logging is configured first

    agent = Agent()

    loop = asyncio.get_running_loop()

    def _shutdown(sig: signal.Signals) -> None:
        logging.getLogger(__name__).info("Received %s, shutting down.", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        await agent.run()
    finally:
        await agent.bus.close()


if __name__ == "__main__":
    asyncio.run(main())
