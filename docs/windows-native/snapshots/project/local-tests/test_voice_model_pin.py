import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from satellite_gateway.gateway import VoiceGateway

class HistoryResponse:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self, **kw): return self._payload

class PinRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_pin_is_sent_with_fallback_allowed(self):
        captured = {}
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self, **kw): return {'run_id':'test-run'}
        class Session:
            def post(self, url, **kw):
                captured.update(kw['json'])
                return Response()
            def get(self, url, **kw):
                return HistoryResponse(404, {})
        pin = {'model':'gpt-6-luna','provider':'openai-codex',
               'model_options':{'reasoning_effort':'low','service_tier':'priority'},
               'require_model_lock': False}
        try:
            client = VoiceGateway(base_url='http://test',api_key='synthetic',session_id='test',runtime_config=pin)
        except TypeError:
            self.fail('VoiceGateway does not support an explicit durable runtime pin')
        client._session = Session()
        async def events(rid):
            yield {'event':'message.delta','delta':'READY'}
            yield {'event':'run.completed'}
        client._events = events
        self.assertEqual(''.join([s async for s in client.ask('test')]), 'READY')
        for key, value in pin.items(): self.assertEqual(captured.get(key), value)

class HistoryRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_prior_turns_are_resumed_as_conversation_history(self):
        captured = {}
        class HistoryResponse:
            def __init__(self, status, payload):
                self.status, self._payload = status, payload
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self, **kw): return self._payload
        class RunResponse:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self, **kw): return {'run_id':'test-run'}
        stored = {'object':'list','session_id':'test','data':[  # 'latest' page: newest first
            {'role':'tool','content':'noise that must never leak in'},
            {'role':'assistant','content':'Noted: teal.'},
            {'role':'user','content':'my favourite colour is teal'},
        ]}
        class Session:
            def post(self, url, **kw):
                captured.update(kw['json'])
                return RunResponse()
            def get(self, url, **kw):
                assert '/api/sessions/test/messages' in url, url
                return HistoryResponse(200, stored)
        client = VoiceGateway(base_url='http://test', api_key='***',
                              session_id='test', runtime_config={})
        client._session = Session()
        async def events(rid):
            yield {'event':'message.delta','delta':'TEAL'}
            yield {'event':'run.completed'}
        client._events = events
        self.assertEqual(''.join([s async for s in client.ask('what is my favourite colour?')]), 'TEAL')
        self.assertEqual(captured['conversation_history'], [
            {'role':'user','content':'my favourite colour is teal'},
            {'role':'assistant','content':'Noted: teal.'},
        ])

    async def test_missing_history_session_degrades_to_a_fresh_turn(self):
        captured = {}
        class Session:
            def post(self, url, **kw):
                captured.update(kw['json'])
                class R:
                    status = 200
                    async def __aenter__(self_): return self_
                    async def __aexit__(self_, *a): pass
                    async def json(self_, **k): return {'run_id':'test-run'}
                return R()
            def get(self, url, **kw):
                class R:
                    status = 404
                    async def __aenter__(self_): return self_
                    async def __aexit__(self_, *a): pass
                    async def json(self_, **k): return {'error':'not found'}
                return R()
        client = VoiceGateway(base_url='http://test', api_key='***',
                              session_id='test', runtime_config={})
        client._session = Session()
        async def events(rid):
            yield {'event':'message.delta','delta':'HI'}
            yield {'event':'run.completed'}
        client._events = events
        self.assertEqual(''.join([s async for s in client.ask('hello')]), 'HI')
        self.assertNotIn('conversation_history', captured)

class PinConfigTest(unittest.TestCase):
    def test_reads_nonsecret_pin_and_rejects_missing_explicit_identity(self):
        from satellite_gateway import gateway
        self.assertTrue(hasattr(gateway, 'load_runtime_config'), 'durable runtime config loader is missing')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'voice-model.json'
            self.assertEqual(gateway.load_runtime_config(path), {})
            pin = {'model':'gpt-6-luna','provider':'openai-codex','require_model_lock':False,
                   'model_options':{'reasoning_effort':'low','service_tier':'priority'}}
            path.write_text(json.dumps(pin))
            self.assertEqual(gateway.load_runtime_config(path), pin)
            path.write_text('{"model":"gpt-6-luna"}')
            with self.assertRaises(ValueError): gateway.load_runtime_config(path)

if __name__ == '__main__': unittest.main()
