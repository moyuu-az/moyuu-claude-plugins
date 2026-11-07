---
type: regex
target:
  source: file
  path: harness/check.sh
pattern: '(?:^|\n)(?![ \t]*#)[^\n]*bench\.sh'
---
