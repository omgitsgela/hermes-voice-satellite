#!/usr/bin/env python3
"""Plugin availability uses exactly the request authentication sources (#88)."""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hermes/plugins"))


class AvailabilityTests(unittest.TestCase):
    def test_both_plugins_accept_environment_or_readable_nonempty_file(self):
        for name, implementation in (("voice_satellite", "voice_tool"),
                                     ("hermes_timers", "timer_tool")):
            plugin = importlib.import_module(name)
            tool = importlib.import_module(f"{name}.{implementation}")
            for source in ("environment", "file", "empty", "whitespace", "missing", "unreadable"):
                with self.subTest(plugin=name, source=source), tempfile.TemporaryDirectory() as tmp:
                    token_file = Path(tmp) / "token"
                    env = " test-environment-token " if source == "environment" else " \t"
                    if source not in ("missing", "environment"):
                        token_file.write_text({"empty": "", "whitespace": " \n"}.get(
                            source, " test-file-token\n"))
                    if source == "unreadable":
                        token_file.unlink()
                        token_file.mkdir()  # read_text raises OSError, even when run as root
                    with patch.dict(os.environ, {"VOICE_ANNOUNCE_TOKEN": env}), \
                            patch.object(tool, "TOKEN_FILE", token_file):
                        expected = source in ("environment", "file")
                        self.assertEqual(plugin._bridge_available(), expected)
                        if expected:
                            self.assertEqual(tool._token(), f"test-{source}-token")
                        else:
                            with self.assertRaises((plugin.VoiceControlError if name == "voice_satellite"
                                                   else plugin.TimerToolError)):
                                tool._token()

    def test_environment_takes_precedence_over_file(self):
        for name, implementation in (("voice_satellite", "voice_tool"),
                                     ("hermes_timers", "timer_tool")):
            plugin = importlib.import_module(name)
            tool = importlib.import_module(f"{name}.{implementation}")
            with self.subTest(plugin=name), patch.dict(os.environ, {
                "VOICE_ANNOUNCE_TOKEN": "test-environment-token"
            }), patch.object(tool, "TOKEN_FILE") as file:
                file.read_text.side_effect = PermissionError("not readable")
                self.assertTrue(plugin._bridge_available())
                self.assertEqual(tool._token(), "test-environment-token")
                file.read_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
