from __future__ import annotations

from hwmusic.app import describe_connection_error
from hwmusic.session import SessionError


def test_write_not_permitted_gets_a_pairing_hint() -> None:
    error = Exception("(3, 'GATT Protocol Error: Write Not Permitted')")
    message = describe_connection_error(error)
    assert "系统级蓝牙配对" in message
    assert "解除配对" in message
    assert "Write Not Permitted" in message


def test_insufficient_authentication_gets_a_pairing_hint() -> None:
    message = describe_connection_error(Exception("Insufficient authentication"))
    assert "系统级蓝牙配对" in message


def test_session_errors_pass_through_unchanged() -> None:
    assert describe_connection_error(SessionError("配对超时")) == "配对超时"


def test_other_errors_are_wrapped() -> None:
    assert describe_connection_error(ValueError("boom")).startswith("连接失败：")
