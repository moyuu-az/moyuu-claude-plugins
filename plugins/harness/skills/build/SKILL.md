---
name: build
description: Claude Code を人の介入なしで最後まで走らせるためのハーネスを、対象プロジェクトに作る。評価スクリプト、終了を判定する Stop hook、伸び悩みと締め切りを知らせる hook、判断を見直す監査役のサブエージェント、実行条件のファイル、起動スクリプトをそろえる。「ハーネスを作って」「放置で回したい」「無人で最適化させたい」「ISUCON のように自律で解かせたい」「HOOTL」などの依頼で使う。作ったハーネスを過去の実行結果から直すとき（improve）にも使う。
argument-hint: "[build|improve] [対象ディレクトリ]"
---

# 無人実行のハーネスを作る

対象プロジェクトに `harness/` を置き、`harness/run.sh` を 1 回実行すれば、人が入力しなくても課題を最後まで進められる状態にする。
引数の 1 つ目が `improve` なら [references/improve.md](references/improve.md) の手順に進む。それ以外は下の build の手順で進める。対象ディレクトリの指定がなければ、作業ディレクトリを対象にする。

## 作るもの

| ファイル | 役割 |
| --- | --- |
| `harness/check.sh` | 評価。終了コード 0 で合格、スコアを測る課題では `SCORE=<数値>` を出力する |
| `harness/harness.py` | check の実行と記録、停止判定、hook の本体（標準ライブラリだけで動く） |
| `harness/harness.json` | 課題の種類、締め切り、伸び悩みとみなす時間などの設定 |
| `harness/settings.json` | hook と deny の設定。`run.sh` が `--settings` で渡すので、ほかのセッションには効かない |
| `harness/CONDITIONS.md` | 実行ごとに変わる条件。初期プロンプトはこのファイルを読ませる 1 行だけ |
| `harness/SYSTEM.md` | 無人で進めるときの作業規則。システムプロンプトに追加する |
| `harness/checklist.md` | 監査役が判断を突き合わせるチェック項目 |
| `harness/decisions.md` | エージェントが判断を書き残す記録 |
| `harness/agents.json` | 読み取り専用の監査役。`run.sh` が `--agents` で渡す |
| `harness/run.sh` | 起動スクリプト |

hook の働きは次のとおり。

- Stop: 終了条件を満たすまで終了させない。テストを通す課題では check を実行し、失敗の出力をエージェントに返す。スコアの課題では、締め切り、目標スコア、残りの実行回数から決める期待値の閾値で判定する。バックグラウンドの処理を待っているときは止めない。
- PostToolUse: 結果が `stall_minutes` 改善しなければ監査役の起動を促す。締め切りの `final_check_minutes` 前に最終確認を促す。どちらも 1 回ずつ知らせる。
- SessionStart: 残り時間と最良の結果を伝える。コンテキストの要約後と再開時は decisions.md の末尾も渡す。
- PreCompact: 要約に残すべきことを指示する。

## 設計の原則

根拠と出典は [references/research.md](references/research.md) にある。

1. 合否か数値を返す check を最初に作る。check を作れない課題は、ハーネスにしても無人では完了を判定できない。作れない理由をユーザーに伝えて止める。
2. 必ず守らせたい規則は hook と deny に置く。CLAUDE.md やプロンプトの指示は、モデルが従わない場合がある。
3. 作業するエージェントと評価する仕組みを分ける。評価系のファイルは deny で守り、監査役は読み取り専用にする。
4. 経過はファイルに残す。scores.jsonl、decisions.md、git の commit があれば、コンテキストが要約されても、監査役も、あとから改善する人も状況を追える。
5. 部品はどれも「モデルが単独ではできないこと」についての仮定である。評価環境で何度も走らせ、効いていない部品は外す。

## build の手順

### 1. 課題を把握する

対象のリポジトリを読み、`git status` と `git log --oneline -5` を確かめる。git 管理でなければ、`git init` と最初の commit をユーザーに提案する。commit が戻り先になるので、git なしでは進めない。

### 2. 決めることを決める

次の項目を、リポジトリと依頼文から決める。決められない項目だけを、AskUserQuestion でまとめて 1 回で聞く。実行が始まったら質問できないので、聞くのはこの段階だけにする。

| 項目 | harness.json | 決め方 |
| --- | --- | --- |
| 課題の種類 | `mode` | テストやビルドを通す課題は `pass_fail`、数値を上げ下げする課題は `score` |
| 評価の方法 | `check_command` | 既存のテスト、ベンチマーク、負荷試験。なければ check.sh に書く |
| 数値の向き | `direction` | 大きいほど良いなら `max`、レイテンシなどは `min` |
| 何が最終結果か | `final_counts` | `score` の課題だけで使う。最後の 1 回が採点される課題は `last`（ISUCON など）、最良の結果を残せる課題は `best` |
| 締め切り | `deadline` か `time_limit_minutes` | 絶対時刻なら deadline、起動からの時間なら time_limit_minutes |
| check が書き換えるファイル | `label_ignore` | `score` の課題で、check が git で管理しているファイル（DB のダンプ、生成物など）を書き換えるなら、そのパスを並べる。並べないと実行のたびにラベルが変わり、同じ構成の回数が `min_samples` に届かないので、最後まで止まらない。パスはプロジェクトのルートからの相対パスで、ディレクトリも指定できる。エージェントが変更して試すファイルは並べない。並べると、変更しても別の構成として数えられない |
| 目標 | `target_score` | 達したら止めてよい値。なければ null |
| 予算 | `HARNESS_MAX_BUDGET_USD` | run.sh の環境変数 |
| 触ってよい範囲 | CONDITIONS.md と settings.json の deny | 本番環境、外部への送信、テスト、生成物など |

課題の種類ごとの設定例は [references/tuning.md](references/tuning.md) にある。

### 3. テンプレートを置く

対象ディレクトリで実行する。既存のファイルは上書きしない（`cp -n`）。上書きされなかったファイルがあれば、中身を確かめてから手で合わせる。macOS の `cp -n` は、コピーしなかったファイルがあると何も表示せずに終了コード 1 を返す。どのファイルが既にあったかは、コピーの前に `ls -A harness` で確かめておく。ファイルはすべて `harness/` の中に置く。`.claude/` は保護されたディレクトリで、権限の設定によっては書き込めないためである。

```bash
cp -Rn "${CLAUDE_SKILL_DIR}/templates/harness" ./
```

### 4. 中身を埋める

- `harness/check.sh`: いちばん大事なファイル。同じコードなら同じ判定になるように書く。エージェントが変更できるファイルだけで合否が決まると、テストを書き換えて通すような抜け道ができる。判定に使うテストや期待値は、deny で守るか check.sh の中に置く。
- `harness/harness.json`: 手順 2 で決めた値を入れる。テンプレートの `mode` は null になっていて、決めずに残すと `harness.py` が動かない。`score` の課題では `final_counts` も同じで、`pass_fail` の課題では null のままでよい。
- `harness/CONDITIONS.md`: コードを読めば分かることは書かない。目的、完了の定義、締め切り、環境、禁止事項、コードからは分からない前提を書く。認証情報の値は書かず、置き場所だけを書く。
- `harness/checklist.md`: 「この課題に固有の項目」を、その分野で見落としやすい点で埋める。
- `harness/settings.json`: 課題に合わせて deny を足す。テストを通す課題なら、テストのディレクトリを `Edit(./tests/**)` のように守る。deny は Claude Code の組み込みツールと、cat・sed・tee などの認識できる Bash コマンドにしか効かない。スクリプトの中から書き込むことは防げないので、check.sh 側でも改ざんを検出できるようにする。
- `harness/SYSTEM.md`: 課題に合わない規則だけを直す。規則を足すときは、ここに書くより hook や check にできないかを先に考える。

### 5. 確かめる

ハーネスを渡す前に、次を実際に実行して結果を確かめる。

1. `python3 harness/harness.py check` が想定どおりの合否と SCORE を返す。`score` の課題では同じ状態で 3 回測り、ばらつきの大きさを CONDITIONS.md の「コードからは分からない前提」に書く。3 回の `label` が同じであることも確かめる。違うなら、check が書き換えたファイルを `git status` で調べて `label_ignore` に並べる。シェルが使えないなどの理由で測れないときは、TODO を残さず、「最初の 3 回の check でばらつきを測り、decisions.md に書く」と書く。どのファイルにも `TODO` を残したまま渡さない。
2. `echo '{}' | python3 harness/harness.py hook stop` が、まだ終了条件を満たしていないときに `"decision": "block"` を返す。
3. `python3 harness/harness.py status` と `python3 harness/harness.py stop-rule` が設定どおりの値を示す。ここまでの記録は、run.sh が起動時に `harness/state/archive/` へ移す。
4. 短い試運転をするかをユーザーに聞く。費用がかかるので、するなら `HARNESS_MAX_BUDGET_USD=1 harness/run.sh` のように上限を下げ、`harness/state/runs/` のログで hook が働いたかを確かめる。`claude -p` は検証に通らない設定ファイルを黙って無視するので、ログに `SessionStart` の `hook_response` があることを必ず見る。ログの `system/init` の `permissionMode` が、選んだモード（既定は `auto`）になっているかも見る。auto モードを使えない環境では Manual で始まり、確認が必要な操作がすべて拒否される。

### 6. 引き渡す

ハーネスのファイルを commit するかをユーザーに聞き、次を伝える。

- 起動: `harness/run.sh`。様子を見たいときは `HARNESS_INTERACTIVE=1 harness/run.sh`。
- 権限: 既定は auto モード。使い捨てのコンテナや VM なら `HARNESS_PERMISSION_MODE=bypassPermissions` も選べる。共有のマシンで bypassPermissions は使わない。
- 経過の見方: `python3 harness/harness.py status`、`harness/state/scores.jsonl`、`harness/decisions.md`、`harness/state/runs/*.jsonl`。
- 終わったあと: 差分と decisions.md を人が読み、取り込むかを決める。マージやデプロイは人が承認する。
- 改善: 何度か走らせたら `/harness:build improve` でハーネスを直す。
