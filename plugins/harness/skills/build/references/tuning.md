# 課題の種類ごとの設定

## スコアを上げる課題（ISUCON、性能改善、モデルの精度）

```json
{ "mode": "score", "direction": "max", "final_counts": "last",
  "time_limit_minutes": 480, "min_samples": 3, "stall_minutes": 30, "final_check_minutes": 30 }
```

- check.sh はベンチマークを実行し、最後に `SCORE=<数値>` を出す。ベンチマークが失敗した回は終了コードを 0 以外にする。失敗した回の数値は停止判定に使われない。
- 採点が最後の 1 回なら `final_counts: "last"`。Stop hook は、残り時間から走らせられる回数を数え、続けたときの期待値より直近の結果が良ければ止める。最良の回が残る課題なら `"best"` にし、締め切りまで続けさせる。
- 1 回の所要時間は記録から中央値を取る。初回から正しく見積もりたいときは `run_seconds` に秒数を入れる。
- 締め切りの数分前は再起動や片付けに使うので、`safety_margin_minutes` で実行回数の計算から外す。
- レイテンシのように小さいほど良い値は `direction: "min"`。

## テストやビルドを通す課題（機能の実装、バグの修正、移行作業）

```json
{ "mode": "pass_fail", "stop_runs_check": true, "stall_minutes": 45, "time_limit_minutes": 240 }
```

- check.sh は型検査、lint、テストを順に実行し、どれかが失敗したら 0 以外で終わる。
- Stop hook は、終了しようとするたびに check を実行し、失敗の出力をエージェントに返す。check が長い場合は `harness/settings.json` の Stop の `timeout` と `check_timeout_seconds` をそろえる。
- バグの修正なら、CONDITIONS.md に再現の手順を書き、check.sh に再現テストを含める。
- エージェントがテストを書き換えて通すのを防ぐため、既存のテストは deny で守る（`Edit(./tests/**)` など）。新しいテストを書かせたい課題では、守るのは既存のテストの場所だけにして、新しいテストは別のディレクトリに置かせる。

## 大きな機能群を複数セッションで作る

Anthropic の長時間エージェントの構成を、このハーネスに載せる。

- 最初に、機能を 1 件ずつ `harness/features.json` に書き出す（`id`、`description`、確かめる手順の `steps`、`passes`）。最初はすべて `"passes": false`。
- check.sh は、各機能の確かめる手順を自動テストとして実行し、すべて通れば 0 で終わる。`SCORE=<通った件数>` を出せば、伸び悩みの知らせにも使える。
- features.json の `passes` 以外をエージェントに変えさせないため、check.sh の中で、初期版（`harness/features.lock.json` として deny で守る）と `id`・`description`・`steps` が一致するかを確かめ、一致しなければ失敗にする。
- SYSTEM.md に「1 回に 1 機能だけ進め、終わるたびに commit して decisions.md に引き継ぎを書く」を足す。
- 一晩かけるなら `time_limit_minutes` と `HARNESS_MAX_BUDGET_USD` を大きくし、`final_check_minutes` は 30 分程度にする。

## ほかのマシンで走らせる

- ハーネスは対象プロジェクトの中で完結し、plugin がなくても動く。対象のホストに Python 3.9 以上と Claude Code があればよい。
- 対話画面を見ながら走らせるなら、tmux などの中で `HARNESS_INTERACTIVE=1 harness/run.sh` を実行する。
- ブラウザの操作が必要な課題（ポータルへのログインなど）は、起動前に人がログインまで済ませ、その状態をエージェントが使えるようにしておく。ログインの操作はハーネスに含めない。
