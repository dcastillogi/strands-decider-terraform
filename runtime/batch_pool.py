"""A bounded microbatch queue with one model executor and cancellation-safe admission."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import time

from inference_pool import QueueFull


class BatchPool:
    def __init__(self, evaluate_batch, max_batch=16, wait_ms=2, queue_size=256):
        if max_batch < 1 or wait_ms < 0 or queue_size < 0:
            raise ValueError('invalid batch configuration')
        self.evaluate_batch = evaluate_batch
        self.max_batch = max_batch
        self.wait_seconds = wait_ms / 1000
        self.capacity = queue_size + max_batch
        self.pending = 0
        self.queue = asyncio.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='model-batch')
        self.worker = asyncio.create_task(self._run())
        self.closing = False

    async def submit(self, request):
        if self.closing or self.pending >= self.capacity:
            raise QueueFull('inference queue is full')
        self.pending += 1
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self.queue.put_nowait((request, future, time.perf_counter()))
        return await asyncio.shield(future)

    async def _run(self):
        while True:
            items = [await self.queue.get()]
            deadline = time.perf_counter() + self.wait_seconds
            while len(items) < self.max_batch:
                try:
                    items.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    try:
                        items.append(await asyncio.wait_for(self.queue.get(), remaining))
                    except asyncio.TimeoutError:
                        break
            started = time.perf_counter()
            try:
                results = await asyncio.get_running_loop().run_in_executor(
                    self.executor, self.evaluate_batch, [item[0] for item in items])
                if len(results) != len(items):
                    raise RuntimeError('batch evaluator returned the wrong number of responses')
                ended = time.perf_counter()
                for (_, future, submitted), result in zip(items, results, strict=True):
                    if isinstance(result, Exception):
                        future.set_exception(copy.copy(result).with_traceback(None))
                    else:
                        result = dict(result)
                        result.update(queue_ms=round((started - submitted) * 1000, 2),
                                      inference_ms=round((ended - started) * 1000, 2),
                                      latency_ms=round((ended - submitted) * 1000, 2),
                                      batch_size=len(items))
                        future.set_result(result)
            except Exception as exc:
                for _, future, _ in items:
                    if not future.done():
                        # Do not expose the live scheduler's traceback frames to
                        # consumers that clear exception frames after handling it.
                        future.set_exception(copy.copy(exc).with_traceback(None))
            finally:
                self.pending -= len(items)
                for _ in items:
                    self.queue.task_done()

    async def close(self):
        self.closing = True
        await self.queue.join()
        self.worker.cancel()
        try:
            await self.worker
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(self.executor.shutdown, wait=True)
