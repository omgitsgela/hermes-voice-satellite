#!/usr/bin/env python3
"""Build the click-through preview from the shipped UI and real catalogue/sample clips.

Only the transport is replaced by an explicitly offline fixture. No server, gateway, device,
service, model download or system configuration is accessed by the resulting HTML. This is a
visual/flow review artifact, never evidence that an installation was performed.

Usage: python scripts/build-installer-preview.py --output <preview.html>
Network is used at build time for unbundled sample MP3s, cached outside the checkout.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package in ("installer", "gateway", "device-esp32", "device-pi"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

from hermes_installer.session import Session  # noqa: E402

ADAPTER = r"""
// Offline review transport. Fixture outcomes are not installation results.
window.PREVIEW_REQUESTS = [];
const fixture = PREVIEW_DATA;
window.fetch = async (path, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : {};
  const url = new URL(path, 'https://installer.example');
  window.PREVIEW_REQUESTS.push({path: url.pathname, body});
  let result = {ok:true};
  switch(url.pathname) {
    case '/api/status': result = fixture.status; break;
    case '/api/voices': result = fixture.voices; break;
    case '/api/wake-words': result = fixture.wake[url.searchParams.get('format')]; break;
    case '/api/ports': result.ports = [{device:'/dev/example-satellite',description:'ESP32 USB data connection'}]; break;
    case '/api/mounts': result.mounts = [{mount:'/media/user/rootfs'}]; break;
    case '/api/device-id': result.device_id = 'esp32-example'; break;
    case '/api/inspect': result.identity_code = 'XK7F-9Q2M'; break;
    case '/api/pair':
      result = {ok:true,identity_code:'XK7F-9Q2M',dial_address:'tls://gateway.example:10702',certs_dir:'example-credentials'}; break;
    case '/api/preflight': result.problems = []; break;
    case '/api/manage':
      if (!body.wifi_ssid && !body.wake_word && !body.wake_model) result = {ok:false,error:'Choose a network or wake word to change.'};
      else result = {ok:true,summary:'Selected settings saved.',commands:[{argv:['settings','--selected-only'],description:'Save the selected settings, keeping pairing and firmware'}]};
      break;
    case '/api/flash': result = {ok:true,commands:[{argv:['install','--device',body.device_profile],description:'Install and provision the selected satellite'}]}; break;
    case '/api/gateway':
      result = {ok:true,commands:[['install-gateway','--address',body.dial_address]]};
      if(!body.dry_run) result.card = {dial_address:'tls://'+body.dial_address+':'+body.tls_port,identity_code:'XK7F-9Q2M',words:'amber lantern copper thrush meadow'};
      break;
    case '/api/voice': {
      const voice = fixture.voices.voices.find(v=>v.id===body.voice);
      if(!voice) result = {ok:false,error:'That voice is not in the catalogue.'};
      else { voice.installed=true; result = {ok:true,voice:voice.id,applied:body.apply,apply_note:body.apply ? 'Shared voice selected for the next Satellite Gateway restart.' : 'Downloaded. The shared voice is unchanged.'}; }
      break;
    }
    case '/api/wake-word': result = {ok:true,path:'example-model.'+(body.format.includes('tflite')?'tflite':'onnx')}; break;
    case '/api/wake-model-upload': {
      const file=body.files?.[0];
      result = file ? {ok:true,path:file.name,name:file.name.replace(/\.(onnx|tflite)$/,'')} : {ok:false,error:'Choose a model file.'}; break;
    }
    case '/api/voice-custom':
      // Never pass a bundled clip off as the person's own uploaded model.
      result = {ok:false,error:'A local Piper runtime is required to synthesize and validate a custom voice. No custom voice has been installed.'}; break;
    case '/api/wake-words/community': result = {ok:true,results:[],note:'Community measurements need a network connection. Custom model files are always accepted.'}; break;
    default: result = {ok:false,error:'That operation is not available.'};
  }
  return {ok:result.ok !== false,json:async()=>JSON.parse(JSON.stringify(result))};
};
"""


def build(output: Path, cache: Path) -> None:
    session = Session(ROOT, dry_run=True)
    try:
        status = session.status()
        # Deliberate fixture, not discovery of the review computer's installation.
        status.update(hermes="desktop", hermes_explained="Hermes Desktop found. The Satellite Gateway can be installed beside it.",
                      platform={"notes": [], "can_flash": True, "can_supervise": True}, dry_run=False,
                      lan_address="192.0.2.24")
        voices = session.voice_catalogue()
        cache.mkdir(parents=True, exist_ok=True)
        for voice in voices["voices"]:
            sample = voice["sample_url"]
            if sample.startswith("/samples/"):
                content = (ROOT / "packages/installer/src/hermes_installer/web" / sample.lstrip("/")).read_bytes()
            else:
                cached = cache / (voice["id"] + ".mp3")
                if not cached.exists():
                    with urllib.request.urlopen(sample, timeout=60) as response:
                        content = response.read()
                    if not content or content.startswith(b"<"):
                        raise ValueError(f"{voice['id']}: sample was not audio")
                    cached.write_bytes(content)
                content = cached.read_bytes()
            voice["sample_url"] = "data:audio/mpeg;base64," + base64.b64encode(content).decode()
            voice["installed"] = False
        payload = {"status": status, "voices": voices, "wake": {
            fmt: session.wake_word_catalogue(fmt) for fmt in ("openwakeword_onnx", "microwakeword_tflite")
        }}
        adapter = ADAPTER.replace("PREVIEW_DATA", json.dumps(payload).replace("</", "<\\/"))
        html = (ROOT / "packages/installer/src/hermes_installer/web/index.html").read_text()
        html = html.replace("</h1>", '</h1><span class="pill">Preview</span>', 1)
        html = html.replace("<script>", "<script>" + adapter + "</script>\n<script>", 1)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html)
        print(f"Built {output.name}: {len(voices['voices'])} catalogue voices, embedded MP3 samples, shipped UI.")
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-cache", type=Path,
                        default=Path.home() / ".cache/hermes-voice-satellite/installer-samples")
    args = parser.parse_args()
    build(args.output, args.sample_cache)
