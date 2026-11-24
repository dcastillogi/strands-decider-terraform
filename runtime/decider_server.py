"""Serve a Strands Decider v2 checkpoint with bounded, cross-request GPU batching."""
import os
from contextlib import asynccontextmanager

from inference_pool import InferencePool, QueueFull
from batch_engine import BatchEngine
from batch_pool import BatchPool

WARMUP_STATE = 'The build passed all tests.'
WARMUP_QUESTIONS = {
    'healthy': {'type': 'noul', 'instructions': 'Is the build healthy?'},
    'team': {'type': 'choice', 'instructions': 'Who should handle this?',
             'criteria': {'engineering': None, 'billing': None}},
    'quality': {'type': 'score', 'instructions': 'Rate build quality.',
                'criteria': ['poor', 'good', 'excellent']},
}


def load_engine(model_id, max_batch_tokens):
    from strands_decider.decision import is_model_checkpoint, load_decision_engine

    if not is_model_checkpoint(model_id):
        raise RuntimeError(f'{model_id} is not a Strands Decider v2 model checkpoint')
    return load_decision_engine(
        model_id, device='cuda', model_name=model_id.rstrip('/').rsplit('/', 1)[-1],
        max_batch_tokens=max_batch_tokens, max_concurrency=1, queue_timeout=60.0,
    )


def create_app(load=load_engine):
    import torch
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from pydantic import ValidationError
    from strands_decider.core import Request as DecisionRequest
    from strands_decider.refusals import EngineBusy

    model_id = os.environ['DECIDER_MODEL_ID']
    workers = int(os.environ.get('DECIDER_INFERENCE_WORKERS', '1'))
    queue_size = int(os.environ.get('DECIDER_QUEUE_SIZE', '256'))
    batching = os.environ.get('DECIDER_BATCHING', 'true') == 'true'
    max_batch = int(os.environ.get('DECIDER_MAX_BATCH', '8'))
    wait_ms = float(os.environ.get('DECIDER_BATCH_WAIT_MS', '2'))
    max_tokens = int(os.environ.get('DECIDER_MAX_BATCH_TOKENS', '8192'))
    shared_state = int(os.environ.get('DECIDER_SHARED_STATE_TOKENS', '512'))
    torch.set_num_threads(int(os.environ.get('DECIDER_TORCH_THREADS', '1')))
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:  # already set in this process (tests create several apps)
        pass
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is required; refusing to fall back to CPU')

    @asynccontextmanager
    async def lifespan(app):
        engine = load(model_id, max_tokens)
        app.state.engine = engine
        warmup = DecisionRequest.model_validate({'state': WARMUP_STATE, 'questions': WARMUP_QUESTIONS})
        single = DecisionRequest.model_validate(
            {'state': WARMUP_STATE, 'questions': {'healthy': WARMUP_QUESTIONS['healthy']}})
        # Initialise lazy CUDA paths before the listener accepts traffic.
        engine.evaluate(warmup)
        engine.evaluate(single)
        if batching:
            adapter = BatchEngine(engine, max_rows=256, max_tokens=max_tokens,
                                  shared_state_tokens=shared_state)
            for size in sorted({1, 2, max_batch // 2, max_batch} - {0}):
                adapter.evaluate([warmup] * size)
            torch.cuda.synchronize()
            app.state.pool = BatchPool(adapter.evaluate, max_batch, wait_ms, queue_size)
        else:
            app.state.pool = InferencePool(engine.evaluate, workers, queue_size)
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(title='Strands Decider v2', version='0.2.0', lifespan=lifespan)

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'pid': os.getpid(), **app.state.engine.describe(),
                'model_processes': int(os.environ.get('DECIDER_MODEL_PROCESSES', '1')),
                'inference_workers': 1 if batching else workers,
                'pending': app.state.pool.pending, 'queue_size': queue_size,
                'batching': batching, 'max_batch': max_batch, 'batch_wait_ms': wait_ms,
                'max_batch_tokens': max_tokens, 'shared_state_tokens': shared_state}

    @app.post('/v1/systemone')
    async def systemone(http: Request):
        try:
            # Upstream's v2 request model: it refuses audio and permits image-only states.
            request = DecisionRequest.model_validate_json(await http.body())
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            payload = await app.state.pool.submit(request)
        except (QueueFull, EngineBusy) as exc:
            raise HTTPException(status_code=503, detail=str(exc),
                                headers={'Retry-After': '1'}) from exc
        except ValueError as exc:  # includes upstream RequestRefused
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(content=payload)

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('decider_server:create_app', factory=True, host='127.0.0.1', port=8001,
                workers=int(os.environ.get('DECIDER_MODEL_PROCESSES', '1')))
