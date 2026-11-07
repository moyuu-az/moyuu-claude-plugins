#!/usr/bin/env bash
set -euo pipefail
cat > service.py <<'PY'
import time
def query():
    time.sleep(0.05)
PY
cat > latency.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
python3 -c 'import time,service; xs=[]
for _ in range(10):
    t=time.time(); service.query(); xs.append((time.time()-t)*1000)
xs.sort(); print(f"p99_ms={xs[-1]:.1f}")'
SH
chmod +x latency.sh
git init -q && git add -A && git -c user.email=eval@example.com -c user.name=eval commit -qm init
