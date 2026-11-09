# ハーネスを直す（improve）

本番で走らせる前に、同じ種類の課題を評価環境で何度も解かせ、失敗のたびにハーネスを直す。ISUCON2026 の優勝者は、評価環境で 100 回以上解かせてハーネスを改善した。

## 1. 実行の記録を集める

`harness/state/archive/<日時>/` が 1 回分の実行で、`harness/state/` が最新の実行である。

- `python3 harness/harness.py report`: 実行ごとの check の回数、合格数、最良と最後の score の一覧
- `scores.jsonl`: check の全履歴。`checks/` に出力の全文
- `events.jsonl`: hook が知らせた時刻（伸び悩み、最終確認）
- `runs/*.jsonl`: `claude -p` のイベント。ツール呼び出し、hook の反応、費用が入っている
- `harness/decisions.md` と git log: エージェントの判断と、実際の変更

## 2. 失敗を分類する

記録を読み、うまくいかなかった実行ごとに、何が足りなかったかを次のどれかに分ける。

| 足りなかったもの | 直す場所 |
| --- | --- |
| エージェントが知りえなかった事実（環境、規約、過去の経緯） | CONDITIONS.md |
| 見落としやすい観点（比べなかった案、確かめなかった前提） | checklist.md |
| 必ず守らせたい手順や禁止（評価系の改ざん、危険な操作） | settings.json の deny、check.sh |
| 知らせる時機（伸び悩みの判定が遅い・早い、最終確認の時間） | harness.json の `stall_minutes`、`final_check_minutes` |
| 止める時機（早すぎる終了、締め切りの超過、同じ label の回数が増えずに止まらない） | harness.json の `min_samples`、`safety_margin_minutes`、`run_seconds`、`label_ignore` |
| 評価そのもの（ばらつきが大きい、抜け道がある） | check.sh |

SYSTEM.md への追記は最後の手段にする。プロンプトの規則は守られないことがあり、増えるほどほかの規則も守られにくくなる。

## 3. 1 か所ずつ直して、もう一度走らせる

一度に直すのは 1 か所にし、同じ課題でもう一度走らせて効果を確かめる。結果がばらつく課題では、1 回の比較で判断しない。

## 4. 効いていない部品を外す

モデルが良くなると、以前は必要だった部品が要らなくなる。checklist.md の項目、SYSTEM.md の規則、hook の知らせを 1 つずつ外して走らせ、結果が変わらなければ外したままにする。まとめて外すと、どれが効いていたのかが分からなくなる。
