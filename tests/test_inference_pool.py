import asyncio
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from inference_pool import InferencePool, QueueFull


class Result:
    def __init__(self, value):
        self.value = value

    def model_dump(self):
        return {'answers': self.value}


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_execution_and_rejects_overflow(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def evaluate(value):
            calls.append(value)
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test worker was not released')
            return Result(value)

        pool = InferencePool(evaluate, workers=1, queue_size=1)
        try:
            first = asyncio.create_task(pool.submit(1))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            second = asyncio.create_task(pool.submit(2))
            await asyncio.sleep(0)
            self.assertEqual(calls, [1])
            with self.assertRaises(QueueFull):
                await pool.submit(3)
            release.set()
            results = await asyncio.gather(first, second)
            self.assertEqual([r['answers'] for r in results], [1, 2])
            self.assertEqual(calls, [1, 2])
            self.assertGreaterEqual(results[1]['queue_ms'], 0)
            self.assertGreaterEqual(results[0]['latency_ms'], results[0]['inference_ms'])
            self.assertEqual(pool.pending, 0)
        finally:
            release.set()
            await pool.close()

    async def test_cancelled_client_does_not_release_active_capacity(self):
        entered, release = threading.Event(), threading.Event()

        def evaluate(value):
            entered.set()
            release.wait(2)
            return Result(value)

        pool = InferencePool(evaluate, workers=1, queue_size=0)
        try:
            task = asyncio.create_task(pool.submit(1))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(pool.pending, 1)
            with self.assertRaises(QueueFull):
                await pool.submit(2)
            release.set()
            await pool.close()
            self.assertEqual(pool.pending, 0)
        finally:
            release.set()
            await pool.close()

    async def test_error_releases_capacity(self):
        def evaluate(value):
            if value == 'invalid':
                raise ValueError('invalid request')
            return Result(value)
        pool = InferencePool(evaluate, workers=1, queue_size=0)
        try:
            with self.assertRaisesRegex(ValueError, 'invalid request'):
                await pool.submit('invalid')
            self.assertEqual(pool.pending, 0)
            self.assertEqual((await pool.submit('valid'))['answers'], 'valid')
        finally:
            await pool.close()

    async def test_invalid_configuration(self):
        for workers, queue in [(0, 1), (1, -1)]:
            with self.assertRaises(ValueError):
                InferencePool(lambda value: Result(value), workers, queue)


if __name__ == '__main__':
    unittest.main()
