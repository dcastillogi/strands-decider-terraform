#!/usr/bin/env bash
set -euo pipefail
export UV_PYTHON_INSTALL_DIR=/opt/decider/python
# Strands Decider v2 (Gemma 4) runs on SDPA: no CUDA extensions are compiled, so the
# DLAMI's toolkit is not needed. The PyPI torch wheel bundles its own CUDA runtime.
if [ ! -x /opt/decider/.venv/bin/python ]; then
  uv venv --python 3.11 /opt/decider/.venv
fi
uv pip install --python /opt/decider/.venv/bin/python -r "$(dirname "$0")/requirements.txt"
/opt/decider/.venv/bin/python - <<'CHECK'
import torch, transformers, strands_decider
assert torch.cuda.is_available(), 'CUDA is not available to torch'
print('torch', torch.__version__, 'cuda', torch.version.cuda, 'transformers', transformers.__version__,
      'strands-decider', strands_decider.__version__, torch.cuda.get_device_name(0))
CHECK
