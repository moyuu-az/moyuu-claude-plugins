#!/usr/bin/env bash
# 無人実行の起動スクリプト。プロジェクトのどこから呼んでもよい。
#   harness/run.sh                  ヘッドレスで実行し、イベントを harness/state/runs/ に保存する
#   HARNESS_INTERACTIVE=1 harness/run.sh   対話画面で実行する（様子を見たいとき。入力はしない）
# 環境変数:
#   HARNESS_PERMISSION_MODE  既定 auto。使い捨てのコンテナなら bypassPermissions も選べる
#   HARNESS_MAX_BUDGET_USD   ヘッドレス時の費用の上限。既定 20
#   HARNESS_MODEL            省略時は Claude Code の既定モデル
set -euo pipefail
cd "$(dirname "$0")/.."

python3 harness/harness.py start

# hook と deny は harness/settings.json にだけ置く。--settings で渡すので、この起動以外のセッションには効かない。
args=(--settings harness/settings.json
      --append-system-prompt "$(cat harness/SYSTEM.md)"
      --permission-mode "${HARNESS_PERMISSION_MODE:-auto}")
if [[ -n "${HARNESS_MODEL:-}" ]]; then args+=(--model "$HARNESS_MODEL"); fi
prompt="$(cat harness/PROMPT.md)"

if [[ -n "${HARNESS_INTERACTIVE:-}" ]]; then
  exec claude "${args[@]}" "$prompt"
fi

mkdir -p harness/state/runs
log="harness/state/runs/$(date +%Y%m%d-%H%M%S).jsonl"
echo "[harness] streaming events to $log"
rc=0
# --permission-prompts none: 確認が必要な操作は拒否され、エージェントには「承認する人はいないので再試行しない」と伝わる。
claude -p "$prompt" "${args[@]}" \
  --permission-prompts none \
  --max-budget-usd "${HARNESS_MAX_BUDGET_USD:-20}" \
  --output-format stream-json --verbose > "$log" || rc=$?
# 認証切れや予算超過で止まっても、どこまで進んだかは必ず表示する（status が最新の実行ログの結果も示す）。
python3 harness/harness.py status
exit "$rc"
