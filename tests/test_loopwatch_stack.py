import sys
import loopwatch


def test_format_stack_includes_project_frames():
    out = loopwatch.format_stack(sys._getframe())
    assert "test_format_stack_includes_project_frames" in out
