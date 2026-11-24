import asyncio
from pathlib import Path
import sys
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from batch_pool import BatchPool
from inference_pool import QueueFull


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_requests_preserves_order_and_isolates_errors(self):
        calls = []
        def evaluate(requests):
            calls.append(requests)
            return [ValueError('invalid') if r == 2 else {'answer': r} for r in requests]
        pool = BatchPool(evaluate, max_batch=4, wait_ms=5, queue_size=0)
        try:
            results = await asyncio.gather(*(pool.submit(n) for n in range(4)), return_exceptions=True)
            self.assertEqual(calls, [[0, 1, 2, 3]])
            self.assertEqual(results[0]['answer'], 0)
            self.assertEqual(results[1]['answer'], 1)
            self.assertIsInstance(results[2], ValueError)
            self.assertEqual(results[3]['answer'], 3)
            self.assertEqual(results[0]['batch_size'], 4)
            self.assertEqual(pool.pending, 0)
        finally:
            await pool.close()

    async def test_cancellation_does_not_exceed_capacity(self):
        entered, release = threading.Event(), threading.Event()
        def evaluate(requests):
            entered.set()
            release.wait(2)
            return [{'answer': r} for r in requests]
        pool = BatchPool(evaluate, max_batch=1, wait_ms=0, queue_size=0)
        try:
            task = asyncio.create_task(pool.submit(1))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(QueueFull):
                await pool.submit(2)
        finally:
            release.set()
            await pool.close()
        self.assertEqual(pool.pending, 0)

    async def test_batch_failure_does_not_kill_worker(self):
        def evaluate(requests):
            if requests == [0]:
                raise RuntimeError('failed batch')
            return [{'answer': r} for r in requests]
        pool = BatchPool(evaluate, max_batch=1, wait_ms=0)
        try:
            with self.assertRaisesRegex(RuntimeError, 'failed batch'):
                await pool.submit(0)
            self.assertEqual((await pool.submit(1))['answer'], 1)
        finally:
            await pool.close()
