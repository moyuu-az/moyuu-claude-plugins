---
max_turns: 60
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit]
tags: [build]
---
service.py の p99 レイテンシを、Claude に放置で下げ続けてもらう自律実行の環境を作ってほしいです。計測は ./latency.sh で、`p99_ms=<ミリ秒>` を出力します。締め切りは 2026-11-08T09:00:00+09:00 で、それまでに出た一番良い結果を採用します。
