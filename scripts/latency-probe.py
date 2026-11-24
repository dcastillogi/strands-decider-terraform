#!/usr/bin/env python3
"""Unloaded latency by request shape: one request at a time, nothing else in flight.

A decision model generates no tokens: the whole answer arrives in one response, so time to
first byte (response headers) is the time to the first, and only, token of output. Each shape
is measured over a persistent connection and over a new TCP connection per request, which
adds the connection setup. Server fields split the server's time into queue and inference.
"""
import argparse
import http.client
import json
import time
from urllib.parse import urlsplit

from common import ROOT, add_target_arguments, percentile, resolve_target

TRIAGE = json.loads((ROOT / 'examples/triage.json').read_text())
LONG = json.loads((ROOT / 'examples/long-triage.json').read_text())


def shapes():
    noul = {'type': 'noul', 'instructions': 'Does this convey urgency?'}
    kinds = {
        'urgent': noul,
        'team': {'type': 'choice', 'instructions': 'Which team should handle this?',
                 'criteria': {'engineering': None, 'billing': None, 'sales': None, 'retail': None}},
        'mood': {'type': 'score', 'instructions': 'How frustrated is the writer?',
                 'criteria': ['calm', 'frustrated', 'very frustrated']},
    }
    many = {f'q{i:02}': {'type': 'noul', 'instructions': f'Does the message mention topic {i}?'}
            for i in range(30)}
    return {
        '1 noul, short state': {'state': TRIAGE['state'], 'questions': {'urgent': noul}},
        'triage example (2 questions)': TRIAGE,
        '3 kinds (noul, choice, score)': {'state': TRIAGE['state'], 'questions': kinds},
        '30 noul questions, one state': {'state': TRIAGE['state'], 'questions': many},
        'long state (~1.5k tokens), 2 questions': LONG,
    }


def measure(url, key, body, fresh, connection):
    if fresh or connection is None:
        if connection is not None:
            connection.close()
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=60)
    started = time.perf_counter()
    if fresh:
        connection.connect()
    connected = time.perf_counter()
    connection.request('POST', url.path, json.dumps(body),
                       {'x-api-key': key, 'content-type': 'application/json'})
    response = connection.getresponse()
    first_byte = time.perf_counter()
    data = response.read()
    ended = time.perf_counter()
    if response.status != 200:
        raise RuntimeError(f'HTTP {response.status}: {data[:200]!r}')
    result = json.loads(data)
    if set(result['answers']) != set(body['questions']):
        raise RuntimeError('answers do not match the questions')
    return connection, {
        'connect_ms': (connected - started) * 1000 if fresh else 0.0,
        'ttfb_ms': (first_byte - started) * 1000, 'total_ms': (ended - started) * 1000,
        'server_ms': result.get('latency_ms'), 'inference_ms': result.get('inference_ms'),
        'queue_ms': result.get('queue_ms'), 'input_tokens': result['usage']['input_tokens'],
    }


def summarize(samples):
    out = {'requests': len(samples), 'input_tokens': samples[0]['input_tokens']}
    for field in ['connect_ms', 'ttfb_ms', 'total_ms', 'server_ms', 'inference_ms', 'queue_ms']:
        values = [s[field] for s in samples if s[field] is not None]
        out[field] = {'p50': percentile(values, .5), 'p90': percentile(values, .9),
                      'p99': percentile(values, .99), 'min': round(min(values), 2) if values else None}
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--requests', type=int, default=100, help='Measured requests per shape and mode')
    p.add_argument('--warmup', type=int, default=10)
    p.add_argument('--output', default='deployment-results/latency-probe.json')
    add_target_arguments(p)
    args = p.parse_args()
    endpoint, key = resolve_target(args)
    url = urlsplit(endpoint)
    report = {'endpoint': endpoint, 'requests_per_shape': args.requests, 'shapes': {}}
    for name, template in shapes().items():
        report['shapes'][name] = {}
        for mode, fresh in [('persistent connection', False), ('new connection per request', True)]:
            connection, samples = None, []
            for index in range(args.warmup + args.requests):
                body = dict(template)
                # A unique state per request: no response could come from a cache.
                body['state'] = f"{template['state']} Probe {mode[:3]}-{index:05}."
                connection, sample = measure(url, key, body, fresh, connection)
                if index >= args.warmup:
                    samples.append(sample)
            connection.close()
            summary = summarize(samples)
            report['shapes'][name][mode] = summary
            print(f"{name:42} {mode:27} tokens={summary['input_tokens']:5} "
                  f"ttfb p50={summary['ttfb_ms']['p50']:8} p99={summary['ttfb_ms']['p99']:8} "
                  f"total p50={summary['total_ms']['p50']:8} server p50={summary['server_ms']['p50']:8} "
                  f"inference p50={summary['inference_ms']['p50']}", flush=True)
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Report: {output}', flush=True)


if __name__ == '__main__':
    main()
