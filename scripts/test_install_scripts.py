#!/usr/bin/env python3
"""Offline checks for the install scripts (scripts/install-esp32.sh, scripts/install-pi.sh).

Run with the project's interpreter (no pytest here):

    ./.venv/bin/python -B scripts/test_install_scripts.py

An install script is the one thing a user runs on a bare device, so its DRY-RUN path is what
gets pinned here: the exact commands it WOULD run, that a board write is never implied, that the
credentials move as file paths rather than argv, and that the required-argument guards actually
fire. Nothing touches a real board or a real /etc -- every run is --dry-run or a bad-argument
invocation, so the suite is offline and repeatable.

What is pinned:

  1  --help exits 0 and prints usage
  2  --dry-run --build prints the build + bundle commands and touches nothing
  3  --dry-run --flash prints the board write (0x0, the bundle) and nothing more
  4  --flash without --port fails with a clear message (a board write is never implied)
  5  --dry-run --provision passes credential/model/wifi material as FILE PATHS, never inline,
     and names the real provisioning helpers with their real flags
  6  the Pi --dry-run renders the config (VS_BASE / device name / wake word) and names the
     coupled-service group
  7  the Pi script refuses to proceed without --base / --device-name
  8  the Pi --endpoint + --tls-certs renders the registry link (VOICE_BRIDGE_HOST/PORT/TLS_*),
     installs the outbound unit, and puts it in the service group
  9  a half registry link is refused: --tls-certs without --endpoint (credentials, nowhere to
     dial) is a dead device
  10 the issuer (init/enroll/verify) produces a consistent CA + server + device set, and a
     device cert the CA cannot verify is refused at verify
  11 the gateway --dry-run names every step (venv, bridge.env registry values, TLS init,
     user unit, plugin) and refuses a loopback --device-reachable-as
  12 the hermes-voice-config helper applies keys in one pass, is idempotent, and leaves
     an already-correct file byte-for-byte untouched
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ESP32 = ROOT / 'scripts' / 'install-esp32.sh'
PI = ROOT / 'scripts' / 'install-pi.sh'
ISSUE = ROOT / 'scripts' / 'issue-registry-tls.py'
GATEWAY = ROOT / 'scripts' / 'install-gateway.sh'
VOICECFG = ROOT / 'scripts' / 'hermes-voice-config.py'
EXAMPLE = ROOT / 'hermes' / 'config' / 'voice-surface.example.yaml'


def run(script, *args):
    return subprocess.run(['bash', str(script), *args], capture_output=True, text=True)


def load_pdt():
    """Load scripts/provision-device-tls.py as a module, so a test recomputes fingerprints the
    same way the provisioner does instead of re-implementing them."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('pdt', ROOT / 'scripts' / 'provision-device-tls.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class InstallScriptTests(unittest.TestCase):
    def test_scripts_exist_and_are_bash(self):
        for script in (ESP32, PI):
            self.assertTrue(script.exists(), f'missing {script}')
            # A shebang that names bash: these use ${BASH_SOURCE} and pipefail.
            self.assertEqual(script.read_text().splitlines()[0], '#!/usr/bin/env bash')

    def test_help_exits_cleanly(self):
        for script in (ESP32, PI):
            with self.subTest(script=script.name):
                result = run(script, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Usage:', result.stdout)

    def test_esp32_dry_run_build_prints_the_commands_and_touches_nothing(self):
        result = run(ESP32, '--dry-run', '--build')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DRY-RUN', result.stdout)
        self.assertIn('idf.py build', result.stdout)
        self.assertIn('bundle-esp32-flash.py', result.stdout)
        self.assertIn('flash.manifest.txt', result.stdout)
        # Nothing is written to a board without --flash.
        self.assertNotIn('write_flash', result.stdout)

    def test_esp32_dry_run_flash_names_the_board_write(self):
        result = run(ESP32, '--dry-run', '--flash', '--port', '/dev/ttyACM0')
        self.assertEqual(result.returncode, 0, result.stderr)
        # The bundle is written straight to 0x0: bootloader + partition table + app.
        self.assertIn('write_flash', result.stdout)
        self.assertIn('0x0', result.stdout)
        self.assertIn('/dev/ttyACM0', result.stdout)
        self.assertIn('flash.bundle.bin', result.stdout)

    def test_esp32_flash_without_a_port_is_refused(self):
        """A board write must be explicit: --flash with no --port fails, it never guesses a port."""
        result = run(ESP32, '--flash')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--port', result.stderr)

    def test_esp32_provision_moves_credentials_as_paths_not_argv(self):
        result = run(ESP32, '--dry-run', '--provision', '--port', '/dev/ttyACM0',
                     '--tls-certs', '/srv/bridge/state/registry-tls', '--device-id', 'esp32-test',
                     '--endpoint', 'tls://192.0.2.10:10702',
                     '--wifi-ssid', 'TestNet', '--wifi-psk-file', '/tmp/psk.txt',
                     '--model', '/tmp/hey_hermes.tflite', '--model-name', 'hey_hermes')
        self.assertEqual(result.returncode, 0, result.stderr)
        # The provisioning helpers take the FILE PATHS; no key material is inline in argv.
        self.assertIn('provision-device-tls.py', result.stdout)
        self.assertIn('--certs /srv/bridge/state/registry-tls', result.stdout)
        self.assertIn('--device esp32-test', result.stdout)
        self.assertIn('--host 192.0.2.10', result.stdout)
        self.assertIn('--port 10702', result.stdout)
        self.assertIn('--registry /srv/bridge/state/device-credentials.json', result.stdout)
        self.assertIn('--port-device /dev/ttyACM0', result.stdout)
        self.assertIn('--write', result.stdout)
        self.assertIn('provision-device-model.py', result.stdout)
        self.assertIn('/tmp/hey_hermes.tflite', result.stdout)
        # The endpoint and the Wi-Fi profile are staged over the console; the passphrase never
        # appears as an argument, only the file that holds it.
        self.assertIn('set-endpoint tls://192.0.2.10:10702', result.stdout)
        self.assertIn('save-profile TestNet /tmp/psk.txt', result.stdout)
        self.assertNotIn('--tls-manifest', result.stdout)
        self.assertNotIn('set-wifi-ssid', result.stdout)

    def test_esp32_provision_without_the_full_set_is_refused(self):
        """Provisioning is a set, not a menu: a board missing any piece is a dead board, so an
        incomplete provision must be refused before it starts, not half-applied."""
        full = {
            '--tls-certs': '/srv/bridge/state/registry-tls',
            '--device-id': 'esp32-test',
            '--endpoint': 'tls://192.0.2.10:10702',
            '--wifi-ssid': 'TestNet',
            '--wifi-psk-file': '/tmp/psk.txt',
        }
        for flag, value in full.items():
            args = ['--dry-run', '--provision', '--port', '/dev/ttyACM0']
            for f, v in full.items():
                if f != flag:
                    args += [f, v]
            result = run(ESP32, *args)
            self.assertNotEqual(result.returncode, 0, f'should have refused without {flag}')
            self.assertIn(flag, result.stderr)

    def test_pi_dry_run_renders_config_and_names_the_coupled_group(self):
        result = run(PI, '--dry-run', '--base', '/opt/wyoming-satellite',
                     '--device-name', 'kitchen', '--wake-word', 'hey_hermes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DRY-RUN', result.stdout)
        self.assertIn('/opt/wyoming-satellite', result.stdout)
        self.assertIn('kitchen', result.stdout)
        self.assertIn('hey_hermes', result.stdout)
        # The four audio units are a coupled group; the script must say so (they Require= each
        # other in a cycle, so restarting one alone leaves it dead).
        self.assertIn('coupled', result.stdout.lower())

    def test_pi_requires_base_and_device_name(self):
        missing_base = run(PI, '--dry-run', '--device-name', 'kitchen')
        self.assertNotEqual(missing_base.returncode, 0)
        self.assertIn('--base', missing_base.stderr)
        missing_name = run(PI, '--dry-run', '--base', '/opt/wyoming-satellite')
        self.assertNotEqual(missing_name.returncode, 0)
        self.assertIn('--device-name', missing_name.stderr)

    def test_pi_registry_link_renders_the_outbound_env(self):
        """--endpoint + --tls-certs wire the registry link that outbound-satellite reads: the
        endpoint's host:port become VOICE_BRIDGE_HOST/PORT, the enrolled cert files become
        VOICE_BRIDGE_TLS_*, and the outbound unit is installed and joined into the service group."""
        result = run(PI, '--dry-run', '--base', '/opt/wyoming-satellite', '--device-name', 'kitchen',
                     '--endpoint', 'tls://192.0.2.10:10702', '--tls-certs', '/srv/bridge/state/registry-tls')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('VOICE_BRIDGE_HOST=192.0.2.10', result.stdout)
        self.assertIn('VOICE_BRIDGE_PORT=10702', result.stdout)
        self.assertIn('VOICE_BRIDGE_TLS_CERT=/srv/bridge/state/registry-tls/kitchen.crt', result.stdout)
        self.assertIn('VOICE_BRIDGE_TLS_KEY=/srv/bridge/state/registry-tls/kitchen.key', result.stdout)
        self.assertIn('outbound-satellite.service', result.stdout)
        # A registry-linked device joins the service group with its link unit.
        self.assertRegex(result.stdout, r'enable --now .*outbound-satellite\.service')

    def test_pi_endpoint_without_certs_is_allowed_as_a_local_link(self):
        """An endpoint with no TLS is a trusted-LAN link, not an error: the env gets host/port
        and the link unit is installed, just with no VOICE_BRIDGE_TLS_* lines."""
        result = run(PI, '--dry-run', '--base', '/opt/wyoming-satellite', '--device-name', 'kitchen',
                     '--endpoint', 'tcp://192.0.2.10:10701')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('VOICE_BRIDGE_HOST=192.0.2.10', result.stdout)
        self.assertNotIn('VOICE_BRIDGE_TLS_CERT', result.stdout)
        self.assertIn('outbound-satellite.service', result.stdout)

    def test_pi_half_registry_link_is_refused(self):
        """Credentials with nowhere to dial is a dead device, so --tls-certs without --endpoint
        must be refused before anything is installed."""
        result = run(PI, '--dry-run', '--base', '/opt/wyoming-satellite', '--device-name', 'kitchen',
                     '--tls-certs', '/srv/bridge/state/registry-tls')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--endpoint', result.stderr)


class IssuerTests(unittest.TestCase):
    """scripts/issue-registry-tls.py: the host-side half of device enrollment. It is run against
    a throwaway state dir so a real registry is never touched, and it needs openssl on the host
    (a host tool, not a project dependency)."""

    @classmethod
    def setUpClass(cls):
        cls.have_openssl = shutil.which('openssl') is not None
        if not cls.have_openssl:
            raise unittest.SkipTest('openssl not available on this host')
        cls.state = Path(tempfile.mkdtemp(prefix='issue-tls-'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.state, ignore_errors=True)

    def run_issue(self, *args):
        return subprocess.run(['python3', str(ISSUE), '--state', str(self.state), *args],
                              capture_output=True, text=True)

    def test_01_init_creates_a_consistent_ca_and_server(self):
        self.assertTrue(self.have_openssl)
        init = self.run_issue('init', '--san', '192.0.2.10,voice.local')
        self.assertEqual(init.returncode, 0, init.stderr)
        tls = self.state / 'registry-tls'
        for name in ('ca.crt', 'ca.key', 'client-ca.crt', 'server.crt', 'server.key'):
            self.assertTrue((tls / name).is_file(), f'missing {name}')
        # Re-running init reuses the CA (it never regenerates a CA that has enrolled devices).
        again = self.run_issue('init')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn('reusing', again.stdout)

    def test_02_enroll_registers_a_fingerprint_matching_the_device(self):
        self.run_issue('init')
        enroll = self.run_issue('enroll', '--new', 'esp32-test',
                                '--endpoint-host', '192.0.2.10', '--endpoint-port', '10702')
        self.assertEqual(enroll.returncode, 0, enroll.stderr)
        tls = self.state / 'registry-tls'
        self.assertTrue((tls / 'esp32-test.crt').is_file())
        self.assertTrue((tls / 'esp32-test.key').is_file())
        # The credential on file maps the device cert's real fingerprint to the device id, which
        # is exactly what the bridge's load_credential_lookup builds. The fingerprint is
        # recomputed here the same way the provisioner does (sha256 of the cert's DER).
        import hashlib
        mod = load_pdt()
        fp = 'sha256:' + hashlib.sha256(mod.pem_to_der((tls / 'esp32-test.crt').read_bytes())).hexdigest()
        creds = json.loads((self.state / 'device-credentials.json').read_text())
        self.assertEqual(creds.get(fp), 'esp32-test')
        # The printed provisioning command names the right flags.
        self.assertIn('--tls-certs', enroll.stdout)
        self.assertIn('--device-id esp32-test', enroll.stdout)

    def test_03_verify_accepts_a_consistent_set(self):
        self.run_issue('init')
        self.run_issue('enroll', '--new', 'pi-test')
        self.assertEqual(self.run_issue('verify').returncode, 0)

    def test_04_verify_refuses_a_cert_signed_by_the_wrong_ca(self):
        self.run_issue('init')
        self.run_issue('enroll', '--new', 'evil')
        tls = self.state / 'registry-tls'
        # Sign a rogue cert with a fresh CA and plant it as the enrolled device's cert, then point
        # the credential at its fingerprint: verify must catch that the CA cannot verify it.
        rogue = tls / 'evil-rogue'
        rogue.mkdir(parents=True, exist_ok=True)
        import hashlib
        mod = load_pdt()

        def op(*a):
            r = subprocess.run(list(a), capture_output=True, text=True)
            assert r.returncode == 0, f"{' '.join(a[:4])}... failed: {r.stderr}"

        op('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
           '-keyout', str(rogue / 'key.pem'), '-out', str(rogue / 'ca.crt'),
           '-days', '1', '-subj', '/CN=rogue')
        op('openssl', 'req', '-newkey', 'rsa:2048', '-nodes',
           '-keyout', str(rogue / 'dev.key'), '-out', str(rogue / 'dev.csr'),
           '-subj', '/CN=evil')
        op('openssl', 'x509', '-req', '-in', str(rogue / 'dev.csr'),
           '-CA', str(rogue / 'ca.crt'), '-CAkey', str(rogue / 'key.pem'),
           '-CAcreateserial', '-out', str(rogue / 'dev.crt'), '-days', '1')
        (tls / 'evil.crt').write_bytes((rogue / 'dev.crt').read_bytes())
        (tls / 'evil.key').write_bytes((rogue / 'dev.key').read_bytes())
        fp = 'sha256:' + hashlib.sha256(mod.pem_to_der((rogue / 'dev.crt').read_bytes())).hexdigest()
        cfile = self.state / 'device-credentials.json'
        creds = json.loads(cfile.read_text())
        creds[fp] = 'evil'
        cfile.write_text(json.dumps(creds))
        result = self.run_issue('verify')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('not verifiable', result.stdout)


class GatewayInstallerTests(unittest.TestCase):
    """scripts/install-gateway.sh: the host-side installer. It is run --dry-run against a
    throwaway clone skeleton, so nothing is built, written, or started."""

    def make_base(self, tmp: Path) -> Path:
        base = tmp / 'clone'
        (base / 'bridge').mkdir(parents=True)
        shutil.copy(ROOT / 'bridge.env.example', base / 'bridge.env.example')
        shutil.copy(ROOT / 'requirements.txt', base / 'requirements.txt')
        return base

    def gateway(self, base: Path, *extra) -> subprocess.CompletedProcess:
        return run(GATEWAY, '--base', str(base), *extra)

    def test_dry_run_names_every_step(self):
        with tempfile.TemporaryDirectory(prefix='gw-') as tmp:
            base = self.make_base(Path(tmp))
            result = self.gateway(base, '--device-reachable-as', '192.0.2.10',
                                  '--registry-port', '10701',
                                  '--satellite-host', '192.0.2.20:10700', '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            out = result.stdout
            # The five steps, in order.
            self.assertIn('1. bridge virtualenv', out)
            self.assertIn('2. bridge.env', out)
            self.assertIn('3. registry TLS material', out)
            self.assertIn('4. systemd user unit', out)
            self.assertIn('5. Hermes side', out)
            # The registry link values, derived from the flags and the bridge's config defaults.
            self.assertIn('VOICE_REGISTRY_ENABLED=1', out)
            self.assertIn('VOICE_REGISTRY_PORT=10701', out)
            self.assertIn('VOICE_REGISTRY_TLS_PORT=10702', out)   # REGISTRY_PORT+1
            self.assertIn('VOICE_SATELLITE_HOST=192.0.2.20', out)
            self.assertIn('VOICE_SATELLITE_PORT=10700', out)
            # The TLS material comes from the issuer, with the device-reachable address as SAN.
            self.assertIn('issue-registry-tls.py', out)
            self.assertIn('--san 192.0.2.10', out)
            # The registry listener binds 0.0.0.0 by default (the dial address is the cert SAN);
            # the commented template line is not treated as a value.
            self.assertIn('VOICE_REGISTRY_HOST=0.0.0.0', out)
            # The credentials path is derived by the bridge from its own repo root, not a key the
            # installer writes, so it must not appear as a managed VOICE_REGISTRY_* line.
            self.assertNotIn('VOICE_REGISTRY_CREDENTIALS', out)
            # The user unit is rendered from its template with the base path; the plugin is a local
            # tree copied in (hermes plugins install cannot take a path), then enabled by name.
            self.assertIn('hermes-voice-bridge.service.in', out)
            self.assertIn('enable --now', out)
            self.assertIn('.hermes/plugins/voice_satellite', out)
            self.assertIn('hermes plugins enable voice_satellite', out)
            # The printed next step names the device commands with the real flags.
            self.assertIn('--tls-certs', out)
            self.assertIn('tls://192.0.2.10:10702', out)

    def test_dry_run_refuses_a_loopback_reachable_address(self):
        """A device cannot route to 127.0.0.1 on this host: it is an address for the host's own
        use, so accepting it would produce a certificate no device can ever pin against."""
        for bad in ('127.0.0.1', '127.0.0.1:10702', 'localhost', '0.0.0.0'):
            with tempfile.TemporaryDirectory(prefix='gw-') as tmp:
                base = self.make_base(Path(tmp))
                result = self.gateway(base, '--device-reachable-as', bad, '--dry-run')
                self.assertNotEqual(result.returncode, 0, f'should have refused {bad}')
                self.assertIn('--device-reachable-as', result.stderr)


class VoiceConfigHelperTests(unittest.TestCase):
    """scripts/hermes-voice-config.py: the one-pass Hermes config edit the gateway installer
    drives. It needs PyYAML, which lives in the bridge venv, so it is skipped on a host whose
    system python lacks it."""

    def setUp(self):
        self.py = ROOT / '.venv' / 'bin' / 'python'
        if not self.py.is_file():
            raise unittest.SkipTest('bridge venv not present')
        self.cfg = Path(tempfile.mktemp(prefix='voice-cfg-'))
        self.cfg.write_text('model: test-model\nother: kept\n')

    def tearDown(self):
        self.cfg.unlink(missing_ok=True)

    def run_helper(self, *pairs, example=None):
        example = str(example) if example else str(EXAMPLE)
        return subprocess.run([str(self.py), str(VOICECFG), '--config', str(self.cfg),
                               '--example', example, *pairs],
                              capture_output=True, text=True)

    def yaml_dump(self, path: Path) -> str:
        """Parse a file with the venv python (the test process may lack PyYAML)."""
        code = (
            "import sys, yaml\n"
            "d = yaml.safe_load(open(sys.argv[1]))\n"
            "print(repr(d))\n"
        )
        r = subprocess.run([str(self.py), '-c', code, str(path)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def test_applies_keys_without_touching_unmanaged_ones(self):
        result = self.run_helper('gateway.api_server.host=127.0.0.1',
                                 'plugins.enabled=["voice_satellite"]')
        self.assertEqual(result.returncode, 0, result.stderr)
        import ast
        data = ast.literal_eval(self.yaml_dump(self.cfg))
        self.assertEqual(data['model'], 'test-model')       # unmanaged key kept
        self.assertEqual(data['other'], 'kept')
        self.assertEqual(data['gateway']['api_server']['host'], '127.0.0.1')
        self.assertIn('voice_satellite', data['plugins']['enabled'])

    def test_is_idempotent_and_leaves_an_unchanged_file_untouched(self):
        first = self.run_helper('gateway.api_server.host=127.0.0.1')
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.cfg.read_bytes()
        second = self.run_helper('gateway.api_server.host=127.0.0.1')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('no change', second.stdout)
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_extends_a_list_without_dropping_existing_entries(self):
        self.cfg.write_text('plugins:\n  enabled:\n    - something_else\n')
        result = self.run_helper('plugins.enabled=["something_else","voice_satellite"]')
        self.assertEqual(result.returncode, 0, result.stderr)
        import ast
        data = ast.literal_eval(self.yaml_dump(self.cfg))
        self.assertEqual(data['plugins']['enabled'], ['something_else', 'voice_satellite'])

    def test_refuses_a_key_not_defined_in_the_example(self):
        """A typo'd dotted key would otherwise be silently created as a key Hermes ignores, so
        the helper must reject any key absent from the example rather than write it."""
        result = self.run_helper('gateway.api_server.not_a_real_key=1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not in the example', result.stderr)
        # The config is left untouched (refusal happens before any write).
        self.assertIn('test-model', self.cfg.read_text())


if __name__ == '__main__':
    unittest.main()