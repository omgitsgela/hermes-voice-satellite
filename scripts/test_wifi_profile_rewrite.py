#!/usr/bin/env python3
"""Rewrite a synthetic NetworkManager keyfile without touching a card (#87)."""
import configparser
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "wifi_provision", Path(__file__).with_name("add-wifi-profile-to-card.py"))
assert spec is not None and spec.loader is not None
wifi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wifi)


class RewriteTests(unittest.TestCase):
    def test_source_device_bindings_removed_and_network_settings_preserved(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict({
            "connection": {"id": "source", "uuid": "old", "type": "wifi",
                           "interface-name": "wlan9", "master": "source-master",
                           "slave-type": "bridge", "mac-address": "02:00:00:00:00:01"},
            "wifi": {"ssid": "test-network", "mode": "infrastructure",
                     "mac-address": "02:00:00:00:00:01",
                     "cloned-mac-address": "02:00:00:00:00:02",
                     "mac-address-denylist": "02:00:00:00:00:03;"},
            "wifi-security": {"key-mgmt": "wpa-psk", "psk": "synthetic-test-value"},
            "ipv4": {"method": "auto"},
        })
        wifi.rewrite(parser, name="device-network", priority=5)
        for key in ("mac-address", "cloned-mac-address", "mac-address-denylist"):
            self.assertFalse(parser.has_option("wifi", key), key)
        for key in ("interface-name", "master", "slave-type", "mac-address"):
            self.assertFalse(parser.has_option("connection", key), key)
        self.assertEqual(parser["connection"]["type"], "wifi")
        self.assertEqual(parser["connection"]["id"], "device-network")
        self.assertNotEqual(parser["connection"]["uuid"], "old")
        self.assertEqual(parser["connection"]["autoconnect"], "true")
        self.assertEqual(parser["connection"]["autoconnect-priority"], "5")
        self.assertEqual(dict(parser["wifi"]), {"ssid": "test-network", "mode": "infrastructure"})
        self.assertEqual(parser["wifi-security"]["psk"], "synthetic-test-value")
        self.assertEqual(parser["ipv4"]["method"], "auto")


if __name__ == "__main__":
    unittest.main()
