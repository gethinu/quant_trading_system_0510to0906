"""Simple signal notification utilities.

This module keeps the public entrypoints used by schedulers and scripts while
delegating the implementation to ``common.signal_notifications``.
"""

from __future__ import annotations

from common.signal_notifications import notify_signals, send_signal_notification

__all__ = ["notify_signals", "send_signal_notification"]


if __name__ == "__main__":
    notify_signals()
