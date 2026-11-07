#!/usr/bin/env bash
# 評価スクリプト。harness.py check がプロジェクトのルートで実行する。
# 終了コード 0 で合格。score を測る課題では最後に `SCORE=<数値>` を 1 行出力する。
# run.sh から起動したエージェントはこのファイルを変更できない（harness/settings.json の deny）。
set -euo pipefail

echo "harness/check.sh がまだ実装されていません。" >&2
exit 2
