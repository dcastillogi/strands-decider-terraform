#!/usr/bin/env python3
"""Wait for this Terraform SSM document version on this instance, then infer."""
import json
import os
import subprocess
import sys
import time


def aws(*args):
    return json.loads(subprocess.check_output(['aws', '--region', os.environ['REGION'],
        'ssm', *args], text=True))


def main():
    deadline = time.monotonic() + 2700
    association_id = os.environ['ASSOCIATION']
    version = os.environ['DOCUMENT_VERSION']
    while time.monotonic() < deadline:
        description = aws('describe-association', '--association-id', association_id)['AssociationDescription']
        if description['DocumentVersion'] == version:
            association_version = description['AssociationVersion']
            executions = aws('describe-association-executions', '--association-id', association_id)['AssociationExecutions']
            relevant = [e for e in executions if e['AssociationVersion'] == association_version]
            if relevant:
                latest = max(relevant, key=lambda e: e['CreatedTime'])
                targets = aws('describe-association-execution-targets', '--association-id', association_id,
                              '--execution-id', latest['ExecutionId'])['AssociationExecutionTargets']
                target = next((t for t in targets if t['ResourceId'] == os.environ['INSTANCE']), None)
                if target and target['Status'] in ['Failed', 'TimedOut', 'Cancelled']:
                    print('Runtime deployment failed:', target['DetailedStatus'], file=sys.stderr)
                    command_id = target.get('OutputSource', {}).get('OutputSourceId')
                    if command_id:
                        invocation = aws('get-command-invocation', '--command-id', command_id,
                                         '--instance-id', os.environ['INSTANCE'])
                        print(invocation.get('StandardErrorContent', '')[-4000:], file=sys.stderr)
                        print(invocation.get('StandardOutputContent', '')[-4000:], file=sys.stderr)
                    return 1
                if target and target['Status'] == 'Success':
                    result = subprocess.run([os.path.join(os.path.dirname(__file__), 'smoke-test.sh'),
                        os.environ['ENDPOINT'], os.environ['SECRET'], os.environ['REGION']])
                    if result.returncode == 0:
                        return 0
        time.sleep(20)
    print('Runtime deployment did not become ready within 45 minutes', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
