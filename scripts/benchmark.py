#!/usr/bin/env python3
"""Run a test script on the in-region load generator, against the private endpoint.

Requires `load_generator = true`. The scripts and examples travel to the load generator
through the results bucket; it runs the tool with python3.11 and uploads its results, which
are extracted under deployment-results/runs/<run id>/. With --telemetry, the decider instance
samples CPU, GPU, memory and power during the run, and each load-test stage gets a summary.

Examples:
  python3 scripts/benchmark.py latency-probe
  python3 scripts/benchmark.py load-test --telemetry -- --concurrency 1,8,16,32,64 --seconds 60
  python3 scripts/benchmark.py overload-test -- --concurrency 384
"""
import argparse
import base64
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ['load-test', 'latency-probe', 'overload-test', 'verify-answers']


class Remote:
    def __init__(self, region):
        self.aws = ['aws', '--region', region]

    def run(self, instance, script, timeout=600, quiet=False):
        command = json.loads(subprocess.check_output(self.aws + [
            'ssm', 'send-command', '--instance-ids', instance, '--document-name', 'AWS-RunShellScript',
            '--parameters', json.dumps({'commands': [script], 'executionTimeout': [str(timeout)]})]))
        command_id = command['Command']['CommandId']
        deadline = time.monotonic() + timeout + 120
        while time.monotonic() < deadline:
            time.sleep(3)
            result = subprocess.run(self.aws + ['ssm', 'get-command-invocation', '--command-id', command_id,
                                                '--instance-id', instance], capture_output=True, text=True)
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            if data['Status'] in ('Pending', 'InProgress', 'Delayed'):
                continue
            if not quiet or data['Status'] != 'Success':
                print(data['StandardOutputContent'], end='', flush=True)
                print(data['StandardErrorContent'], end='', flush=True)
            if data['Status'] != 'Success':
                raise RuntimeError(f'{instance}: command {data["Status"]}')
            return data['StandardOutputContent'].strip()
        raise TimeoutError(command_id)


def bundle(include):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for path in ['scripts', 'examples', *include]:
            tar.add(ROOT / path, arcname=path,
                    filter=lambda info: None if '__pycache__' in info.name else info)
    return buffer.getvalue()


def stage_resources(report, rows):
    for stage in report.get('stages', []):
        points = [r for r in rows if stage['started_epoch'] <= r['epoch'] <= stage['ended_epoch']]
        if not points:
            continue
        average = lambda f: round(sum(f(r) for r in points) / len(points), 1)
        stage['resources'] = {
            'samples': len(points), 'gpu_avg_pct': average(lambda r: r['gpu_util_pct']),
            'gpu_max_pct': max(r['gpu_util_pct'] for r in points),
            'cpu_avg_pct': average(lambda r: r['cpu_busy_pct']['cpu']),
            'process_cpu_avg_pct': average(lambda r: r['process_cpu_pct']),
            'vram_max_mib': max(r['vram_mib'] for r in points),
            'ram_available_min_mib': min(r['ram_available_mib'] for r in points),
            'gpu_power_avg_w': average(lambda r: r['gpu_power_w']),
        }
        print(stage['concurrency'], stage['resources'], flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('tool', choices=TOOLS)
    p.add_argument('tool_args', nargs=argparse.REMAINDER, help='Arguments for the tool, after --')
    p.add_argument('--telemetry', action='store_true', help='Sample decider resources during the run')
    p.add_argument('--include', action='append', default=[],
                   help='Extra repository path to send (e.g. deployment-results/baseline-answers.json)')
    p.add_argument('--timeout', type=int, default=7200)
    args = p.parse_args()
    tool_args = args.tool_args[1:] if args.tool_args[:1] == ['--'] else args.tool_args

    outputs = {k: v['value'] for k, v in json.loads(subprocess.check_output(
        ['terraform', 'output', '-json'], cwd=ROOT)).items()}
    loadgen, bucket = outputs.get('load_generator_instance_id'), outputs.get('results_bucket')
    if not loadgen or not bucket:
        raise SystemExit('Set load_generator = true and apply first.')
    secret, decider = outputs['api_key_secret_arn'], outputs['instance_id']
    remote = Remote(secret.split(':')[3])
    run_id = time.strftime('%Y%m%d-%H%M%S-') + args.tool + '-' + uuid.uuid4().hex[:6]
    prefix = f's3://{bucket}/runs/{run_id}'
    destination = ROOT / 'deployment-results' / 'runs' / run_id
    destination.mkdir(parents=True)

    with tempfile.NamedTemporaryFile(suffix='.tgz') as f:
        f.write(bundle(args.include))
        f.flush()
        subprocess.run(remote.aws + ['s3', 'cp', '--quiet', f.name, f'{prefix}/bundle.tgz'], check=True)

    telemetry = None
    if args.telemetry:
        payload = base64.b64encode((ROOT / 'scripts/monitor-resources.py').read_bytes()).decode()
        telemetry = f'/tmp/decider-telemetry-{run_id}'
        pid = remote.run(decider, f'printf %s {shlex.quote(payload)} | base64 -d > {telemetry}.py\n'
                         f'nohup python3 {telemetry}.py {telemetry}.jsonl {args.timeout} > {telemetry}.log '
                         f'2>&1 < /dev/null &\necho $!', quiet=True)
    health = remote.run(loadgen, f'curl -fsS --max-time 5 {outputs["private_endpoint"].rsplit("/v1/", 1)[0]}/healthz',
                        quiet=True)
    print(f'Run {run_id} on {loadgen} -> {outputs["private_endpoint"]} (envoy: {health})', flush=True)
    command = shlex.join(['python3.11', f'scripts/{args.tool}.py', *tool_args])
    script = f'''set -uo pipefail
WORK=$(mktemp -d) && cd "$WORK"
aws s3 cp --quiet {prefix}/bundle.tgz - | tar xz || exit 1
export DECIDER_ENDPOINT={shlex.quote(outputs["private_endpoint"])}
export DECIDER_API_KEY_SECRET_ARN={shlex.quote(secret)}
ulimit -n 65536
{command} > run.log 2>&1
status=$?
mkdir -p deployment-results && tar czf results.tgz deployment-results run.log
aws s3 cp --quiet results.tgz {prefix}/results.tgz
tail -c 20000 run.log
rm -rf "$WORK"
exit $status'''
    try:
        remote.run(loadgen, script, timeout=args.timeout)
    finally:
        with tempfile.NamedTemporaryFile(suffix='.tgz') as f:
            if subprocess.run(remote.aws + ['s3', 'cp', '--quiet', f'{prefix}/results.tgz', f.name]).returncode == 0:
                with tarfile.open(f.name) as tar:
                    tar.extractall(destination, filter='data')
        if telemetry:
            remote.run(decider, f'kill {int(pid)} 2>/dev/null || true; gzip -c {telemetry}.jsonl | '
                       f'aws s3 cp --quiet - {prefix.replace("/runs/", "/telemetry/")}.jsonl.gz', quiet=True)
            with tempfile.NamedTemporaryFile(suffix='.gz') as f:
                subprocess.run(remote.aws + ['s3', 'cp', '--quiet',
                    f'{prefix.replace("/runs/", "/telemetry/")}.jsonl.gz', f.name], check=True)
                import gzip
                raw = gzip.decompress(Path(f.name).read_bytes())
            (destination / 'resources.jsonl').write_bytes(raw)
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            for summary in destination.glob('deployment-results/load-test/*/summary.json'):
                report = json.loads(summary.read_text())
                stage_resources(report, rows)
                summary.write_text(json.dumps(report, indent=2) + '\n')
        print(f'Results: {destination}', flush=True)


if __name__ == '__main__':
    main()
