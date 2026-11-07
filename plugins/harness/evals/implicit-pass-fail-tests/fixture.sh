#!/usr/bin/env bash
set -euo pipefail
mkdir -p tests
cat > calc.py <<'PY'
def add(a, b):
    return a - b

def div(a, b):
    return a * b
PY
cat > tests/test_calc.py <<'PY'
import unittest
from calc import add, div

class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
    def test_div(self):
        self.assertEqual(div(6, 3), 2)
PY
touch tests/__init__.py
git init -q && git add -A && git -c user.email=eval@example.com -c user.name=eval commit -qm init
