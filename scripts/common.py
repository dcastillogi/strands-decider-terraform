"""Endpoint and API key resolution shared by the test scripts.

Order: command-line flags, then environment (DECIDER_ENDPOINT, DECIDER_API_KEY or
DECIDER_API_KEY_SECRET_ARN), then `terraform output`. AWS credentials come from the standard
chain (AWS_PROFILE, environment, instance role); the region comes from the secret's ARN.
"""
import json
import math
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def add_target_arguments(parser):
    parser.add_argument('--endpoint', default=os.environ.get('DECIDER_ENDPOINT'),
                        help='POST URL of /v1/systemone (default: terraform output endpoint)')
    parser.add_argument('--secret-arn', default=os.environ.get('DECIDER_API_KEY_SECRET_ARN'),
                        help='Secrets Manager ARN of the API key (default: terraform output)')


def _terraform_output(name):
    return json.loads(subprocess.check_output(['terraform', 'output', '-json', name], cwd=ROOT))


def resolve_target(args):
    """(endpoint, api_key) for the parsed arguments."""
    endpoint = args.endpoint or _terraform_output('endpoint')
    key = os.environ.get('DECIDER_API_KEY')
    if not key:
        secret = args.secret_arn or _terraform_output('api_key_secret_arn')
        key = json.loads(subprocess.check_output([
            'aws', '--region', secret.split(':')[3], 'secretsmanager', 'get-secret-value',
            '--secret-id', secret, '--query', 'SecretString', '--output', 'text'], text=True))['key']
    return endpoint, key


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(p * len(ordered)) - 1)], 2)
