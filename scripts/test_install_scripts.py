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
  2  --dry-run --build prints the opt-in build + bundle commands and touches nothing
  3  --dry-run --flash prints the board write (0x0, the PREBUILT bundle) and does NOT build
  3b --flash's integrity gate is --verify with no build tree, --check with one
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
  13 the GUIDED flow (no flags / --guided) piped through with --dry-run runs exactly the
     commands the equivalent flag invocation runs, per installer
  14 a zero-argument run with a non-TTY stdin refuses with a "pass the flags" message and
     writes nothing (automation must pass flags, not guess at piped answers)
  15 answering no at the guided confirmation aborts with nothing written, and a bad answer
     is re-prompted (the guided validators mirror the flag rules) rather than accepted
  16 guided mode FORWARDS every flag the user typed, including the ones it does not prompt
     for (--bundle-dir/--registry/--model/--download-model/--model-name, --hermes-bin,
     --no-deps/--no-units/--no-config/--no-services): the run line the guided flow prints
     must contain the flag and its value, and the run must actually use it
  17 a value-taking flag as the LAST argv token refuses with a clear "needs a value" message
     in all three parsers -- never a silent exit 1 with no output
  18 install-pi guided with --tls-certs typed and an empty endpoint never silently diverges
     from the flag path: the flag survives (and the re-executed run refuses with the flag
     path's own --tls-certs needs --endpoint message) or the flow refuses with that same
     message -- never a quiet local-test install
  19 the whole-class flag-surface guard: every non-control flag of every parser (enumerated
     from the parse arms, the same list `grep -oE '^\\s+--[a-z-]+\\)' scripts/install-*.sh`
     prints), typed into guided mode, survives into the announced run line
  20 a typed five-word setup code enrols the board with the gateway, and when the gateway is
     unreachable the guided flow falls back to the manual certificate-folder prompt (the
     pre-plug-and-play path) -- nothing written, and the code never echoed
  21 the setup-code path's assembled run (--tls-certs, with --registry beside the fetched
     certificates) is exactly the equivalent flag invocation, even in --dry-run (which fetches
     nothing and never contacts the gateway)
"""
import json
import os
import re
import shutil
import subprocess
import sys
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
# Flow-control flags: they steer the installer itself and take no typed value to preserve.
CONTROL_FLAGS = {'--guided', '--dry-run'}


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

    def test_help_prints_the_whole_header(self):
        """--help must not truncate the header: the last header comment line has to appear in the
        output. usage() reads a fixed line range, so a stale range silently cuts the final line
        (that happened to install-esp32.sh) and no other test would notice."""
        for script in (ESP32, PI, GATEWAY):
            with self.subTest(script=script.name):
                lines = script.read_text().splitlines()
                cut = next((i for i, l in enumerate(lines) if l.startswith('set -euo')), len(lines))
                last = next((l for l in reversed(lines[:cut]) if l.startswith('#')), '')
                self.assertTrue(last, f'{script.name}: no header comments found')
                needle = last.lstrip('#').strip()
                result = run(script, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(needle, result.stdout,
                              f'{script.name}: --help drops the last header line {needle!r}')

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

    def test_esp32_flash_does_not_build(self):
        """--flash writes the PREBUILT bundle and must not invoke idf.py: a user with no ESP-IDF
        flashes the published image. Only --build compiles from source."""
        result = run(ESP32, '--dry-run', '--flash', '--port', '/dev/ttyACM0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('idf.py', result.stdout, '--flash must not build; --build is opt-in')
        self.assertIn('bundle-esp32-flash.py', result.stdout)
        self.assertIn('write_flash', result.stdout)

    def test_esp32_flash_gate_falls_to_verify_without_a_build_tree(self):
        """A fresh checkout has no firmware/esp32/build, so the flash gate must use --verify (the
        manifest's bundle hash) rather than --check (which re-hashes the source build)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            (repo / 'scripts').mkdir(parents=True)
            (repo / 'firmware' / 'esp32').mkdir(parents=True)   # deliberately no build/
            (repo / 'scripts' / 'lib').mkdir(parents=True)
            shutil.copy(ESP32, repo / 'scripts' / 'install-esp32.sh')
            shutil.copy(ROOT / 'scripts' / 'lib' / 'guided.sh',
                        repo / 'scripts' / 'lib' / 'guided.sh')
            result = run(repo / 'scripts' / 'install-esp32.sh',
                         '--dry-run', '--flash', '--port', '/dev/ttyACM0')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--verify', result.stdout)
            self.assertIn('prebuilt bundle', result.stdout)

    def test_esp32_uses_the_repo_venv_interpreter_when_present(self):
        """The provisioning helpers and esptool must run under the repository's own venv when it
        exists, because that is where esptool and pyserial live; a bare python3 may have neither
        (a real machine did not, and the flash would have failed after the point of no return)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            (repo / 'scripts').mkdir(parents=True)
            (repo / 'firmware' / 'esp32').mkdir(parents=True)
            (repo / '.venv' / 'bin').mkdir(parents=True)
            venv_python = repo / '.venv' / 'bin' / 'python'
            venv_python.write_text('#!/bin/sh\nexit 0\n')
            venv_python.chmod(0o755)
            (repo / 'scripts' / 'lib').mkdir(parents=True)
            shutil.copy(ESP32, repo / 'scripts' / 'install-esp32.sh')
            shutil.copy(ROOT / 'scripts' / 'lib' / 'guided.sh',
                        repo / 'scripts' / 'lib' / 'guided.sh')
            result = run(repo / 'scripts' / 'install-esp32.sh',
                         '--dry-run', '--flash', '--port', '/dev/ttyACM0')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(venv_python), result.stdout,
                          'the repo venv interpreter was not used for the flash commands')
            self.assertIn('-m esptool', result.stdout)

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


class GuidedModeTests(unittest.TestCase):
    """The GUIDED (no-flags) setup flow of the three installers, driven over a pipe with
    --guided --dry-run so nothing is ever written. What matters: a guided run must run exactly
    the commands the equivalent flag invocation runs (it re-executes the flag path with the
    assembled answers), a non-TTY zero-argument run must refuse instead of guessing, and the
    confirmation must be an explicit yes -- a no aborts with nothing written."""

    @staticmethod
    def run_guided(script, answers, *extra):
        return subprocess.run(['bash', str(script), '--guided', '--dry-run', *extra],
                              input='\n'.join(answers) + '\n',
                              capture_output=True, text=True)

    @staticmethod
    def dry_run_lines(result):
        return [line for line in result.stdout.splitlines()
                if line.startswith('DRY-RUN:') or line.startswith('+ ')]

    @staticmethod
    def run_line(result):
        """The exact command line the guided flow announces it will run (the line under the
        "I will now run:" banner), or '' when it never printed one. This is the contract for
        pass-through: every flag the user typed must appear here."""
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == 'I will now run:':
                for follow in lines[i + 1:]:
                    if follow.strip():
                        return follow.strip()
        return ''

    @staticmethod
    def lines_with(result, needle):
        return [line for line in result.stdout.splitlines() if needle in line]

    def assert_same_command_set(self, guided, flags):
        """The guided run and the flag run must produce the identical command set."""
        self.assertEqual(guided.returncode, 0, guided.stderr)
        self.assertEqual(flags.returncode, 0, flags.stderr)
        guided_cmds = self.dry_run_lines(guided)
        flag_cmds = self.dry_run_lines(flags)
        self.assertTrue(flag_cmds, 'flag run produced no commands at all')
        self.assertEqual(guided_cmds, flag_cmds,
                         'guided run and equivalent flag run disagree on the commands')

    def test_esp32_guided_matches_the_flag_invocation(self):
        psk = Path(tempfile.mkdtemp(prefix='enrol-psk-')) / 'psk.txt'
        psk.write_text('a-test-passphrase\n')
        answers = [
            'n',                                     # write the firmware to the satellite?
            'y',                                     # set the satellite up?
            '/dev/ttyACM0',                          # satellite serial port
            '/srv/bridge/state/registry-tls',        # certificates folder
            'esp32-test',                            # name this satellite is enrolled under
            'tls://192.0.2.10:10702',                # address it reaches the gateway on
            'TestNet',                               # Wi-Fi network
            str(psk),                                # a file that holds the passphrase
            'n',                                     # compile the firmware yourself? (advanced)
            'yes',                                   # confirm
        ]
        guided = self.run_guided(ESP32, answers)
        flags = run(ESP32, '--dry-run', '--provision', '--port', '/dev/ttyACM0',
                    '--tls-certs', '/srv/bridge/state/registry-tls', '--device-id', 'esp32-test',
                    '--endpoint', 'tls://192.0.2.10:10702',
                    '--wifi-ssid', 'TestNet', '--wifi-psk-file', str(psk))
        self.assert_same_command_set(guided, flags)
        # The guided answers really provision: the same helpers as the flag path ran.
        self.assertIn('provision-device-tls.py', flags.stdout)
        self.assertIn(f'save-profile TestNet {psk}', flags.stdout)

    def test_a_bare_ip_is_completed_with_the_scheme_and_the_port(self):
        """Typing just the gateway's IP address is what people actually do, and they should not
        have to know the scheme or the port. Both are filled in, and the resolved address is said
        out loud so nothing happens silently. A port that IS given is respected, so a gateway on a
        non-standard port still works."""
        base = ['n', 'y', '/dev/ttyACM0', '/srv/bridge/state/registry-tls', 'esp32-test']
        tail = ['TestNet', '/tmp/psk.txt', 'n', 'no']

        result = self.run_guided(ESP32, base + ['192.0.2.10'] + tail)
        run_line = self.run_line(result)
        self.assertIn('--endpoint tls://192.0.2.10:10702', run_line,
                      'a bare IP must be completed to scheme://host:port')
        self.assertIn('tls://192.0.2.10:10702', result.stdout,
                      'the resolved address must be shown, not applied silently')
        # No default may be offered for the gateway: this machine's own LAN IP answers "what is
        # my address?", not "where is the gateway?", and is wrong whenever the gateway is another
        # machine. A wrong default is worse than no default.
        self.assertNotRegex(result.stdout,
                            r'Your gateway \(its IP address or host name\) \[',
                            'the gateway prompt must not suggest this machine\'s own IP')

        # a host with an explicit port keeps that port
        result = self.run_guided(ESP32, base + ['192.0.2.20:9000'] + tail)
        self.assertIn('--endpoint tls://192.0.2.20:9000', self.run_line(result),
                      'a port the person gave must never be overwritten by the default')

        # and the flag path agrees with the prompt path
        flags = run(ESP32, '--dry-run', '--provision', '--port', '/dev/ttyACM0',
                    '--tls-certs', '/srv/bridge/state/registry-tls', '--device-id', 'esp32-test',
                    '--endpoint', '192.0.2.10', '--wifi-ssid', 'TestNet',
                    '--wifi-psk-file', '/tmp/psk.txt')
        self.assertIn('set-endpoint tls://192.0.2.10:10702', flags.stdout,
                      'the --endpoint flag must accept a bare IP too')

    def test_usb_and_bluetooth_are_named_but_refused_until_they_are_carried(self):
        """The gateway link is a TRANSPORT: usb://localhost (cabled into the gateway computer),
        bt:// (radio), tls:// or tcp:// (network). The vocabulary and the normaliser understand all
        three so the design is not closed off, but only the network link is carried today -- so
        asking for usb must be EXPLAINED, not accepted, or the person is handed a satellite that
        cannot connect. The refusal must also offer the way that does work."""
        base = ['n', 'y', '/dev/ttyACM0', '/srv/bridge/state/registry-tls', 'esp32-test']
        tail = ['TestNet', '/tmp/psk.txt', 'n', 'no']

        # 'usb' is refused with a reason; the re-prompt then takes a real address
        result = self.run_guided(ESP32, base + ['usb', '192.0.2.10'] + tail)
        out = result.stdout
        self.assertIn('firmware cannot carry it yet', out, 'usb must be explained, not accepted')
        self.assertIn('IP address or host name', out,
                      'the refusal must name the way that does work')
        self.assertIn('--endpoint tls://192.0.2.10:10702', self.run_line(result),
                      'the person must be able to recover at the re-prompt')

        # the normaliser already speaks all three, so landing a transport needs no rework
        got = subprocess.run(['bash', '-c',
                              'source scripts/lib/guided.sh; '
                              'for v in usb bluetooth 192.0.2.10; do '
                              'printf "%s\\n" "$(guided_normalise_endpoint "$v")"; done'],
                             capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(got.stdout.split(), ['usb://localhost', 'bt://', 'tls://192.0.2.10:10702'],
                         got.stdout)

    def test_a_typed_passphrase_is_held_in_a_private_file_and_never_printed(self):
        """You can just type the Wi-Fi passphrase. It must never appear in the output -- not in
        the summary, not in the run line, not in a log -- because argv is visible in `ps` and in
        shell history. Instead the run carries a private file path, and the value is gone from
        every surface a person or a log can see."""
        secret = 'an-extremely-secret-passphrase'
        answers = [
            'n', 'y', '/dev/ttyACM0', '/srv/bridge/state/registry-tls', 'esp32-test',
            'tls://192.0.2.10:10702', 'TestNet', secret,
            'n', 'no',
        ]
        result = self.run_guided(ESP32, answers)
        # Answering 'no' at the confirmation aborts (exit 1) and writes nothing: that is the
        # contract. The secrecy claim below must hold on that path too.
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn('aborted', result.stderr)
        combined = result.stdout + result.stderr
        self.assertNotIn(secret, combined, 'the typed passphrase must never be printed')
        run_line = self.run_line(result)
        self.assertIn('--wifi-psk-file', run_line)
        self.assertNotIn(secret, run_line)
        self.assertIn('hermes-wifi-psk', run_line,
                      'the passphrase travels as a private file, not as a value')

    def test_esp32_guided_enrol_falls_back_to_the_folder_prompt_when_the_gateway_is_unreachable(self):
        """Plug-and-play: a five-word setup code answered at the setup question enrols the board
        with the gateway (scripts/enroll_device.py). When the gateway cannot be reached, the guided
        flow falls back to the manual certificate-folder prompt -- the pre-plug-and-play path --
        so nothing regresses. Driven from a throwaway repo skeleton with a scratch HOME and an
        unreachable enrol address, and stopped at the confirmation: nothing is written and the
        setup code appears nowhere in the output."""
        token = 'zebra-mango-tulip-quartz-ivory'   # a test sentinel, not a real setup code
        with tempfile.TemporaryDirectory(prefix='enroll-guided-') as tmp:
            repo = self.make_enrol_skeleton(Path(tmp))
            # flash / set-up / detail prompts (setup code, name, address, network, passphrase,
            # then the FALLBACK folder) / compile / confirm.
            answers = [
                'n',                                  # write the firmware to the board?
                'y',                                  # set the board up?
                '/dev/ttyACM0',                       # board serial port
                token,                                # setup code (five words)
                'esp32-enroll-fb',                    # name this board is enrolled under
                'tls://192.0.2.10:10702',             # address it reaches the assistant on
                'TestNet',                            # Wi-Fi network
                '/tmp/psk.txt',                       # passphrase file
                '/srv/bridge/state/registry-tls',     # FALLBACK: the certificates folder
                'n',                                  # compile the firmware yourself?
                'no',                                 # confirm -> abort
            ]
            result = subprocess.run(
                ['bash', str(repo / 'scripts' / 'install-esp32.sh'), '--guided'],
                input='\n'.join(answers) + '\n', capture_output=True, text=True,
                env=self.enrol_env(repo))
            combined = result.stdout + result.stderr
            # The exchange was attempted, its failure was explained, and the old folder prompt
            # appeared as the fallback.
            self.assertIn('fetching this satellite', result.stdout)
            self.assertIn('could not reach the gateway', combined)
            self.assertIn("Folder holding this satellite's certificates", result.stdout)
            # The fallback answer becomes the run's --tls-certs (no --registry: this is the
            # manual path's own layout), and a refused confirmation runs nothing.
            self.assertIn('--tls-certs /srv/bridge/state/registry-tls', self.run_line(result))
            self.assertNotIn('--registry', self.run_line(result))
            self.assertIn('aborted', result.stderr)
            self.assertEqual(self.dry_run_lines(result), [], 'no command may run after a no')
            # The setup code is never an argument and never echoed.
            self.assertNotIn(token, combined)

    def test_esp32_guided_dry_run_enrol_matches_the_flag_invocation(self):
        """The setup-code path in --dry-run: nothing is fetched (a dry run never contacts the
        gateway) but the assembled run names the folder the fetch would use and the registry file
        beside it -- and that run is exactly the equivalent flag invocation."""
        token = 'zebra-mango-tulip-quartz-ivory'
        with tempfile.TemporaryDirectory(prefix='enroll-dry-') as tmp:
            repo = self.make_enrol_skeleton(Path(tmp))
            enroll_dir = Path(tmp) / 'home' / '.config' / 'hermes-voice-satellite' / 'certs'
            # flash / set-up / detail prompts / compile / confirm.
            psk = Path(tmp) / 'psk.txt'
            psk.write_text('a-test-passphrase\n')
            answers = [
                'n',                                  # write the firmware to the satellite?
                'y',                                  # set the satellite up?
                '/dev/ttyACM0',                       # satellite serial port
                token,                                # setup code (five words)
                'esp32-enroll-dr',                    # name this satellite is enrolled under
                'tls://192.0.2.10:10702',             # address it reaches the gateway on
                'TestNet',                            # Wi-Fi network
                str(psk),                             # a file that holds the passphrase
                'n',                                  # compile the firmware yourself?
                'yes',                                # confirm
            ]
            guided = subprocess.run(
                ['bash', str(repo / 'scripts' / 'install-esp32.sh'), '--guided', '--dry-run'],
                input='\n'.join(answers) + '\n', capture_output=True, text=True,
                env=self.enrol_env(repo))
            flags = run(repo / 'scripts' / 'install-esp32.sh', '--dry-run', '--provision',
                        '--port', '/dev/ttyACM0', '--tls-certs', str(enroll_dir),
                        '--device-id', 'esp32-enroll-dr',
                        '--endpoint', 'tls://192.0.2.10:10702',
                        '--wifi-ssid', 'TestNet', '--wifi-psk-file', str(psk),
                        '--registry', str(enroll_dir / 'device-credentials.json'))
            self.assert_same_command_set(guided, flags)
            self.assertIn('nothing fetched', guided.stdout)
            self.assertIn('--tls-certs %s' % enroll_dir, self.run_line(guided))
            self.assertIn('--registry %s' % (enroll_dir / 'device-credentials.json'),
                          self.run_line(guided))
            self.assertNotIn(token, guided.stdout + guided.stderr)

    def test_a_reused_name_offers_to_reissue_instead_of_stopping(self):
        """A previous attempt already enrolled this name, so the gateway answers 409 and the
        client exits 3. The guided flow must offer to issue a new certificate and retry with
        --force -- not drop into the certificate-folder prompt with no way forward."""
        token = 'zebra-mango-tulip-quartz-ivory'
        with tempfile.TemporaryDirectory(prefix='enrol-reissue-') as tmp:
            repo = self.make_enrol_skeleton(Path(tmp))
            # Stub the enrolment client: refuse with exit 3 unless --force is given, and record
            # every invocation so the retry can be checked.
            calls = Path(tmp) / 'calls.txt'
            stub = repo / 'scripts' / 'enroll_device.py'
            stub.write_text(
                'import sys\n'
                f'open({str(calls)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
                'raise SystemExit(0 if "--force" in sys.argv else 3)\n')
            psk = Path(tmp) / 'psk.txt'
            psk.write_text('a-test-passphrase\n')
            answers = [
                'n', 'y', '/dev/ttyACM0', token, 'esp32-reissue',
                'tls://192.0.2.10:10702', 'TestNet', str(psk),
                'y',                                     # issue a new certificate?
                # NOTE the order: the reissue question is asked inside the enrol step, which
                # comes BEFORE the compile question. Getting this wrong sends 'y' to the wrong
                # prompt and shows up as a bogus --tls-certs value.
                'n',                                     # compile? (advanced)
                'no',                                    # confirm (abort: nothing is written)
            ]
            result = subprocess.run(
                ['bash', str(repo / 'scripts' / 'install-esp32.sh'), '--guided'],
                input='\n'.join(answers) + '\n', capture_output=True, text=True,
                env=self.enrol_env(repo))
            out = result.stdout + result.stderr
            self.assertIn('already has a certificate', out,
                          'the person must be told why the name was refused')
            self.assertIn('Issue a new certificate', out, 'a reissue must be offered')
            ran = calls.read_text().splitlines() if calls.exists() else []
            self.assertEqual(len(ran), 2, f'expected a refusal then a forced retry: {ran}')
            self.assertNotIn('--force', ran[0], 'the first attempt must not force')
            self.assertIn('--force', ran[1], 'the retry must reissue with --force')
            self.assertNotIn('Folder holding', out,
                             'the certificate-folder dead end must not appear')

    @staticmethod
    def make_enrol_skeleton(tmp: Path) -> Path:
        """A throwaway checkout: the scripts, a repo venv whose python is this test's own
        interpreter (so enroll_device.py runs for real), and nothing else that could hold
        certificates or a saved setup code (HOME is scratch too, see enrol_env)."""
        repo = tmp / 'repo'
        shutil.copytree(ROOT / 'scripts', repo / 'scripts')
        (repo / 'firmware' / 'esp32').mkdir(parents=True)
        (repo / '.venv' / 'bin').mkdir(parents=True)
        venv_python = repo / '.venv' / 'bin' / 'python'
        venv_python.write_text('#!/bin/sh\nexec %s "$@"\n' % sys.executable)
        venv_python.chmod(0o755)
        (tmp / 'home').mkdir()
        return repo

    @staticmethod
    def enrol_env(repo: Path):
        """A scratch HOME (no saved setup code, no certificates to find) and an enrol address
        that refuses instantly, so the fallback path is exercised without any real gateway."""
        return {**os.environ, 'HOME': str(repo.parent / 'home'),
                'VOICE_ENROLL_URL': 'http://127.0.0.1:1'}

    def test_pi_guided_matches_the_flag_invocation(self):
        answers = [
            '/opt/wyoming-satellite',                # base
            'kitchen',                               # device name
            'hey_hermes',                            # wake word
            'pi',                                    # user
            'tls://192.0.2.10:10702',                # registry endpoint
            '/srv/bridge/state/registry-tls',        # TLS certificates directory
            '/etc/voice-satellite/satellite.env',    # satellite.env path
            '10700',                                 # satellite port
            'yes',                                   # confirm
        ]
        guided = self.run_guided(PI, answers)
        flags = run(PI, '--dry-run', '--base', '/opt/wyoming-satellite', '--device-name', 'kitchen',
                    '--wake-word', 'hey_hermes', '--user', 'pi', '--satellite-port', '10700',
                    '--env-path', '/etc/voice-satellite/satellite.env',
                    '--endpoint', 'tls://192.0.2.10:10702',
                    '--tls-certs', '/srv/bridge/state/registry-tls')
        self.assert_same_command_set(guided, flags)
        self.assertRegex(flags.stdout, r'enable --now .*outbound-satellite\.service')

    def test_gateway_guided_matches_the_flag_invocation(self):
        with tempfile.TemporaryDirectory(prefix='gw-guided-') as tmp:
            base = Path(tmp) / 'clone'
            (base / 'bridge').mkdir(parents=True)
            shutil.copy(ROOT / 'bridge.env.example', base / 'bridge.env.example')
            shutil.copy(ROOT / 'requirements.txt', base / 'requirements.txt')
            answers = [
                str(base),                           # base (the clone the gateway runs from)
                '192.0.2.10',                        # device-reachable-as
                '10701',                             # registry port
                '10702',                             # TLS port
                '0.0.0.0',                           # registry bind
                '192.0.2.20:10700',                  # satellite host:port (old shape)
                'y',                                 # configure the Hermes side?
                'yes',                               # confirm
            ]
            guided = self.run_guided(GATEWAY, answers)
            flags = run(GATEWAY, '--base', str(base), '--device-reachable-as', '192.0.2.10',
                        '--registry-port', '10701', '--tls-port', '10702',
                        '--registry-bind', '0.0.0.0', '--satellite-host', '192.0.2.20:10700',
                        '--dry-run')
            self.assert_same_command_set(guided, flags)
            self.assertIn('--san 192.0.2.10', flags.stdout)

    def test_zero_args_with_a_non_tty_stdin_is_refused(self):
        """The zero-argument guided flow needs a terminal. A scripted run must pass flags; with
        stdin closed the installer must refuse, name the flags, and write nothing."""
        for script in (ESP32, PI, GATEWAY):
            with self.subTest(script=script.name):
                result = subprocess.run(['bash', str(script)], capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('flags', result.stderr)
                self.assertEqual(result.stdout, '', 'refusal must not run any step')
                self.assertNotIn('DRY-RUN:', result.stdout)

    def test_answering_no_at_the_confirmation_aborts_with_nothing_written(self):
        """The confirmation is a real gate: an explicit no must abort before any command runs."""
        cases = (
            (ESP32, ['n', 'y', '/dev/ttyACM0', '/srv/bridge/state/registry-tls',
                     'esp32-test', 'tls://192.0.2.10:10702', 'TestNet', '/tmp/psk.txt',
                     'n', 'no']),
            (PI, ['/opt/wyoming-satellite', 'kitchen', 'hey_hermes', 'pi',
                  'tls://192.0.2.10:10702', '/srv/bridge/state/registry-tls',
                  '/etc/voice-satellite/satellite.env', '10700', 'no']),
        )
        for script, answers in cases:
            with self.subTest(script=script.name):
                result = self.run_guided(script, answers)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('aborted', result.stderr)
                self.assertEqual(self.dry_run_lines(result), [], 'no command may run after a no')
        with tempfile.TemporaryDirectory(prefix='gw-no-') as tmp:
            base = Path(tmp) / 'clone'
            (base / 'bridge').mkdir(parents=True)
            shutil.copy(ROOT / 'bridge.env.example', base / 'bridge.env.example')
            shutil.copy(ROOT / 'requirements.txt', base / 'requirements.txt')
            result = self.run_guided(GATEWAY, [str(base), '192.0.2.10', '10701', '10702',
                                               '0.0.0.0', '192.0.2.20:10700', 'y', 'no'])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('aborted', result.stderr)
            self.assertEqual(self.dry_run_lines(result), [])
            self.assertFalse((base / 'bridge.env').exists(), 'refusal must not write bridge.env')

    def test_a_bad_answer_is_reprompted_with_the_flag_rules_reason(self):
        """Guided validation mirrors the flag rules: an ftp:// endpoint is refused with the same
        reason the flag path would die with, and the flow re-prompts instead of accepting it."""
        answers = ['n', 'y', '/dev/ttyACM0', '/srv/bridge/state/registry-tls', 'esp32-test',
                   'ftp://192.0.2.10:10702',                       # rejected, re-prompt
                   'tls://192.0.2.10:10702',                       # accepted
                   'TestNet', '/tmp/psk.txt', 'n', 'yes']
        result = self.run_guided(ESP32, answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The point is that guided and the flag path refuse with the SAME reason, so assert
        # the phrase both of them use.
        self.assertIn('use tls:// or tcp://', result.stdout)
        self.assertIn('set-endpoint tls://192.0.2.10:10702', result.stdout)

    # ---- pass-through: flags with no prompt of their own -------------------------
    # Guided mode must run EXACTLY what the user asked for. A flag the guided flow does not
    # prompt for is not a flag it may drop: it must be forwarded verbatim to the re-executed
    # run (visible in the printed run line) and actually take effect there.

    # A provision run driven through the guided prompts (defaults seeded by --provision). The
    # compile-yourself question is asked LAST, after the detail prompts, so its answer sits just
    # before the confirmation.
    PROV_ANSWERS = [
        '',                                      # write the firmware? (seeded n by --provision)
        '',                                      # set the board up? (seeded y)
        '/dev/ttyACM0',                          # board serial port
        '/srv/bridge/state/registry-tls',        # certificates folder
        'esp32-test',                            # name this board is enrolled under
        'tls://192.0.2.10:10702',                # address it reaches the assistant on
        'TestNet',                               # Wi-Fi network
        '/tmp/psk.txt',                          # passphrase file
        '',                                      # compile yourself? (advanced; seeded n)
        'yes',                                   # confirm
    ]
    # A flash-only run (defaults seeded by --flash): write, set up, port, compile, confirm.
    FLASH_ANSWERS = ['', '', '', '', 'yes']
    # A Pi run with no registry link (endpoint answered 'none'): base, device name, wake word,
    # user, endpoint, env path, satellite port, confirm.
    PI_SKIP_ANSWERS = ['', '', '', '', 'none', '', '', 'yes']

    def test_esp32_guided_forwards_bundle_dir(self):
        result = self.run_guided(ESP32, self.FLASH_ANSWERS,
                                 '--flash', '--bundle-dir', '/mnt/custom-bundle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--bundle-dir /mnt/custom-bundle', self.run_line(result))
        # And the board write really targets THAT bundle, not the default bundle dir (the
        # esptool command line, not the summary's prose).
        write = [ln for ln in self.lines_with(result, 'write_flash') if 'esptool' in ln]
        self.assertTrue(write, result.stdout)
        self.assertIn('/mnt/custom-bundle/flash.bundle.bin', write[0])
        self.assertNotIn('xvf3800-flash/flash.bundle.bin', write[0])

    def test_esp32_guided_forwards_registry(self):
        result = self.run_guided(ESP32, self.PROV_ANSWERS, '--provision',
                                 '--registry', '/mnt/custom/device-credentials.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--registry /mnt/custom/device-credentials.json', self.run_line(result))
        tls = self.lines_with(result, 'provision-device-tls.py')
        self.assertTrue(tls, result.stdout)
        self.assertIn('--registry /mnt/custom/device-credentials.json', tls[0])

    def test_esp32_guided_forwards_model(self):
        result = self.run_guided(ESP32, self.PROV_ANSWERS, '--provision',
                                 '--model', '/tmp/custom-model.tflite')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--model /tmp/custom-model.tflite', self.run_line(result))
        model = self.lines_with(result, 'provision-device-model.py')
        self.assertTrue(model, result.stdout)
        self.assertIn('--model /tmp/custom-model.tflite', model[0])

    def test_esp32_guided_forwards_download_model(self):
        result = self.run_guided(ESP32, self.PROV_ANSWERS, '--provision',
                                 '--download-model', 'https://models.example.com/community.tflite')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--download-model https://models.example.com/community.tflite',
                      self.run_line(result))
        self.assertIn('fetching wake model: https://models.example.com/community.tflite',
                      result.stdout)

    def test_esp32_guided_forwards_model_name(self):
        result = self.run_guided(ESP32, self.PROV_ANSWERS, '--provision',
                                 '--model-name', 'wakey_wakey')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--model-name wakey_wakey', self.run_line(result))
        model = self.lines_with(result, 'provision-device-model.py')
        self.assertTrue(model, result.stdout)
        self.assertIn('--name wakey_wakey', model[0])

    def test_gateway_guided_forwards_hermes_bin(self):
        with tempfile.TemporaryDirectory(prefix='gw-bin-') as tmp:
            base = Path(tmp) / 'clone'
            (base / 'bridge').mkdir(parents=True)
            shutil.copy(ROOT / 'bridge.env.example', base / 'bridge.env.example')
            shutil.copy(ROOT / 'requirements.txt', base / 'requirements.txt')
            answers = [str(base), '192.0.2.10', '10701', '10702', '0.0.0.0',
                       '192.0.2.20:10700', 'y', 'yes']
            result = self.run_guided(GATEWAY, answers, '--hermes-bin', '/nonexistent-hermes-bin')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--hermes-bin /nonexistent-hermes-bin', self.run_line(result))
            # The Hermes-side step must drive the binary the user named, not the default.
            self.assertIn('/nonexistent-hermes-bin plugins enable voice_satellite', result.stdout)
            self.assertNotIn('DRY-RUN: hermes plugins enable voice_satellite', result.stdout)

    def test_pi_guided_forwards_no_deps(self):
        result = self.run_guided(PI, self.PI_SKIP_ANSWERS, '--no-deps')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--no-deps', self.run_line(result))
        self.assertNotIn('== dependencies ==', result.stdout)
        self.assertNotIn('python3 -m venv', result.stdout)
        self.assertIn('== systemd units ==', result.stdout)   # the other steps still run

    def test_pi_guided_forwards_no_units(self):
        result = self.run_guided(PI, self.PI_SKIP_ANSWERS, '--no-units')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--no-units', self.run_line(result))
        self.assertNotIn('== systemd units ==', result.stdout)
        self.assertIn('== dependencies ==', result.stdout)

    def test_pi_guided_forwards_no_config(self):
        result = self.run_guided(PI, self.PI_SKIP_ANSWERS, '--no-config')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--no-config', self.run_line(result))
        self.assertNotIn('== configuration (', result.stdout)
        self.assertIn('== systemd units ==', result.stdout)

    def test_pi_guided_forwards_no_services(self):
        result = self.run_guided(PI, self.PI_SKIP_ANSWERS, '--no-services')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--no-services', self.run_line(result))
        self.assertNotIn('== services ==', result.stdout)
        self.assertNotIn('systemctl enable --now', result.stdout)
        self.assertIn('== configuration (', result.stdout)

    def test_pi_guided_tls_certs_with_an_empty_endpoint_never_silently_diverges(self):
        """--tls-certs typed while the endpoint prompt resolves empty (answered 'none'): the
        flag path REFUSES (--tls-certs needs --endpoint), so guided must not quietly install a
        local test device instead. Either the flag is forwarded verbatim (and the re-executed
        run refuses with the flag path's own message) or the guided flow refuses with that same
        message -- anything else is guided doing something different from what was asked."""
        flag_path = run(PI, '--base', '/opt/x', '--device-name', 'd',
                        '--tls-certs', '/srv/certs', '--dry-run')
        self.assertNotEqual(flag_path.returncode, 0)
        self.assertIn('--tls-certs needs --endpoint', flag_path.stderr)

        # base, device name, wake word, user, endpoint ('none' -> empty), env path,
        # satellite port, confirm.
        answers = ['', '', '', '', 'none', '', '', 'no']
        result = self.run_guided(PI, answers, '--tls-certs', '/srv/certs')
        combined = result.stdout + result.stderr
        forwarded = '--tls-certs /srv/certs' in self.run_line(result)
        refused = '--tls-certs needs --endpoint' in combined
        self.assertTrue(forwarded or refused,
                        'guided silently dropped --tls-certs; run line %r, output %r'
                        % (self.run_line(result), combined))

        # And a CONFIRMED run must end exactly where the flag path does: the same refusal
        # message and no command run -- never a quiet local-test install.
        result = self.run_guided(PI, answers[:-1] + ['yes'], '--tls-certs', '/srv/certs')
        combined = result.stdout + result.stderr
        self.assertIn('--tls-certs needs --endpoint', combined)
        self.assertEqual(self.dry_run_lines(result), [],
                         'a refused half link may not run any command')


class FlagSurfaceGuardTests(unittest.TestCase):
    """Guards for the argv/guided contract, and the whole-class guard whose absence let the
    "--tls-certs silently dropped" bug ship twice.

    (a) A value-taking flag as the LAST argv token must refuse with a clear "needs a value"
        message in every parser -- never a silent exit 1 with empty output.
    (b) Every non-control flag of every parser, typed into guided mode, must survive into the
        run line the guided flow announces. The parser flag surface is enumerated straight
        from the parse arms -- the same list `grep -oE '^\\s+--[a-z-]+\\)' scripts/install-*.sh`
        prints -- so a flag added later is covered automatically and must be given a typed
        test value here before the suite goes green again."""

    # One typed value per value-taking flag. It seeds the prompt default where the flag is
    # prompted (bare Enter at that prompt accepts it) and must survive VERBATIM into the
    # announced run line. Values are chosen to pass the prompt validators where those exist.
    TYPED = {
        'install-esp32.sh': {
            '--port': '/dev/ttyACM7',
            '--bundle-dir': '/srv/typed-bundle',
            '--tls-certs': '/srv/typed-certs',
            '--device-id': 'typed-dev-1',
            '--device-name': 'Typed Friendly Name',
            '--endpoint': 'tls://192.0.2.10:10702',
            '--registry': '/srv/typed/device-credentials.json',
            '--model': '/srv/typed/model.tflite',
            '--download-model': 'https://models.example.com/typed.tflite',
            '--model-name': 'typed_model',
            '--wifi-ssid': 'TypedNet',
            '--wifi-psk-file': '/srv/typed/psk.txt',
        },
        'install-pi.sh': {
            '--base': '/srv/typed-base',
            '--device-name': 'typed-dev',
            '--wake-word': 'typed_wake',
            '--user': 'typeduser',
            '--satellite-port': '10799',
            '--endpoint': 'tls://192.0.2.10:10702',
            '--tls-certs': '/srv/typed-certs',
            '--env-path': '/etc/typed/satellite.env',
        },
        'install-gateway.sh': {
            '--base': '/srv/typed-gwbase',
            '--device-reachable-as': '192.0.2.55',
            '--registry-port': '11701',
            '--tls-port': '11702',
            '--registry-bind': '0.0.0.0',
            '--satellite-host': '192.0.2.20:10700',
            '--hermes-bin': '/srv/typed-hermes-bin',
        },
    }

    @staticmethod
    def surface(script):
        """Every '--flag)' arm of the script's argv parser, mapped to whether the arm consumes
        a VALUE ($2). This is the parser's full flag surface, derived from the source so new
        flags are covered without editing the test."""
        surface = {}
        lines = Path(script).read_text().splitlines()
        for i, line in enumerate(lines):
            # Allow an empty body on the arm line: a parser arm may put its body (and even its
            # $2 read) on the following line, and the guard must still see that flag. The old
            # pattern required non-empty trailing text, which would silently lose coverage the
            # moment someone reformatted a parser.
            m = re.match(r'\s+(--[a-z0-9_-]+)\)(.*)', line)
            if not m:
                continue
            body = m.group(2)
            lookahead = 0
            while not body.strip() and i + lookahead + 1 < len(lines) and lookahead < 3:
                lookahead += 1
                body += ' ' + lines[i + lookahead]
            # ${2:-}, ${2}, $2 and "$2" all consume a value. A bare-substring test for '$2'
            # MISSES the braced form ${2...}, which is exactly what these parsers use, and a
            # detector that sees no value-taking flag makes the guard tests pass vacuously.
            surface[m.group(1)] = bool(re.search(r'\$\{?2', body))
        return surface

    def drive(self, script, flag):
        """Guided --dry-run with `flag` typed, every prompt answered so the typed value is the
        one accepted (bare Enter at the flag's own prompt), and a refusal at the confirmation:
        the run line printed before that refusal is the pass-through contract, and nothing is
        written or re-executed."""
        typed = self.TYPED[script.name]
        extra = [flag, typed[flag]] if flag in typed else [flag]
        if script.name == 'install-pi.sh':
            # base, device name, wake word, user, endpoint (optional; 'none' = empty -> no TLS
            # prompt follows), env path, satellite port, confirm.
            answers = ['' if flag == '--base' else '/srv/pisat',
                       '' if flag == '--device-name' else 'pi-test',
                       '' if flag == '--wake-word' else 'hey_test',
                       '' if flag == '--user' else 'pitest']
            if flag == '--endpoint':
                answers += ['',                            # accept the typed endpoint
                            '/srv/bridge/state/registry-tls']  # TLS prompt (endpoint non-empty)
            else:
                answers += ['none']                        # empty endpoint -> no TLS prompt
            answers += ['' if flag == '--env-path' else '/etc/voice-satellite/satellite.env',
                        '' if flag == '--satellite-port' else '10700',
                        'no']
        elif script.name == 'install-esp32.sh':
            # flash / provision accept their seeded defaults; the compile-yourself question is
            # asked LAST, after the detail prompts, so its answer sits just before the confirm.
            fp = ['', '']
            if flag == '--build':                 # seeded n/n/y: no detail prompts run
                answers = fp + ['', 'no']
            elif flag == '--provision':           # seeded n/y/n: the full provision prompt set
                answers = fp + ['/dev/ttyACM0', '/srv/bridge/state/registry-tls', 'esp32-test',
                                'tls://192.0.2.10:10702', 'TestNet', '/tmp/psk.txt', '', 'no']
            elif flag == '--flash':               # seeded y/n/n: only the port
                answers = fp + ['/dev/ttyACM0', '', 'no']
            else:
                # No action flag typed: seeded flash + provision, so every prompt runs. The
                # prompt belonging to the flag under test accepts the typed default ('').
                answers = fp + [
                    '' if flag == '--port' else '/dev/ttyACM0',
                    '' if flag == '--tls-certs' else '/srv/bridge/state/registry-tls',
                    '' if flag == '--device-id' else 'esp32-test',
                    '' if flag == '--endpoint' else 'tls://192.0.2.10:10702',
                    '' if flag == '--wifi-ssid' else 'TestNet',
                    '' if flag == '--wifi-psk-file' else '/tmp/psk.txt',
                    '', 'no']
        else:  # install-gateway.sh
            answers = ['' if flag == '--base' else '/srv/typed-gwbase',
                       '' if flag == '--device-reachable-as' else '192.0.2.55',
                       '' if flag == '--registry-port' else '11701',
                       '' if flag == '--tls-port' else '11702',
                       '' if flag == '--registry-bind' else '0.0.0.0',
                       '' if flag == '--satellite-host' else '192.0.2.20:10700',
                       '',      # Hermes side (seeded by --skip-hermes when typed)
                       'no']
        return GuidedModeTests.run_guided(script, answers, *extra)

    def test_a_value_taking_flag_as_the_last_token_says_it_needs_a_value(self):
        """A value-taking flag with nothing after it used to exit 1 with NO message in all
        three parsers: the arm's own shift empties the list and the loop's shift dies under
        `set -e`. The parser must say what is missing instead."""
        for script in (ESP32, PI, GATEWAY):
            for flag, takes_value in sorted(self.surface(script).items()):
                if not takes_value or flag in CONTROL_FLAGS:
                    continue
                with self.subTest(script=script.name, flag=flag):
                    result = run(script, flag)     # the value flag is the LAST argv token
                    combined = result.stdout + result.stderr
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('needs a value', combined,
                                  'no message at all for a bare %s' % flag)
                    self.assertIn(flag, combined)

    def test_every_typed_flag_survives_into_the_guided_run_line(self):
        """The whole-class guard: enumerate the parser flag surface and drive guided mode with
        each flag typed. Every non-control flag's typed token (and its value) must appear in
        the run line the guided flow announces -- a flag silently missing there is the bug
        class that shipped."""
        for script in (ESP32, PI, GATEWAY):
            surface = self.surface(script)
            self.assertTrue(surface,
                            '%s: the surface regex matched no parse arms at all' % script.name)
            for flag, takes_value in sorted(surface.items()):
                if flag in CONTROL_FLAGS:
                    continue
                if takes_value:
                    self.assertIn(flag, self.TYPED[script.name],
                                  '%s: add a typed test value for %s (a new value-taking flag '
                                  'must be covered here)' % (script.name, flag))
                with self.subTest(script=script.name, flag=flag):
                    result = self.drive(script, flag)
                    run_line = GuidedModeTests.run_line(result)
                    self.assertTrue(run_line, 'guided flow printed no run line: %r'
                                    % (result.stdout + result.stderr))
                    if takes_value:
                        self.assertIn('%s %s' % (flag, self.TYPED[script.name][flag]), run_line,
                                      'typed %s did not survive into the run line' % flag)
                    else:
                        self.assertIn(flag, run_line.split(),
                                      'typed %s did not survive into the run line' % flag)


    def test_the_value_detector_actually_sees_value_taking_flags(self):
        """Guard-the-guard. If the value detector silently stops working, the two tests above
        iterate ZERO flags and pass while covering nothing (that happened: a substring test for
        '$2' misses the braced ${2...} form these parsers actually use). Assert the detector
        still sees both forms, and still treats a plain switch as a switch."""
        with tempfile.TemporaryDirectory() as d:
            probe = Path(d) / 'probe.sh'
            probe.write_text('        --braced)  X="${2:-}"; shift ;;\n'
                             '        --bare)    X="$2"; shift ;;\n'
                             '        --braced_nb) X="${2:?}"; shift ;;\n'
                             '        --switch)  X=1 ;;\n')
            seen = self.surface(probe)
        self.assertTrue(seen.get('--braced'), 'the ${2:-} form was not seen as value-taking')
        self.assertTrue(seen.get('--bare'), 'the $2 form was not seen as value-taking')
        self.assertTrue(seen.get('--braced_nb'), 'the ${2:?} form was not seen as value-taking')
        self.assertFalse(seen.get('--switch'), 'a plain switch was misread as value-taking')
        for script in (ESP32, PI, GATEWAY):
            with self.subTest(script=script.name):
                surface = self.surface(script)
                self.assertTrue(any(surface.values()),
                                '%s: the detector classified NO flag as value-taking, so the '
                                'typed-value guard above is vacuous' % script.name)


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