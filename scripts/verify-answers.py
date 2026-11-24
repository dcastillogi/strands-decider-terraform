#!/usr/bin/env python3
"""Capture/verify model outputs across distinct inputs and concurrent requests."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import urllib.request

from common import ROOT, add_target_arguments, resolve_target

def fixtures():
    states = ['The build passed all tests.', 'The build failed and production is down.',
              'Help! My payouts have been failing for 3 days!',
              'Please tell me the pricing for your enterprise plan.',
              'Mi factura tiene un cobro duplicado. Necesito un reembolso.',
              'The customer is calm and happy with the service.',
              'Daily report: all systems operational. ' * 100 + 'However, the payment service is now failing.',
              'A shop customer wants to return a damaged product.']
    return [{'state': state, 'questions': {
        'urgent': {'type': 'noul', 'instructions': 'Does this convey urgency?'},
        'team': {'type': 'choice', 'instructions': 'Which team should handle this?',
                 'criteria': dict.fromkeys(['engineering', 'billing', 'sales', 'retail'][:2 + i % 3], '')},
        'mood': {'type': 'score', 'instructions': 'How frustrated is the writer?',
                 'criteria': ['calm', 'frustrated', 'very frustrated']},
    }} for i, state in enumerate(states)]

def numeric(value, prefix=''):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            result.update(numeric(item, prefix + '/' + key))
    elif isinstance(value, (int, float)):
        result[prefix] = value
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['capture', 'compare'])
    parser.add_argument('--baseline', default='deployment-results/baseline-answers.json')
    parser.add_argument('--output', default='deployment-results/answer-verification.json')
    add_target_arguments(parser)
    args = parser.parse_args()
    endpoint, key = resolve_target(args)
    def call(body):
        request = urllib.request.Request(endpoint, json.dumps(body).encode(),
            {'Content-Type': 'application/json', 'x-api-key': key})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    bodies = fixtures()
    baseline_file = ROOT / args.baseline
    baseline_file.parent.mkdir(parents=True, exist_ok=True)
    if args.mode == 'capture':
        values = [call(body) for body in bodies]
        baseline_file.write_text(json.dumps({'requests': bodies, 'responses': values}, indent=2) + '\n')
        print('Captured', len(values), 'responses')
        return
    baseline = json.loads(baseline_file.read_text())
    outcomes, failures = [], []
    for concurrency in [1, 8, 32]:
        pairs = list(enumerate(baseline['requests'])) * (4 if concurrency == 32 else 1)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            responses = list(pool.map(lambda pair: (pair[0], call(pair[1])), pairs))
        maximum = 0
        for index, actual in responses:
            expected = baseline['responses'][index]
            assert actual['model'] == expected['model']
            assert set(actual['answers']) == set(expected['answers'])
            for name, answer in actual['answers'].items():
                old = expected['answers'][name]
                assert answer['type'] == old['type']
                if answer['type'] == 'choice':
                    if answer['choice'] != old['choice']:
                        failures.append({'concurrency': concurrency, 'index': index, 'question': name,
                                         'reason': 'choice changed', 'expected': old, 'actual': answer})
                before, after = numeric(old), numeric(answer)
                assert before.keys() == after.keys()
                difference = max((abs(after[k] - before[k]) for k in before), default=0)
                maximum = max(maximum, difference)
                if difference > .02:
                    failures.append({'concurrency': concurrency, 'index': index, 'question': name,
                                     'difference': difference, 'expected': old, 'actual': answer})
        outcomes.append({'concurrency': concurrency, 'requests': len(responses),
                         'maximum_numeric_difference': round(maximum, 6),
                         'passed': not any(f['concurrency'] == concurrency for f in failures)})
        print(outcomes[-1], flush=True)
    (ROOT / args.output).write_text(json.dumps({'outcomes': outcomes, 'failures': failures}, indent=2) + '\n')
    if failures:
        raise SystemExit(f'{len(failures)} comparisons failed; details in {args.output}')

if __name__ == '__main__':
    main()
