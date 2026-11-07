#!/usr/bin/env bash
set -euo pipefail
cat > calc.py <<'PY'
def add(a, b):
    return a - b
PY
cat > test_calc.py <<'PY'
import unittest
from calc import add
class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
PY
git init -q && git add -A && git -c user.email=eval@example.com -c user.name=eval commit -qm init
