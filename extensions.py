"""Shared Flask extension instances, created here to avoid circular imports."""
import sqlite3

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """SQLite hardening for cPanel/Passenger multi-process use (audit M4/M5,
    accounting-module requirement):

    * foreign_keys=ON   — makes every ondelete rule actually enforce.
    * journal_mode=WAL  — readers never block the writer; concurrent Passenger
                          processes stop hitting 'database is locked' on reads.
    * busy_timeout      — a writer waits up to 15 s for a lock instead of
                          failing immediately.
    * synchronous=NORMAL — safe with WAL, much faster than FULL.

    Runs on every new connection, any engine. Non-SQLite engines are skipped.
    The read-only 'costing' bind rejects journal_mode changes, hence the
    per-pragma guard rather than one try block.

    SQLITE_NO_WAL=1 skips the WAL pragma. WAL needs mmap of a -shm sidecar,
    which network/virtual filesystems sometimes lack ('disk I/O error').
    Local disks (the Mac, cPanel) run WAL; only set the variable on an
    environment whose filesystem cannot support WAL."""
    import os
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    pragmas = ["PRAGMA foreign_keys=ON",
               "PRAGMA busy_timeout=15000"]
    if os.environ.get("SQLITE_NO_WAL") != "1":
        pragmas.append("PRAGMA journal_mode=WAL")
    pragmas.append("PRAGMA synchronous=NORMAL")
    cursor = dbapi_connection.cursor()
    for pragma in pragmas:
        try:
            cursor.execute(pragma)
        except sqlite3.OperationalError:
            # Read-only databases refuse journal-mode changes; fine.
            pass
    cursor.close()
