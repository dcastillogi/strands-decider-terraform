#!/usr/bin/env python3
"""Send one bounded burst, verify 503 backpressure and subsequent recovery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import threading
import time
from urllib.parse import urlsplit

from common import ROOT, add_target_arguments, resolve_target

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--concurrency', type=int, default=384)
    p.add_argument('--output', default='deployment-results/overload.json')
    p.add_argument('--fresh-connections', action='store_true',
                   help='Include a simultaneous connection burst instead of warming connections first')
    add_target_arguments(p)
    args = p.parse_args()
    endpoint, key = resolve_target(args)
    url = urlsplit(endpoint)
    template = json.loads((ROOT / 'examples/triage.json').read_text())
    barrier = threading.Barrier(args.concurrency + 1)

    def connect(_):
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=35)
        connection.request('GET', '/healthz')
        response = connection.getresponse()
        response.read()
        assert response.status == 200
        return connection

    connections = []
    if not args.fresh_connections:
        with ThreadPoolExecutor(max_workers=16) as pool:
            connections = list(pool.map(connect, range(args.concurrency)))

    def call(index, synchronize=True):
        body = dict(template)
        body['state'] += f' Burst ticket {index}.'
        connection = (connections[index] if synchronize and connections else
                      http.client.HTTPConnection(url.hostname, url.port, timeout=35))
        if synchronize:
            barrier.wait()
        start = time.perf_counter()
        try:
            connection.request('POST', url.path, json.dumps(body),
                               {'x-api-key': key, 'content-type': 'application/json'})
            response = connection.getresponse()
            data = json.loads(response.read())
            if response.status == 200:
                assert set(data['answers']) == set(template['questions'])
            elif response.status == 503:
                assert response.getheader('Retry-After') == '1'
            else:
                raise AssertionError(f'unexpected HTTP {response.status}')
            return {'status': response.status, 'latency_ms': round((time.perf_counter() - start) * 1000, 2)}
        except Exception as exc:
            return {'status': 0, 'error': str(exc),
                    'latency_ms': round((time.perf_counter() - start) * 1000, 2)}
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(call, index) for index in range(args.concurrency)]
        barrier.wait()
        results = [future.result() for future in futures]
    statuses = {str(status): sum(r['status'] == status for r in results) for status in [200, 503, 0]}
    recovery = call('recovery', synchronize=False)
    passed = recovery['status'] == 200 and statuses['503'] > 0 and statuses['0'] == 0
    report = {'concurrency': args.concurrency, 'fresh_connections': args.fresh_connections,
              'statuses': statuses, 'recovery': recovery, 'passed': passed, 'requests': results}
    (ROOT / args.output).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / args.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'requests'}), flush=True)
    if not passed:
        raise SystemExit('Overload/recovery check failed; see saved results')

if __name__ == '__main__':
    main()
