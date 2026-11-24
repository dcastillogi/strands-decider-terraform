import importlib.util
from pathlib import Path
from strands_decider.core import Request


def fixture_requests():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'verify-answers.py'
    spec = importlib.util.spec_from_file_location('verify_answers', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [Request.model_validate(body) for body in module.fixtures()]
