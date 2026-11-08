#!/usr/bin/env bash
# The plugin under audit, shared by every case. Its suite has four planted faults:
#   weekly/skill-fired   the prompt starts with /myplug:report, so no Skill call is recorded
#   weekly/has-result    '## 結果' is already in the template, so copying it unfilled passes
#   weekly/prompt.md     an absolute date
#   off-topic/never      a "never invoke" Skill grader beside another grader, without arm: both
set -euo pipefail
p=myplug
mkdir -p $p/.claude-plugin $p/skills/report/templates $p/evals/weekly/graders $p/evals/off-topic/graders
echo '{"name":"myplug","version":"0.1.0"}' > $p/.claude-plugin/plugin.json
cat > $p/skills/report/SKILL.md <<'EOF'
---
name: report
description: 週次レポートを作る。「週次レポートを作って」などの依頼で使う。
---
templates/report.md を作業ディレクトリの report.md に写し、今週の結果を「## 結果」の下に書く。
EOF
printf '# 週次レポート\n\n## 結果\n\nTODO\n' > $p/skills/report/templates/report.md
cat > $p/evals/weekly/prompt.md <<'EOF'
---
max_turns: 30
allowed_tools: [Read, Write, Skill]
---
/myplug:report 2026-12-01 締めの週次レポートを作って。今週はログイン画面の不具合を 3 件直しました。
EOF
cat > $p/evals/weekly/graders/skill-fired.md <<'EOF'
---
type: tool_used
tool: Skill
input_match: '"skill"\s*:\s*"(?:[\w-]+:)?report"'
---
EOF
cat > $p/evals/weekly/graders/has-result.md <<'EOF'
---
type: regex
target:
  source: file
  path: report.md
pattern: '## 結果'
---
EOF
cat > $p/evals/off-topic/prompt.md <<'EOF'
---
max_turns: 20
allowed_tools: [Read, Edit, Skill]
---
README.md の「インストル」という誤字を直してください。
EOF
cat > $p/evals/off-topic/graders/never.md <<'EOF'
---
type: tool_used
tool: Skill
input_match: '"skill"\s*:\s*"(?:[\w-]+:)?report"'
min: 0
max: 0
---
EOF
cat > $p/evals/off-topic/graders/fixed.md <<'EOF'
---
type: regex
target:
  source: file
  path: README.md
pattern: 'インストール'
---
EOF
