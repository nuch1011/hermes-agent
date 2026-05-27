from __future__ import annotations

import sqlite3

import pytest

from gateway import run as gateway_run


def test_disk_io_error_is_transient_not_corrupt() -> None:
    exc = sqlite3.OperationalError("disk I/O error")

    assert gateway_run._is_transient_kanban_sqlite_error(exc) is True
    assert gateway_run._is_corrupt_board_db_error(exc) is False


def test_corrupt_db_error_is_not_transient() -> None:
    exc = sqlite3.DatabaseError("file is not a database")

    assert gateway_run._is_corrupt_board_db_error(exc) is True
    assert gateway_run._is_transient_kanban_sqlite_error(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database is busy"),
        sqlite3.OperationalError("unable to open database file"),
    ],
)
def test_common_operational_errors_are_transient(exc: sqlite3.OperationalError) -> None:
    assert gateway_run._is_transient_kanban_sqlite_error(exc) is True
