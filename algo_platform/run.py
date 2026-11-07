#!/usr/bin/env python3
"""Starts the web app and the trading engine (same process, same asyncio loop).

Usage:
  python run.py                  start the server (binds to 127.0.0.1:8000 by default)
  python run.py genkey           print a new master encryption key for ALGO_MASTER_KEY
  python run.py migrate          apply database migrations and exit
  python run.py reset-password USERNAME   set a new password for a user (e.g. a locked-out admin)
  python run.py create-admin USERNAME     create the first admin from the server shell (so the
                                          public /setup page is never needed on a server)
"""
from __future__ import annotations

import getpass
import logging
import sys


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "uvicorn.access", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "genkey":
        from storage.crypto import Crypto

        print(Crypto.generate_key())
        return 0

    from core.config import AppConfig

    cfg = AppConfig.from_env()
    if cmd == "migrate":
        from storage.db import Database

        Database(cfg.database_url).migrate()
        print("database is up to date")
        return 0
    if cmd == "reset-password":
        if len(argv) < 3:
            print("usage: python run.py reset-password USERNAME")
            return 2
        from sqlalchemy import select

        from app.auth.security import hash_password, password_problems
        from storage.db import Database
        from storage.models import User

        db = Database(cfg.database_url)
        db.migrate()
        username = argv[2].strip().lower()
        pw = getpass.getpass(f"New password for {username}: ")
        if pw != getpass.getpass("Repeat: "):
            print("passwords do not match")
            return 1
        problems = password_problems(pw, username)
        if problems:
            print("password " + "; ".join(problems))
            return 1
        with db.session() as s:
            u = s.scalar(select(User).where(User.username == username))
            if u is None:
                print(f"no such user {username}")
                return 1
            u.password_hash, u.failed_attempts, u.locked_until, u.is_active = hash_password(pw), 0, None, True
        print(f"password updated for {username}")
        return 0
    if cmd == "create-admin":
        if len(argv) < 3:
            print("usage: python run.py create-admin USERNAME")
            return 2
        from app.auth.security import hash_password, password_problems, user_count
        from storage.db import Database
        from storage.models import User

        db = Database(cfg.database_url)
        db.migrate()
        if user_count(db) > 0:
            print("users already exist; use reset-password or the Users page instead")
            return 1
        username = argv[2].strip().lower()
        if not username or not username.replace("_", "").replace(".", "").isalnum() or len(username) > 64:
            print("username: letters, digits, '.' and '_' only")
            return 1
        pw = getpass.getpass(f"Password for {username}: ")
        if pw != getpass.getpass("Repeat: "):
            print("passwords do not match")
            return 1
        problems = password_problems(pw, username)
        if problems:
            print("password " + "; ".join(problems))
            return 1
        with db.session() as s:
            s.add(User(username=username, password_hash=hash_password(pw), role="admin"))
        print(f"admin {username} created")
        return 0
    if cmd != "serve":
        print(__doc__)
        return 2

    if not cfg.master_key:
        print("ALGO_MASTER_KEY is not set.\n  1. python run.py genkey\n  2. put ALGO_MASTER_KEY=<key> in .env "
              "(or your environment)\n  3. python run.py", file=sys.stderr)
        return 1
    _logging()
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger("algo").warning("binding to %s — make sure this is behind an HTTPS reverse proxy", cfg.host)
    import uvicorn

    from app.main import create_app

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info", proxy_headers=cfg.trust_proxy_headers,
                forwarded_allow_ips=cfg.forwarded_allow_ips if cfg.trust_proxy_headers else None, ws_ping_interval=20)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
