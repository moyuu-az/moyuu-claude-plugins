---
max_turns: 60
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit]
tags: [build]
---
service.py の p99 レイテンシを、Claude に放置で下げ続けてもらう自律実行の環境を作ってほしいです。計測は ./latency.sh で、`p99_ms=<ミリ秒>` を出力します。`query()` が 42 を返すことは tests/test_service.py が確かめていて、この振る舞いは変えてはいけません。制限時間は 12 時間で、それまでに出た一番良い結果を採用します。
