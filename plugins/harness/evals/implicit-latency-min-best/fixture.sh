#!/usr/bin/env bash
set -euo pipefail
mkdir -p tests
cat > service.py <<'PY'
import time

def query():
    """Look up the answer. Must keep returning 42."""
    time.sleep(0.05)  # stands in for a slow lookup
    return 42
PY
cat > tests/test_service.py <<'PY'
import unittest
import service

class T(unittest.TestCase):
    def test_query_returns_answer(self):
        self.assertEqual(service.query(), 42)
PY
touch tests/__init__.py
cat > latency.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
python3 -c 'import time,service; xs=[]
for _ in range(100):
    t=time.time(); service.query(); xs.append((time.time()-t)*1000)
xs.sort(); print(f"p99_ms={xs[98]:.2f}")'
SH
chmod +x latency.sh
git init -q && git add -A && git -c user.email=eval@example.com -c user.name=eval commit -qm init
