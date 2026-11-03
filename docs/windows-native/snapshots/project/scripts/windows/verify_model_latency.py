"""Bounded real gateway probes; never prints credentials. Not an audio test."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import time
import uuid
import urllib.request

ROOT = Path(__file__).resolve().parents[2]

def probe(model):
    token = (ROOT / 'state/hermes-api-key.txt').read_text().strip()
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    sid = 'satellite-model-probe-' + uuid.uuid4().hex
    body = {'input': 'Reply with exactly READY. Do not use tools.', 'session_id': sid,
            'model': model, 'provider': 'openai-codex',
            'model_options': {'reasoning_effort': 'low', 'service_tier': 'priority'}}
    start = time.monotonic()
    req = urllib.request.Request('http://127.0.0.1:8642/v1/runs', data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        run = json.load(r)
    rid = run['run_id']
    req = urllib.request.Request('http://127.0.0.1:8642/v1/runs/' + rid + '/events', headers=headers)
    first = None
    reply = ''
    terminal = None
    with urllib.request.urlopen(req, timeout=100) as r:
        for raw in r:
            if not raw.startswith(b'data:'):
                continue
            event = json.loads(raw[5:].decode())
            name = event.get('event', '')
            if name == 'message.delta':
                if first is None:
                    first = time.monotonic() - start
                reply += event.get('delta', '')
            if name in ('run.completed', 'run.failed', 'run.cancelled'):
                terminal = {k:v for k,v in event.items() if k in ('event','error','model','provider','runtime','usage')}
                break
    return {'requested_model': model, 'provider': 'openai-codex', 'reasoning_effort': 'low',
            'service_tier': 'priority', 'session_id': sid, 'run_id': rid,
            'first_text_seconds': round(first,3) if first is not None else None,
            'total_seconds': round(time.monotonic()-start,3), 'reply': reply, 'terminal': terminal}

if __name__ == '__main__':
    models = ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6.1-terra', 'gpt-6-sol', 'gpt-6-luna']
    results = []
    # Two at once avoids serial network latency without saturating the subscription.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(probe, models):
            results.append(result)
            print(json.dumps(result), flush=True)
    (ROOT/'logs/windows/model-latency-probes.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
