"""HTTP integration checks; the engine is a stub, FastAPI, upstream's request model and the
executors are real."""
import concurrent.futures
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from decider_server import create_app

NOUL = {'ok': {'type': 'noul', 'instructions': 'Is it ok?'}}


class Engine:
    model_name = 'test-model'

    def __init__(self):
        self.evaluated = []
        self.entered, self.release = threading.Event(), threading.Event()

    def describe(self):
        return {'model': self.model_name, 'device': 'cuda:0'}

    def evaluate(self, request):
        from strands_decider.refusals import RequestRefused
        from strands_decider.schema import SystemOneResponse

        self.evaluated.append(request)
        if request.state == 'invalid':
            raise RequestRefused('invalid question')
        if request.state == 'block':
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError('test timeout')
        return SystemOneResponse.model_validate({'model': self.model_name,
            'answers': {'ok': {'type': 'noul', 'noul': .9}},
            'usage': {'input_tokens': 1, 'output_tokens': 1}})


class ServerTests(unittest.TestCase):
    def test_lifespan_admission_and_http_contract(self):
        engine = Engine()
        env = {'DECIDER_MODEL_ID': 'org/test-model', 'DECIDER_INFERENCE_WORKERS': '1',
               'DECIDER_QUEUE_SIZE': '0', 'DECIDER_TORCH_THREADS': '1', 'DECIDER_BATCHING': 'false'}
        with patch.dict(os.environ, env), patch('torch.cuda.is_available', return_value=True):
            with TestClient(create_app(load=lambda model_id, tokens: engine)) as client:
                self.assertEqual(len(engine.evaluated), 2)  # warmup before startup finishes
                health = client.get('/health').json()
                self.assertEqual((health['status'], health['model']), ('ok', 'test-model'))
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    first = executor.submit(client.post, '/v1/systemone',
                                            json={'state': 'block', 'questions': NOUL})
                    try:
                        self.assertTrue(engine.entered.wait(1))
                        self.assertEqual(client.get('/health').json()['pending'], 1)
                        rejected = client.post('/v1/systemone', json={'state': 'extra', 'questions': NOUL})
                        self.assertEqual(rejected.status_code, 503)
                        self.assertEqual(rejected.headers['retry-after'], '1')
                    finally:
                        engine.release.set()
                    result = first.result(timeout=2)
                    self.assertEqual(result.status_code, 200)
                    payload = result.json()
                    self.assertEqual(payload['model'], 'test-model')
                    self.assertEqual(payload['answers']['ok']['noul'], .9)
                    self.assertTrue({'queue_ms', 'inference_ms', 'latency_ms'} <= payload.keys())
                self.assertEqual(client.post('/v1/systemone',
                    json={'state': 'invalid', 'questions': NOUL}).status_code, 422)
                self.assertEqual(client.post('/v1/systemone', json={'state': 'x'}).status_code, 422)
                self.assertEqual(client.post('/v1/systemone', content=b'{').status_code, 422)
                self.assertEqual(client.get('/health').json()['pending'], 0)

    def test_batching_lifespan_uses_adapter_and_reports_refusals(self):
        engine = Engine()
        batches = []

        class Adapter:
            def __init__(self, engine, **kwargs):
                self.engine = engine

            def evaluate(self, requests):
                batches.append(len(requests))
                out = []
                for request in requests:
                    try:
                        out.append(self.engine.evaluate(request).model_dump())
                    except ValueError as exc:
                        out.append(exc)
                return out

        env = {'DECIDER_MODEL_ID': 'org/test-model', 'DECIDER_BATCHING': 'true',
               'DECIDER_MAX_BATCH': '4', 'DECIDER_QUEUE_SIZE': '8'}
        with patch.dict(os.environ, env), patch('torch.cuda.is_available', return_value=True), \
                patch('torch.cuda.synchronize'), patch('decider_server.BatchEngine', Adapter):
            with TestClient(create_app(load=lambda model_id, tokens: engine)) as client:
                self.assertEqual(batches, [1, 2, 4])  # warmup shapes
                ok = client.post('/v1/systemone', json={'state': 'fine', 'questions': NOUL})
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(ok.json()['batch_size'], 1)
                self.assertEqual(client.post('/v1/systemone',
                    json={'state': 'invalid', 'questions': NOUL}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
