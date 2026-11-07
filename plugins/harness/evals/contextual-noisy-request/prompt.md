---
max_turns: 60
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit]
tags: [build]
---
来週チームで ISUCON の練習会をやります。前回は私が張り付いて Claude に指示を出し続けていて、正直しんどかったです。今回は HOOTL（人が一切ループに入らない）でやってみたいと思っています。
このリポジトリの負荷試験は ./bench.sh で、`score: <数値>` を出します。採点は最後の 1 回、競技時間は 6 時間です。ちなみに app/ は Python で、DB は使っていません。
練習会の当日までに、起動したら放っておけるように準備しておいてもらえますか。
