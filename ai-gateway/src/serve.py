"""Run the gateway locally: uvicorn on the event loop psycopg needs.

    uv run python src/serve.py                 # http://127.0.0.1:8000; DATABASE_URL from the environment or .env
    uv run python src/serve.py --port 8765

Why not `uvicorn app:create_app --factory` directly: on Windows uvicorn picks the Proactor event loop, on which
psycopg's async connections hang. This starts the same server on the selector loop. On Linux (the container and the
cluster) either way works, and the container uses plain uvicorn.
"""

import argparse
import asyncio
import os
import sys

import uvicorn

import migrate


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the gateway locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-database", action="store_true", help="use GATEWAY_KEYS instead of PostgreSQL")
    args = parser.parse_args()

    if not args.no_database and "DATABASE_URL" not in os.environ:
        try:
            os.environ["DATABASE_URL"] = migrate.database_url()  # from .env
        except migrate.MigrationError:
            sys.exit("no DATABASE_URL (or .env); start the database or pass --no-database")
    if args.no_database:
        os.environ.pop("DATABASE_URL", None)

    config = uvicorn.Config("app:create_app", factory=True, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(server.serve(), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
