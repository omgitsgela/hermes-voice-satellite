"""Session and HTTP interface checks, against the real server on loopback.

    python packages/installer/tests/run_session_tests.py
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for package in ("installer", "gateway", "device-esp32", "device-pi"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

import hermes_device_esp32  # noqa: E402
import hermes_device_pi  # noqa: E402
from hermes_installer.registry import DeviceRegistry  # noqa: E402
from hermes_installer.secrets import CredentialStore  # noqa: E402
from hermes_installer.server import Handler  # noqa: E402
from hermes_installer.session import Session  # noqa: E402


def mock_home(path):
    """Patch the function Path.home() calls, which honours HOME only on POSIX."""
    from unittest import mock

    return mock.patch("pathlib.Path.home", return_value=Path(path))


class MemoryBackend:
    name = "memory"

    def __init__(self):
        self.data = {}

    def save(self, account, secret):
        self.data[account] = secret

    def load(self, account):
        return self.data.get(account)

    def forget(self, account):
        self.data.pop(account, None)


#: Device operations run the repository's shell scripts, so a driver refuses on Windows. Tests that
#: expect a command to be built are POSIX-only; the refusal itself is asserted in the device suite.
POSIX = sys.platform != "win32"
posix_only = unittest.skipUnless(POSIX, "device operations run the repository's shell scripts")


def build_session(dry_run=True):
    registry = DeviceRegistry()
    registry.register(hermes_device_esp32.PROFILE, hermes_device_esp32.DRIVER)
    registry.register(hermes_device_pi.PROFILE, hermes_device_pi.DRIVER)
    return Session(ROOT, registry=registry, store=CredentialStore(backend=MemoryBackend()),
                   dry_run=dry_run)


class InterfaceSendsIdentifiers(unittest.TestCase):
    """The interface must send what the device understands, not what the person reads.

    The wake-word list shows "hey hermes" and the model is called "hey_hermes". Reading the selected
    option's text sent the phrase, so the ESP32 driver did not recognise its own default and
    provisioned a different model than the one on screen.
    """

    def test_the_wake_word_comes_from_the_option_value(self):
        page = (Path(__file__).resolve().parents[1] / "src" / "hermes_installer" / "web"
                / "index.html").read_text()
        start = page.index("function deviceFields")
        end = page.index("async function ensureWakeModel")
        body = page[start:end]
        self.assertIn('$("wake-select").value', body)
        self.assertNotIn("selectedOptions[0]", body,
                         "the display text is not an identifier")
        self.assertNotIn("textContent", body)


class WakeModelUpload(unittest.TestCase):
    """A wake model chosen in a file dialog: staged privately, then handed to the provisioning step."""

    @staticmethod
    def upload(name="mine.tflite", body=b"model-bytes"):
        import base64

        return [{"name": name, "content_b64": base64.b64encode(body).decode()}]

    def test_a_model_is_staged_privately_and_named_for_the_selector(self):
        import os
        import stat

        from hermes_installer.session import Session

        result = Session(Path.cwd(), dry_run=True).wake_model_from_upload(self.upload())
        self.assertTrue(result["ok"], result)
        staged = Path(result["path"])
        self.assertTrue(staged.exists())
        self.assertEqual(result["name"], "mine", "the selector is the file stem")
        self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
        os.unlink(staged)

    def test_a_file_that_is_not_a_wake_model_is_refused(self):
        from hermes_installer.session import Session

        result = Session(Path.cwd(), dry_run=True).wake_model_from_upload(self.upload("song.mp3"))
        self.assertFalse(result["ok"])
        self.assertIn("not a wake model", result["error"])

    def test_a_path_in_the_filename_cannot_escape(self):
        from hermes_installer.session import Session

        result = Session(Path.cwd(), dry_run=True).wake_model_from_upload(
            self.upload("../../etc/passwd.tflite")
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(result["path"]).name, "passwd.tflite")
        Path(result["path"]).unlink(missing_ok=True)

    def test_an_absurdly_large_file_is_refused(self):
        from hermes_installer.session import Session

        result = Session(Path.cwd(), dry_run=True).wake_model_from_upload(
            self.upload(body=b"x" * (9 * 1024 * 1024))
        )
        self.assertFalse(result["ok"])
        self.assertIn("larger than a wake model", result["error"])


class VoiceUpload(unittest.TestCase):
    """A voice chosen in a file dialog arrives as bytes, because a browser hides the path.

    The pair is staged privately, validated by the same rules a typed path gets, and the staging
    directory is removed whatever happens -- a half-kept voice on disk later looks like a supported
    one.
    """

    @staticmethod
    def pair(model_name="mine.onnx", with_config=True):
        import base64

        files = [{"name": model_name,
                  "content_b64": base64.b64encode(b"onnx-bytes").decode()}]
        if with_config:
            files.append({"name": model_name + ".json",
                          "content_b64": base64.b64encode(b"{}").decode()})
        return files

    def test_a_chosen_pair_is_accepted(self):
        from hermes_installer.session import Session

        session = Session(Path.cwd(), dry_run=True)
        result = session.voice_from_upload(self.pair(), action="preview")
        self.assertTrue(result["ok"], result)

    def test_a_model_without_its_config_is_refused(self):
        from hermes_installer.session import Session

        session = Session(Path.cwd(), dry_run=True)
        result = session.voice_from_upload(self.pair(with_config=False), action="preview")
        self.assertFalse(result["ok"])
        self.assertIn("config is missing", result["error"])

    def test_a_path_in_the_filename_cannot_escape(self):
        from hermes_installer.session import Session

        session = Session(Path.cwd(), dry_run=True)
        result = session.voice_from_upload(
            self.pair(model_name="../../etc/evil.onnx", with_config=False), action="preview"
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("config is missing", result["error"], "the name should have been reduced to a leaf")

    def test_nothing_is_left_behind(self):
        import glob
        import tempfile

        from hermes_installer.session import Session

        before = set(glob.glob(str(Path(tempfile.gettempdir()) / "voice-upload-*")))
        Session(Path.cwd(), dry_run=True).voice_from_upload(self.pair(), action="preview")
        after = set(glob.glob(str(Path(tempfile.gettempdir()) / "voice-upload-*")))
        self.assertEqual(before, after, "a staging directory outlived the request")


class SessionBehaviour(unittest.TestCase):
    def setUp(self):
        self.session = build_session()

    def test_status_explains_what_it_found(self):
        status = self.session.status()
        self.assertIn(status["hermes"], ("desktop", "service", "none"))
        self.assertTrue(status["hermes_explained"])

    def test_both_devices_are_offered_with_their_capabilities(self):
        devices = {d["id"]: d for d in self.session.status()["devices"]}
        self.assertEqual(set(devices), {"esp32-xvf3800", "pi-reSpeaker-2mic"})
        self.assertIn("playback_volume", devices["esp32-xvf3800"]["remote_settings"])
        self.assertIn("wifi", devices["esp32-xvf3800"]["cable_settings"])

    def test_a_loopback_dial_address_is_refused(self):
        result = self.session.install_gateway("127.0.0.1")
        self.assertFalse(result["ok"])
        self.assertIn("cannot be reached by a device", result["error"])

    def test_the_gateway_plan_matches_the_script_flags(self):
        result = self.session.install_gateway("192.0.2.10")
        self.assertTrue(result["ok"])
        argv = result["commands"][0]
        self.assertIn("--device-reachable-as", argv)
        self.assertEqual(argv[argv.index("--device-reachable-as") + 1], "192.0.2.10")

    def test_a_dry_run_never_executes(self):
        result = self.session.install_gateway("192.0.2.10")
        self.assertTrue(result["dry_run"])

    @posix_only
    def test_flash_previews_the_command_for_a_cable_device(self):
        result = self.session.flash(
            "esp32-xvf3800", device_name="kitchen",
            dial_address="tls://192.0.2.10:10702", port="/dev/ttyACM0",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertIn("install-esp32.sh", result["commands"][0]["argv"][0])

    def test_flash_without_a_port_refuses_cleanly(self):
        result = self.session.flash("esp32-xvf3800", device_name="k",
                                    dial_address="tls://192.0.2.10:10702")
        self.assertFalse(result["ok"])
        self.assertIn("USB port", result["summary"])

    def test_a_platform_that_cannot_do_it_is_an_answer_not_a_traceback(self):
        import sys
        from unittest import mock

        real = sys.platform
        with mock.patch.object(sys, "platform", "win32"):
            result = self.session.flash("esp32-xvf3800", device_name="k",
                                        dial_address="tls://192.0.2.10:10702",
                                        port="COM3", dry_run=False)
        self.assertEqual(sys.platform, real)
        self.assertFalse(result["ok"])
        self.assertIn("Linux or macOS", result["error"])

    def test_unknown_device_lists_the_known_ones(self):
        result = self.session.flash("nope", device_name="k")
        self.assertFalse(result["ok"])
        self.assertIn("esp32-xvf3800", result["error"])

    def test_a_preview_session_cannot_be_talked_into_a_real_run(self):
        # The session's dry-run is a ceiling. A client sending dry_run:false must not beat it, so
        # this intercepts the one function that would actually execute.
        import hermes_gateway

        seen = {}
        original = hermes_gateway.run_commands

        def spy(commands, dry_run=False, cwd=None, env=None, executor=None):
            seen["dry_run"] = dry_run
            seen["cwd"] = cwd
            return True, "preview"

        hermes_gateway.run_commands = spy
        try:
            build_session(dry_run=True).install_gateway("192.0.2.10", dry_run=False)
        finally:
            hermes_gateway.run_commands = original
        self.assertIs(seen.get("dry_run"), True, "a preview session asked to execute for real")
        # The engine also has to run in the checkout being installed, not the launcher's directory.
        self.assertEqual(Path(str(seen.get("cwd"))).resolve(), Path(seen.get("cwd")).resolve())
        self.assertIsNotNone(seen.get("cwd"), "the install commands were started without a working directory")

    def test_a_practice_session_does_not_enrol_or_store_anything(self):
        import hermes_installer.pairing as pairing_module

        called = {"pair": 0}
        original = pairing_module.pair

        def spy(*args, **kwargs):
            called["pair"] += 1
            return original(*args, **kwargs)

        pairing_module.pair = spy
        try:
            result = build_session(dry_run=True).pair(
                "192.0.2.10", 10704, "a b c d e", "kitchen", expected_code="ABCD-EFGH",
                remember=True,
            )
        finally:
            pairing_module.pair = original
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("practice"))
        self.assertEqual(called["pair"], 0, "a practice session asked the gateway to enrol")
        self.assertIsNone(build_session(dry_run=True).store.recall("192.0.2.10", 10704))

    @posix_only
    def test_installing_a_device_runs_the_staging_stage_too(self):
        # Flashing alone leaves Wi-Fi and credentials unstaged, which is why the review flagged a
        # device that "installs" and then cannot connect.
        result = self.session.install_device(
            "esp32-xvf3800", device_name="kitchen", device_id="esp32-0011223344",
            dial_address="tls://192.0.2.10:10702", port="/dev/ttyACM0", wifi_ssid="home",
            wifi_password="correct horse battery staple", certs_dir=str(ROOT / "packages"),
            dry_run=True,
        )
        self.assertTrue(result["ok"])
        stages = [s["stage"] for s in result.get("stages", [])]
        self.assertEqual(stages, ["flash", "provision"])
        actions = " ".join(" ".join(c["argv"]) for c in result["commands"])
        self.assertIn("--flash", actions)
        self.assertIn("--provision", actions)

    def test_a_gateway_install_leaves_a_startable_pairing_unit(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("pathlib.Path.home", return_value=Path(tmp)):
                session = build_session(dry_run=False)
                unit = session._write_pairing_unit("192.0.2.10", 10702)
                self.assertIsNotNone(unit, "no pairing unit was written")
                self.assertTrue(unit["path"].exists())
                text = unit["path"].read_text()
                self.assertIn("tls://192.0.2.10:10702", text)
                self.assertIn("--port 10704", text)
                joined = " ".join(unit["instructions"])
                self.assertIn("systemctl --user enable --now hermes-pairing.service", joined)

    def test_pairing_materialises_the_files_the_provisioner_reads(self):
        import tempfile
        from pathlib import Path

        from hermes_installer.pairing import PairedCredentials

        with tempfile.TemporaryDirectory() as tmp:
            session = build_session(dry_run=False)
            session.repo_root = Path(tmp)
            credentials = PairedCredentials(
                gateway=None, identity_code="ABCD-EFGH", ca_pem="CA", cert_pem="CERT",
                key_pem="KEY", server_pin="PIN", dial_address="tls://h:10702",
                server_cert_pem="SERVER", device_id="esp32-0011223344",
                device_fingerprint="abc123",
            )
            directory = Path(session._materialise_credentials(credentials))
            names = sorted(p.name for p in directory.iterdir())
            self.assertEqual(names, sorted([
                "client-ca.crt", "esp32-0011223344.crt", "esp32-0011223344.key", "server.crt",
                "device-credentials.json",
            ]))
            import json as _json

            record = _json.loads((directory / "device-credentials.json").read_text())
            self.assertEqual(record, {"abc123": "esp32-0011223344"},
                             "the registry record must map the fingerprint to this device only")
            session.close()
            self.assertFalse(directory.exists(), "temporary credentials outlived the session")

    def test_a_voice_download_fetches_the_model_and_its_config(self):
        import tempfile

        from hermes_installer import catalogue

        seen = []

        def fake_download(url, destination, **_kwargs):
            seen.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x")
            return destination

        original = catalogue.download
        catalogue.download = fake_download
        try:
            with tempfile.TemporaryDirectory() as tmp:
                # The voice is installed into the checkout's own voices/ directory, so the test has
                # to own that directory: writing into the real checkout would pass here and leak.
                session = build_session(dry_run=False)
                session.repo_root = Path(tmp)
                with mock_home(tmp):
                    result = session.download_voice("en_US-amy-medium")
        finally:
            catalogue.download = original

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(seen), 2, "the model and its config must both be fetched")
        self.assertIn("/en/en_US/amy/medium/en_US-amy-medium.onnx", seen[0])
        self.assertTrue(seen[1].endswith(".onnx.json"))

    def test_the_default_voices_are_fetched_from_the_published_path(self):
        """The speaker folder keeps its underscore: `hfc_female`, never `hfc/female`.

        Splitting on the underscore built a URL that 404s for both voices we ship as defaults, so
        previewing worked and downloading never did.
        """
        import tempfile

        from hermes_installer import catalogue

        seen = []

        def fake_download(url, destination, **_kwargs):
            seen.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x")
            return destination

        original = catalogue.download
        catalogue.download = fake_download
        try:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                session = build_session(dry_run=False)
                session.repo_root = repo
                result = session.download_voice("en_US-hfc_female-medium")
        finally:
            catalogue.download = original

        self.assertTrue(result["ok"], result)
        self.assertTrue(seen, "nothing was fetched")
        self.assertIn("/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx", seen[0])

    def test_applying_a_voice_points_the_gateway_at_it(self):
        import tempfile

        from hermes_installer import catalogue, session as session_module

        def fake_download(url, destination, **_kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x")
            return destination

        original = catalogue.download
        catalogue.download = fake_download
        try:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                (repo / "satellite-gateway.env").write_text("VOICE_PIPER_VOICE=en_US-hfc_female-medium\nKEEP=me\n")
                session = build_session(dry_run=False)
                session.repo_root = repo
                with mock_home(str(repo / "home")):
                    result = session.download_voice("en_US-amy-medium", apply=True)
                text = (repo / "satellite-gateway.env").read_text()
                # The temporary directory is gone after this block, so record what landed inside it.
                installed = (
                    (repo / "voices" / "en_US-amy-medium.onnx").exists(),
                    (repo / "voices" / "en_US-amy-medium.onnx.json").exists(),
                )
        finally:
            catalogue.download = original

        self.assertTrue(result["ok"], result)
        self.assertIn("VOICE_PIPER_VOICE=en_US-amy-medium", text)
        self.assertIn("KEEP=me", text, "the rest of the environment file must survive")
        # The service loads <checkout>/voices/<name>.onnx: the files have to be THERE, and the name
        # is the only thing that selects them. VOICE_CONFIG was written for years and read by nothing.
        self.assertEqual(installed, (True, True), result)
        self.assertNotIn("VOICE_CONFIG=", text, "an inert key must not be written as if it selected the voice")

    def test_a_voice_file_must_arrive_with_its_config(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "my_voice.onnx"
            model.write_bytes(b"model")
            self.assertIn("config is missing", " ".join(self.session.voice_file_problems(str(model))))
            (Path(str(model) + ".json")).write_text("{}")
            self.assertEqual(self.session.voice_file_problems(str(model)), [])

    def test_a_voice_file_preview_and_install_do_nothing_in_a_preview_session(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "my_voice.onnx"
            model.write_bytes(b"model")
            Path(str(model) + ".json").write_text("{}")
            preview = self.session.preview_voice_file(str(model))
            install = self.session.install_voice_file(str(model))
        self.assertTrue(preview.get("practice"))
        self.assertTrue(install.get("practice"))
        self.assertFalse((self.session.repo_root / "voices" / "my_voice.onnx").exists())

    def test_installing_a_voice_file_copies_both_files_and_selects_it(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "voices").mkdir(parents=True)
            (repo / "satellite-gateway.env").write_text("VOICE_PIPER_VOICE=old\nKEEP=me\n")
            model = Path(tmp) / "my_voice.onnx"
            model.write_bytes(b"model")
            Path(str(model) + ".json").write_text('{"audio": {}}')
            session = build_session(dry_run=False)
            session.repo_root = repo
            result = session.install_voice_file(str(model))
            env = (repo / "satellite-gateway.env").read_text()
            model_copied = (repo / "voices" / "my_voice.onnx").exists()
            config_copied = (repo / "voices" / "my_voice.onnx.json").exists()
        self.assertTrue(result["ok"], result)
        self.assertTrue(model_copied, "the model was not copied next to the others")
        self.assertTrue(config_copied, "the config was not copied beside it")
        self.assertIn("VOICE_PIPER_VOICE=my_voice", env)
        self.assertIn("KEEP=me", env)

    def test_community_measurements_are_read_only(self):
        from hermes_installer import catalogue

        original = catalogue.refresh_measurements
        catalogue.refresh_measurements = lambda query: [
            {"id": 2654, "word": "Hey Hermes", "downloads": 35, "recall": 74.2,
             "fa_per_hour": 0.87, "tflite": True}
        ]
        try:
            result = self.session.community_wake_words("hermes")
        finally:
            catalogue.refresh_measurements = original
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["recall"], 74.2)
        self.assertIn("account", result["note"])

    def test_preflight_refuses_a_board_without_a_network(self):
        # The board's staging step stages the network and refuses without it, so the refusal has to
        # come before the firmware is written, not after.
        check = self.session.preflight(
            "esp32-xvf3800", device_name="kitchen", dial_address="tls://192.0.2.10:10702",
            port="/dev/ttyACM0", device_id="esp32-0011223344",
        )
        self.assertFalse(check["ok"])
        self.assertTrue(any("Wi-Fi" in problem for problem in check["problems"]))

    def test_preflight_collects_every_problem_at_once(self):
        check = self.session.preflight("esp32-xvf3800")
        self.assertGreaterEqual(len(check["problems"]), 3)

    def test_preflight_passes_for_a_ready_board(self):
        check = self.session.preflight(
            "esp32-xvf3800", device_name="kitchen", dial_address="tls://192.0.2.10:10702",
            port="/dev/ttyACM0", device_id="esp32-0011223344", wifi_ssid="home",
            wifi_password="correct horse battery staple",
        )
        self.assertTrue(check["ok"], check["problems"])

    def test_install_refuses_before_writing_when_not_ready(self):
        import hermes_installer.driver as driver_module

        written = []
        original = driver_module.Runner.run

        def spy(self, command):
            written.append(command)
            return original(self, command)

        driver_module.Runner.run = spy
        try:
            result = self.session.install_device(
                "esp32-xvf3800", device_name="kitchen",
                dial_address="tls://192.0.2.10:10702", port="/dev/ttyACM0",
            )
        finally:
            driver_module.Runner.run = original
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])
        self.assertEqual(written, [], "something ran before the setup was checked")

    @posix_only
    def test_managing_a_device_stages_without_flashing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as card:
            (Path(card) / "etc").mkdir()
            (Path(card) / "home" / "satellite").mkdir(parents=True)
            result = self.session.manage_device(
                "pi-reSpeaker-2mic", device_name="bedroom", dial_address="tls://192.0.2.10:10702",
                card_mount=card, wifi_ssid="home", dry_run=True,
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result.get("mode"), "manage")
        joined = " ".join(" ".join(c["argv"]) for c in result["commands"])
        self.assertIn("add-wifi-profile-to-card.py", joined)
        self.assertNotIn("install-esp32", joined)

    @posix_only
    def test_managing_with_nothing_to_change_says_so(self):
        import tempfile

        with tempfile.TemporaryDirectory() as card:
            (Path(card) / "etc").mkdir()
            result = self.session.manage_device(
                "pi-reSpeaker-2mic", device_name="bedroom", dial_address="tls://h:10702",
                card_mount=card, dry_run=True,
            )
        self.assertFalse(result["ok"])
        self.assertIn("nothing to change", result["error"].lower())

    def test_the_platform_report_states_what_this_machine_can_do(self):
        report = self.session.platform_report()
        self.assertIn("can_flash", report)
        self.assertIn("can_supervise", report)
        self.assertIsInstance(report["notes"], list)

    def test_closing_removes_every_credential_directory(self):
        import tempfile
        from pathlib import Path as _Path

        from hermes_installer.pairing import PairedCredentials

        made = []
        with tempfile.TemporaryDirectory() as tmp:
            session = build_session(dry_run=False)
            session.repo_root = _Path(tmp)
            for index in range(3):
                credentials = PairedCredentials(
                    gateway=None, identity_code="ABCD-EFGH", ca_pem="CA", cert_pem="CERT",
                    key_pem="KEY", server_pin="PIN", dial_address="tls://h:10702",
                    server_cert_pem="SERVER", device_id=f"device-{index}",
                )
                made.append(_Path(session._materialise_credentials(credentials)))
            # Each pairing supersedes the last, so at most one directory is live at a time.
            live = [path for path in made if path.exists()]
            self.assertLessEqual(len(live), 1)
            session.close()
            self.assertEqual([p for p in made if p.exists()], [],
                             "a private key survived the session")

    def test_pairing_without_a_confirmed_code_is_refused(self):
        result = self.session.pair("192.0.2.10", 10702, "a b c d e", "kitchen")
        self.assertFalse(result["ok"])
        self.assertIn("confirm the gateway's identity code", result["error"])
        # And nothing was remembered.
        self.assertIsNone(self.session.store.recall("192.0.2.10", 10702))


class HttpInterface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session = build_session()
        cls.token = "test-token-not-a-secret"
        handler = type("Bound", (Handler,), {"session": session, "token": cls.token})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get_json(self, path):
        request = urllib.request.Request(
            self.base + path, headers={"X-Installer-Token": self.token}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def post_json(self, path, payload, token=None, content_type="application/json"):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": content_type,
                     "X-Installer-Token": self.token if token is None else token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            return json.loads(error.read())

    def test_the_page_is_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            body = response.read().decode()
        self.assertIn("Hermes Voice Satellite", body)
        self.assertIn("identity code", body)
        # The first screen must not tell a person they can set up a gateway they cannot.
        self.assertIn("Hermes was not found on this computer", body)

    def test_status_is_json(self):
        status = self.get_json("/api/status")
        self.assertEqual(len(status["devices"]), 2)

    def test_gateway_preview_over_http(self):
        result = self.post_json("/api/gateway", {"dial_address": "192.0.2.10", "dry_run": True})
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])

    def test_a_bad_address_is_a_clean_error(self):
        result = self.post_json("/api/gateway", {"dial_address": "127.0.0.1"})
        self.assertFalse(result["ok"])
        self.assertIn("cannot be reached", result["error"])

    @posix_only
    @posix_only
    def test_flash_over_http(self):
        import tempfile

        with tempfile.TemporaryDirectory() as card:
            (Path(card) / "etc").mkdir()
            result = self.post_json("/api/flash", {
                "device_id": "pi-reSpeaker-2mic", "device_name": "bedroom",
                "dial_address": "tls://192.0.2.10:10702", "card_mount": card,
                "dry_run": True,
            })
        self.assertTrue(result["ok"])
        # The card writer, not the on-device installer: install-pi.sh writes to the running
        # machine's /etc and would have installed services on the workbench.
        self.assertIn("deploy-to-card.sh", result["commands"][0]["argv"][0])

    def test_a_request_without_the_token_is_refused(self):
        # Binding to loopback is not authorisation: any page in the same browser can POST here.
        result = self.post_json("/api/gateway", {"dial_address": "192.0.2.10"}, token="")
        self.assertEqual(result.get("error"), "missing or wrong installer token")

    def test_a_form_encoded_body_is_refused(self):
        # text/plain and form encodings can be sent cross-origin without a preflight.
        result = self.post_json("/api/gateway", {"dial_address": "192.0.2.10"},
                                content_type="text/plain")
        self.assertEqual(result.get("error"), "expected application/json")

    def test_the_served_page_carries_the_token_for_its_own_scripts(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            body = response.read().decode()
        self.assertIn(f'window.INSTALLER_TOKEN="{self.token}"', body)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/nope", timeout=10)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
