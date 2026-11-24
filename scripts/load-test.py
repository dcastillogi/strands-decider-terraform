#!/usr/bin/env python3
"""Closed-loop load test with persistent connections and a unique state per request.

Each worker sends a request, waits for the whole response and sends the next. Per request it
records the time to the response headers (time to first byte) and to the full body.
"""
import argparse
import concurrent.futures
import http.client
import json
import threading
import time
from urllib.parse import urlsplit

from common import ROOT, add_target_arguments, percentile, resolve_target

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--warmup-seconds', type=float, default=3)
    parser.add_argument('--request', default='examples/triage.json')
    parser.add_argument('--concurrency', default='1,2,4,8,16,32')
    parser.add_argument('--output', default='deployment-results/load-test')
    parser.add_argument('--max-error-rate', type=float, default=.10,
                        help='Stop after a stage with a larger fraction of failed requests')
    add_target_arguments(parser)
    args = parser.parse_args()
    endpoint, key = resolve_target(args)
    url = urlsplit(endpoint)
    template = json.loads((ROOT / args.request).read_text())
    directory = ROOT / args.output / time.strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True, exist_ok=True)
    report = {'endpoint': endpoint,
              'workload': args.request + ': unique ticket ID per request',
              'warmup_seconds': args.warmup_seconds,
              'stage_seconds': args.seconds, 'stages': []}
    print('concurrency requests errors successful_rps p50_ms p95_ms p99_ms ttfb_p50_ms server_p50_ms batch_size_mean queue_p95_ms', flush=True)
    with (directory / 'requests.jsonl').open('w') as raw:
        for concurrency in map(int, args.concurrency.split(',')):
            barrier = threading.Barrier(concurrency + 1)
            samples = []
            deadline = [0.0]
            def worker(worker_id):
                connection = http.client.HTTPConnection(url.hostname, url.port, timeout=35)
                local = []
                counter = 0
                barrier.wait()
                while time.perf_counter() < deadline[0]:
                    counter += 1
                    body = dict(template)
                    body['state'] += f' Ticket ID: {concurrency:02}-{worker_id:02}-{counter:06}.'
                    started = time.perf_counter()
                    status, error, server_ms, tokens, ttfb = 0, None, None, None, None
                    queue_ms, inference_ms, batch_size = None, None, None
                    try:
                        connection.request('POST', url.path, json.dumps(body),
                                           {'x-api-key': key, 'Content-Type': 'application/json'})
                        response = connection.getresponse()
                        ttfb = (time.perf_counter() - started) * 1000
                        status = response.status
                        data = response.read()
                        if status == 200:
                            result = json.loads(data)
                            if set(result.get('answers', {})) != set(template['questions']):
                                error = 'invalid answers'
                            server_ms = result.get('latency_ms')
                            queue_ms = result.get('queue_ms')
                            inference_ms = result.get('inference_ms')
                            batch_size = result.get('batch_size')
                            tokens = result.get('usage', {}).get('input_tokens')
                        else:
                            error = data.decode(errors='replace')[:200]
                    except Exception as exc:
                        error = str(exc)
                        connection.close()
                        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=35)
                    if started < deadline[0] - args.seconds:
                        continue
                    local.append({'concurrency': concurrency, 'status': status, 'error': error,
                                  'latency_ms': (time.perf_counter() - started) * 1000, 'ttfb_ms': ttfb,
                                  'server_ms': server_ms, 'input_tokens': tokens,
                                  'queue_ms': queue_ms, 'inference_ms': inference_ms, 'batch_size': batch_size})
                connection.close()
                return local
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(worker, n) for n in range(concurrency)]
                stage_started_epoch = time.time() + args.warmup_seconds
                started = time.perf_counter()
                deadline[0] = started + args.warmup_seconds + args.seconds
                barrier.wait()
                for future in futures:
                    samples.extend(future.result())
                elapsed = time.perf_counter() - started - args.warmup_seconds
            good = [s for s in samples if s['status'] == 200 and s['error'] is None]
            latencies = [s['latency_ms'] for s in good]
            server = [s['server_ms'] for s in good if s['server_ms'] is not None]
            statuses = {}
            for sample in samples:
                statuses[str(sample['status'])] = statuses.get(str(sample['status']), 0) + 1
                raw.write(json.dumps(sample) + '\n')
            raw.flush()
            stage = {'concurrency': concurrency, 'started_epoch': stage_started_epoch,
                     'ended_epoch': time.time(), 'requests': len(samples), 'errors': len(samples) - len(good),
                     'elapsed_seconds': round(elapsed, 2), 'successful_rps': round(len(good) / elapsed, 2),
                     'p50_ms': percentile(latencies, .50), 'p95_ms': percentile(latencies, .95),
                     'p99_ms': percentile(latencies, .99),
                     'ttfb_p50_ms': percentile([s['ttfb_ms'] for s in good], .50),
                     'ttfb_p95_ms': percentile([s['ttfb_ms'] for s in good], .95),
                     'server_p50_ms': percentile(server, .50),
                     'statuses': statuses,
                     'queue_p95_ms': percentile([s['queue_ms'] for s in good if s['queue_ms'] is not None], .95),
                     'inference_p50_ms': percentile([s['inference_ms'] for s in good if s['inference_ms'] is not None], .5),
                     'batch_size_mean': round(sum(s['batch_size'] or 1 for s in good) / max(len(good), 1), 2),
                     'input_tokens_min': min((s['input_tokens'] for s in good if s['input_tokens']), default=None),
                     'input_tokens_max': max((s['input_tokens'] for s in good if s['input_tokens']), default=None)}
            report['stages'].append(stage)
            (directory / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
            print(' '.join(str(stage[k]) for k in ['concurrency', 'requests', 'errors', 'successful_rps',
                                                  'p50_ms', 'p95_ms', 'p99_ms', 'ttfb_p50_ms', 'server_p50_ms', 'batch_size_mean', 'queue_p95_ms']), flush=True)
            if stage['errors'] > len(samples) * args.max_error_rate:
                print(f'Stopping: more than {args.max_error_rate:.0%} failed requests.', flush=True)
                break
            time.sleep(2)
    print(f'Report: {directory / "summary.json"}', flush=True)

if __name__ == '__main__':
    main()
