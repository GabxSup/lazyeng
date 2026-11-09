"""AURUM AI entrypoint."""
from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path

from .api import create_server
from .config import APP_NAME, APP_VERSION, Config
from .db import Store
from .engine import TradingEngine

LOGGER = logging.getLogger("aurum")


def main() -> int:
    config = Config.from_env(Path(__file__).resolve().parent.parent)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    LOGGER.info("%s v%s starting", APP_NAME, APP_VERSION)
    if not config.auth_enabled and config.host not in {"127.0.0.1", "::1", "localhost"}:
        LOGGER.error(
            "Refusing to expose an unauthenticated dashboard on %s. "
            "Set AURUM_DASHBOARD_PASSWORD (and AURUM_SECRET_KEY) or bind to 127.0.0.1.",
            config.host,
        )
        return 2
    store = Store(config.data_dir / "aurum.db", config.paper_balance)
    engine = TradingEngine(config, store)
    server = create_server(config, engine)
    engine.start()

    shutting_down = threading.Event()

    def _shutdown(signum, frame) -> None:  # noqa: ARG001
        if shutting_down.is_set():
            return
        shutting_down.set()
        LOGGER.info("Shutdown signal received")

        def stop_everything() -> None:
            engine.stop()
            server.shutdown()

        threading.Thread(target=stop_everything, name="aurum-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    host = 'localhost' if config.host in {'0.0.0.0', '::', '127.0.0.1', '::1'} else config.host
    print("")
    print("  █████╗ ██╗   ██╗██████╗ ██╗   ██╗███╗   ███╗")
    print("  ██╔══██╗██║   ██║██╔══██╗██║   ██║████╗ ████║")
    print("  ███████║██║   ██║██████╔╝██║   ██║██╔████╔██║")
    print("  ██╔══██║██║   ██║██╔══██╗██║   ██║██║╚██╔╝██║")
    print("  ██║  ██║╚██████╔╝██║  ██║╚██████╔╝██║ ╚═╝ ██║")
    print("  ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝")
    print("")
    print(f"  {APP_NAME} v{APP_VERSION} · XAU/USD autonomous desk")
    print(f"  Dashboard   http://{host}:{config.port}")
    print(f"  Mode        {'PAPER (simulated)' if engine.mode == 'paper' else 'MT5 / LIVE'}")
    print(f"  Auth        {'ENABLED' if config.auth_enabled else 'DISABLED — set AURUM_DASHBOARD_PASSWORD'}")
    print(f"  Live gate   {'ENABLED — arming still required' if config.live_enabled else 'disabled'}")
    print(f"  Data dir    {config.data_dir}")
    print("")
    if not config.auth_enabled:
        LOGGER.warning("Dashboard authentication is disabled. Bind to 127.0.0.1 or set AURUM_DASHBOARD_PASSWORD.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOGGER.info("Interrupted")
    finally:
        engine.stop()
        server.server_close()
        store.close()
        LOGGER.info("Shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
