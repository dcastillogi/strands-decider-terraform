"""Bound inference execution and queue size without blocking the HTTP event loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time


class QueueFull(Exception):
    pass


class InferencePool:
    def __init__(self, evaluate, workers=2, queue_size=64):
        if workers < 1 or queue_size < 0:
            raise ValueError('workers must be positive and queue_size nonnegative')
        self.evaluate = evaluate
        self.capacity = workers + queue_size
        self.pending = 0
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='inference')

    async def submit(self, request):
        # Called only on the event loop: admission is atomic until the first await.
        if self.pending >= self.capacity:
            raise QueueFull('inference queue is full')
        self.pending += 1
        submitted = time.perf_counter()

        def execute():
            started = time.perf_counter()
            result = self.evaluate(request).model_dump()
            ended = time.perf_counter()
            result['queue_ms'] = round((started - submitted) * 1000, 2)
            result['inference_ms'] = round((ended - started) * 1000, 2)
            result['latency_ms'] = round((ended - submitted) * 1000, 2)
            return result

        try:
            future = asyncio.get_running_loop().run_in_executor(self.executor, execute)
        except Exception:
            self.pending -= 1
            raise

        def completed(future):
            self.pending -= 1
            # Retrieve errors even if the HTTP client disconnected during inference.
            if not future.cancelled():
                future.exception()

        future.add_done_callback(completed)
        # Cancellation cannot release a permit while CUDA work is still running.
        return await asyncio.shield(future)

    async def close(self):
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
