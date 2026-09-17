# SPDX-License-Identifier: Apache-2.0
# Executed by the declared Common Base Bash; no shebang location assumed.
set -euo pipefail
: "${BPCE_PREFIX:?}"
: "${BPCE_BASE_PYTHON:?}"
"$BPCE_BASE_PYTHON" - <<'PYTHON'
import os
from pathlib import Path
out = Path(os.environ['BPCE_PREFIX']) / 'bin' / 'bpce-demo-tool'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("printf '%s\\n' 'bpce-demo-tool-ok'\n")
out.chmod(0o755)
PYTHON
