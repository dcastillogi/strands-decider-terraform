"""Cross-request batches against upstream's own engine, on a tiny random Gemma 4 checkpoint.

The checkpoint is the E2B layout scaled down (sliding and full layers, KV-shared layers,
per-layer inputs), with the real E2B tokenizer and decision_config.json and a random readout.
It loads through upstream's `load_decision_engine` on the CPU in float32, so the comparison
exercises upstream rendering, the prefix_bidir attention and the readout, without a GPU.
"""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from batch_engine import BatchEngine, prefix_bidir_mask
from verify_answers_for_tests import fixture_requests

REPO = 'StrandsAgents/strands-decider-E2B-gemma4-v2-2611'
REVISION = 'main'


def tiny_checkpoint(directory):
    import transformers
    from huggingface_hub import hf_hub_download
    from safetensors.numpy import save_file

    for name in ['config.json', 'decision_config.json', 'tokenizer.json', 'tokenizer_config.json']:
        shutil.copy(hf_hub_download(REPO, name, revision=REVISION), directory / name)
    config = json.loads((directory / 'config.json').read_text())
    text = config['text_config']
    text.update(hidden_size=32, intermediate_size=64, num_hidden_layers=6, num_kv_shared_layers=2,
                num_attention_heads=2, num_key_value_heads=1, head_dim=16, global_head_dim=32,
                hidden_size_per_layer_input=8, sliding_window=8,
                layer_types=['sliding_attention', 'sliding_attention', 'full_attention'] * 2)
    config['vision_config'] = None
    config['audio_config'] = None
    config['dtype'] = 'float32'
    model_config = transformers.Gemma4Config.from_dict(config)
    torch.manual_seed(0)
    model = transformers.Gemma4ForConditionalGeneration(model_config).eval()
    model.save_pretrained(directory)
    rng = np.random.default_rng(0)
    save_file({'weight': rng.normal(0, 0.5, (255, 32)).astype(np.float32)},
              str(directory / 'readout.safetensors'))


def adapter_render(engine, request):
    from strands_decider.core import render

    return render(request, engine.tokenizer, max_length=engine.max_length)


class BatchEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from strands_decider.decision import load_decision_engine

        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name)
        tiny_checkpoint(path)
        cls.engine = load_decision_engine(str(path), device='cpu', model_name='tiny')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def requests(self):
        return fixture_requests()

    def assertSameAnswers(self, expected, actual, tolerance=2e-3):
        self.assertEqual(expected.keys(), actual.keys())
        for name, answer in expected.items():
            got = actual[name]
            self.assertEqual(answer['type'], got['type'])
            if answer['type'] == 'choice':
                self.assertEqual(answer['choice'], got['choice'])
            for key in ('noul', 'score', 'confidence'):
                if key in answer:
                    self.assertAlmostEqual(answer[key], got[key], delta=tolerance)
            for option, value in answer.get('probabilities', {}).items():
                self.assertAlmostEqual(value, got['probabilities'][option], delta=tolerance)

    def test_mask_is_prefix_bidir_without_padding_keys(self):
        mask = prefix_bidir_mask([2, 1], [4, 2], 4)[:, 0]
        # Row 0: state of 2, question of 2. Row 1: state of 1, question of 1, two pads.
        self.assertEqual(mask[0].int().tolist(), [[1, 1, 0, 0], [1, 1, 0, 0], [1, 1, 1, 1], [1, 1, 1, 1]])
        self.assertEqual(mask[1].int().tolist(), [[1, 0, 0, 0], [1, 1, 0, 0], [1, 1, 0, 0], [1, 1, 0, 0]])

    def test_batched_answers_match_upstream_engine(self):
        requests = self.requests()
        expected = [self.engine.evaluate(r).model_dump() for r in requests]
        adapter = BatchEngine(self.engine, max_rows=256, max_tokens=8192)
        actual = adapter.evaluate(requests)
        for want, got in zip(expected, actual, strict=True):
            self.assertEqual(want['model'], got['model'])
            self.assertEqual(want['usage'], got['usage'])
            self.assertSameAnswers(want['answers'], got['answers'])

    def test_small_token_budget_splits_batches_and_routes_long_prompts_upstream(self):
        requests = self.requests()
        expected = [self.engine.evaluate(r).model_dump() for r in requests]
        adapter = BatchEngine(self.engine, max_rows=3, max_tokens=300, shared_state_tokens=10**9)
        calls = []
        forward = adapter._forward
        adapter._forward = lambda rows: calls.append(len(rows)) or forward(rows)
        actual = adapter.evaluate(requests)
        self.assertTrue(calls and max(calls) <= 3)
        for want, got in zip(expected, actual, strict=True):
            self.assertSameAnswers(want['answers'], got['answers'])

    def test_long_shared_states_use_upstream_engine(self):
        requests = self.requests()
        expected = [self.engine.evaluate(r).model_dump() for r in requests]
        adapter = BatchEngine(self.engine, shared_state_tokens=200)
        rows = []
        forward = adapter._forward
        adapter._forward = lambda batch: rows.extend(batch) or forward(batch)
        actual = adapter.evaluate(requests)
        batched = {row['request'] for row in rows}
        routed = [i for i, r in enumerate(requests)
                  if (len(r.questions) - 1) * len(next(iter(adapter_render(self.engine, r).questions.values())).state_ids) > 200]
        self.assertTrue(routed)
        self.assertFalse(batched & set(routed))
        for want, got in zip(expected, actual, strict=True):
            self.assertSameAnswers(want['answers'], got['answers'])

    def test_refusals_are_returned_per_request(self):
        from strands_decider.core import Request
        from strands_decider.refusals import RequestRefused

        good, = self.requests()[:1]
        bad = Request.model_validate({'state': 'x ' * 40000, 'questions': {
            'q': {'type': 'noul', 'instructions': 'Is it long?'}}})
        results = BatchEngine(self.engine).evaluate([good, bad])
        self.assertIsInstance(results[0], dict)
        self.assertIsInstance(results[1], RequestRefused)


if __name__ == '__main__':
    unittest.main()
