import logging

import logging_setup as LS


def test_benign_telegram_errors_are_dropped_from_console_but_real_ones_kept():
    f = LS._BenignTelegramFilter()

    def rec(msg):
        try:
            raise RuntimeError(msg)
        except RuntimeError:
            import sys
            return logging.LogRecord("x", logging.ERROR, __file__, 1, "m", (), sys.exc_info())
    assert not f.filter(rec("Telegram server says - Bad Request: message is not modified: specified new"))
    assert not f.filter(rec("Bad Request: message to delete not found"))
    assert f.filter(rec("Unauthorized"))
    assert f.filter(rec("Cannot connect to host x:443"))


def test_console_handler_has_the_filter(monkeypatch):
    root = logging.getLogger()
    old_handlers, old_cfg = list(root.handlers), LS._configured
    try:
        LS._configured = False
        root.handlers = []
        LS.setup_logging()
        stream = [h for h in root.handlers if type(h) is logging.StreamHandler]
        assert stream and any(isinstance(x, LS._BenignTelegramFilter) for x in stream[0].filters)
    finally:
        for h in root.handlers:
            if h not in old_handlers:
                h.close()
        root.handlers = old_handlers
        LS._configured = old_cfg
