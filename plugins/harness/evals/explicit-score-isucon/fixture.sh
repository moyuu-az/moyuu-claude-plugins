#!/usr/bin/env bash
set -euo pipefail
mkdir -p app
cat > app/server.py <<'PY'
import time
def handle(n):
    time.sleep(0.01 * n)  # deliberately slow
    return n
PY
cat > bench.sh <<'SH'
#!/usr/bin/env bash
# 負荷試験。最後に score を 1 行出す
set -euo pipefail
python3 -c 'import time,sys; sys.path.insert(0,"app"); import server; t=time.time(); [server.handle(1) for _ in range(20)]; print("score:", int(1000/(time.time()-t)))'
SH
chmod +x bench.sh
git init -q && git add -A && git -c user.email=eval@example.com -c user.name=eval commit -qm init
