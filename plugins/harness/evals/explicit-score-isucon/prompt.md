---
max_turns: 60
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit]
tags: [build]
---
/harness:build このリポジトリを、ISUCON のように Claude に無人でスコアを上げさせたいです。負荷試験は ./bench.sh で、最後の行に `score: <数値>` を出します。採点されるのは最後に走らせた 1 回で、制限時間は 8 時間です。app/ 以下は自由に変更してよいですが、bench.sh は変更させないでください。
