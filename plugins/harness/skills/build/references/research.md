# 調査の根拠

ハーネスの各部品を、どの資料のどの記述に基づいて決めたかをまとめる。2026-11-07 時点で読んだ内容である。OpenAI Codex の節だけは 2026-11-09 に読んだ。

## 参照した資料

- [ISUCON2026 競技当日編（kawasy, Zenn）](https://zenn.dev/luvtechno/articles/isucon2026-competition-day): 完全自律の競技エージェントで優勝した記録。準備の詳細は「準備編」として別記事の予定で、ディレクトリ構成や hook の定義は書かれていない。
- [Effective harnesses for long-running agents（Anthropic, 2025-11）](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Harness design for long-running application development（Anthropic, 2026-03）](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [Best practices for Claude Code](https://code.claude.com/docs/en/best-practices)
- [openai/codex](https://github.com/openai/codex)（commit `8703d32`）: OpenAI のコーディングエージェント Codex のソースコード。下の節のパスは、このリポジトリの `codex-rs/` からの相対パスである
- [Hooks reference](https://code.claude.com/docs/en/hooks)、[Configure permissions](https://code.claude.com/docs/en/permissions)、[Skills](https://code.claude.com/docs/en/skills)、[Plugin manifest reference](https://code.claude.com/docs/en/plugins/manifest-reference)

## ISUCON2026 の記事から取り入れたこと

| 記事の記述 | ハーネスでの形 |
| --- | --- |
| 実行ごとに変わる条件（競技時間、レギュレーション、ポータルの URL）を CONDITIONS.md に書き、初期プロンプトは「CONDITIONS.md を読み、今回の条件を確認してから、ISUCON を解いてください。」の 1 行だけ。追加の指示はシステムプロンプトに足した | `harness/CONDITIONS.md`、`harness/PROMPT.md`、`harness/SYSTEM.md` を `--append-system-prompt` で渡す |
| 起動後、人からの入力は 0 回、エージェントが人を呼んだ回数も 0 回 | 質問は build の手順 2 だけで済ませる。SYSTEM.md で、迷ったら decisions.md に根拠を書いて進むよう指示する |
| 監査役は「エージェントがこれまでの判断を書き残した記録を、準備しておいたチェック項目の一覧と突き合わせて見直す、サブエージェント」 | `harness/agents.json`（`--agents` で渡す監査役）、`harness/checklist.md`、`harness/decisions.md` |
| スコアが 30 分以上伸びないときと終盤に、Claude Code の hook が監査役の起動を促した | PostToolUse hook の `stall_minutes` と `final_check_minutes` |
| 「この構成で測ったスコアの一覧と残り時間から『あと何回走らせられるか』を計算し、その回数だけ走らせ直せるときの期待値を閾値にしました」。残り回数が減ると閾値も下がる | `harness.py` の `continuation_value` と Stop hook。最後の 1 回が採点される前提で、最適停止の漸化式 C_1 = E[X]、C_k = E[max(X, C_{k-1})] を使う |
| 同じ構成でもスコアが 474,987〜629,638 点とばらつき、後半の変更の効果より大きかった | check の記録に commit の label を付け、同じ label の結果だけで閾値を計算する。`min_samples` 回測るまで止めない。checklist.md に「差がばらつきより大きいか」を入れる |
| 環境に偽の指示を書いた AGENTS.md が置かれていたが、エージェントは従わなかった | SYSTEM.md で、指示の出どころをシステムプロンプトと CONDITIONS.md に限る |
| 原因を確定しないまま、実測値からボトルネックをベンチマーカー側と判断した。自分で入れたデバッグモードの JPEG のデコードが CPU 時間の約 86% を占めていた。200MB のつもりの取得で 13GB を落とした | checklist.md と SYSTEM.md の「計測の扱い」に、それぞれを防ぐ確認の項目として入れた |
| SSH のコマンドが返らないまま止まった転送を、接続が切れても続く形で実行し直した。再起動後にサービスが戻るかを確かめた | SYSTEM.md の「計測の扱い」と「最終確認」 |
| 自動要約が 1 回起き、コンテキストが約 96.7 万トークンから約 2.3 万トークンになった | SessionStart hook（source が compact のとき decisions.md の末尾を渡す）と PreCompact hook |
| 評価環境で 3 か月、100 回以上問題を解かせて、ユーザハーネスを改善した | `harness.py report` と improve の手順 |

## Anthropic の記事から取り入れたこと

- 長時間の作業では、機能の一覧（JSON）、進捗のファイル、`init.sh`、git の commit を残し、次のセッションがそれを読んで再開する。モデルは「Markdown より JSON を不適切に書き換えにくい」ため一覧は JSON にし、テストを消したり書き換えたりすることを禁じる。→ tuning.md の「大きな機能群を複数セッションで作る」。
- 作業する者と評価する者を分けると効く。生成した本人に自分の成果を厳しく見させるより、独立した評価役を懐疑的に調整するほうが扱いやすい。評価役は静的な出力ではなく動いているアプリを操作して確かめる。→ auditor を独立させ、読み取り専用にする。
- 評価の基準には、それぞれ下回ったら不合格になる閾値を置く。→ check.sh は合否を終了コードで返す。
- 部品は「モデルが単独ではできないこと」についての仮定で、モデルが良くなると古くなる。1 つずつ外して、効いている部品を見極める。→ improve の手順。

## OpenAI Codex から取り入れたこと

Codex には、目標を達成するまで自動でターンを続ける goal の機能がある。続けるたびに渡す指示（`ext/goal/templates/goals/continuation.md`）が、このハーネスの Stop hook と監査役に近い役目を持つ。

| Codex の実装 | ハーネスでの形 |
| --- | --- |
| goal を続けるときの指示で、直前のターンを「進展」「確かめた待ち」「進展なし」に分ける。状況の言い直しや実行していない計画は進展ではない。見張りの時間切れや、状態の取得の一時的な失敗は処理の終了ではなく、それだけを理由に処理を起動し直さない（`continuation.md:18-21`） | SYSTEM.md の「進んでいるかの確かめ」。スコアの課題で Stop hook が終了を止めるときは、理由の文でこの節を示す |
| 同じ障害が 3 ターン続くまで blocked にしない。言い回しが変わっても同じ障害として数える（`continuation.md:21`、`:44-50`） | 同じ障害が 3 回続いたら decisions.md に書き、auditor を起動する |
| 完了を宣言する前に、目標から要件を書き出し、要件ごとに今の状態の証拠を確かめる。テストの合格は、その要件を見ていると確かめたときだけ証拠になる。「残りの作業が見つからない」は完了の証拠にならない。テストに通りやすい狭い解に置き換えない（`continuation.md:26-42`） | SYSTEM.md の「終える前の確かめ」、auditor の最終監査、checklist.md の項目 |
| 自動承認の審査役（guardian）への指示に「以前の判断は文脈であって前例ではない」とある（`prompts/templates/guardian/policy.md:3`） | auditor は、decisions.md に残った以前の監査の結論を前例として扱わない |
| 承認が要る操作を、ほかのツールで回避しないよう指示している（`prompts/templates/permissions/approval_policy/on_request.md:42`）。審査役が 1 ターンで 3 回続けて拒否すると、ターンを止める（`ext/guardian-reviewer/src/circuit_breaker.rs:3-7`） | 拒否された操作を別の書き方で試さず、decisions.md に書く。checklist.md でも確かめる |
| 書き込みを許したディレクトリの中でも、`.git` などへの書き込みは sandbox が防ぐ（`protocol/src/permissions.rs:43-54`） | settings.json の deny に `Edit(./.git/**)` を足した。deny は組み込みツールと認識できる Bash のファイル操作にしか効かない |
| codex 系モデルの指示で、自分が入れていない変更を戻さず、`git reset --hard` や `git checkout --` を使わない（`core/gpt-5.2-codex_prompt.md:13-19`） | SYSTEM.md で、戻すのは自分が入れた変更だけにする |

次のものは、ハーネスがすでに同じ形をとっているので変えていない。

- hook の出力が 2,500 トークンを超えると、全文をファイルに書き、先頭と末尾だけをモデルに渡す（`hooks/src/output_spill.rs:13`）。harness.py の Stop hook も、check の出力の末尾 3,000 文字と、全文のログのパスだけをエージェントに渡す。
- コンテキストの要約のあと、SessionStart の hook を source `compact` で呼び直す（`core/src/session/mod.rs:4931`）。要約に残す内容の指示も、進捗と判断、制約、残りの作業、続けるのに要るデータで、PreCompact hook の指示と重なる（`prompts/templates/compact/prompt.md`）。
- `codex exec` の結果のファイルは、エージェントが動き出す前に起動側が開く（`exec/src/lib.rs:1086`）。run.sh も、ログのファイルを起動側でリダイレクトしている。

次のものは取り入れていない。

- **AGENTS.md を信頼できる指示として扱うこと。** guardian は AGENTS.md を信頼できる情報源に数える（`prompts/templates/guardian/policy_template.md:6`）。このハーネスは ISUCON2026 で偽の AGENTS.md が置かれた例に合わせ、指示の出どころをシステムプロンプトと CONDITIONS.md に限ったままにする。
- **予期しない変更を見つけたら止まって人に聞くこと**（`core/gpt-5.2-codex_prompt.md:18`）。無人の実行では答えが返らないので、decisions.md に書いて続ける形に替えた。
- **費用の上限がないこと。** `codex exec` には、費用やターン数、経過時間の上限を決めるオプションがない。run.sh の `--max-budget-usd` は残す。
- **Stop の連続ブロックに上限がないこと。** Codex の Stop hook は何回でも終了を止められる（`core/src/session/turn.rs:841`）。Claude Code には上限があるが、ツールを呼ぶたびに数え直されるので、作業を続けている実行は止まらない。

## Claude Code のドキュメントから取り入れたこと

- 指示には、モデルが判断する非決定的なもの（プロンプト、CLAUDE.md、skill）と、ハーネスが必ず実行する決定的なもの（hook、deny、managed settings）がある。必ず起きてほしいことは決定的なほうに置く。
- 「Claude は作業が終わったように見えたところで止まる」。合否を返すものを渡せば、変更、実行、修正を自分で繰り返せる。Stop hook は終了コード 2 か `{"decision": "block", "reason": ...}` で終了を止め、reason が次の指示としてエージェントに渡る。
- Stop hook には、ツール呼び出しを挟まずに 8 回続けて止めると、次は止めずに終了させる上限がある（`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` で変えられる）。Stop の入力の `background_tasks` で、バックグラウンドの処理を待っているだけかを見分けられる。
- 無人で走らせる前に、完了の定義、自分で確かめる手段、触ってよい範囲、止める仕組み（`--max-budget-usd` など）、結果の行き先と承認者を決めておく。
- `--settings <file>` で渡した設定の `/path` は、そのファイルのディレクトリを起点に解決される。`./path` は作業ディレクトリが起点になる。Read と Edit の deny は組み込みツールと、認識できる Bash のファイル操作には効くが、スクリプトが中でファイルを開く場合には効かない。
- CLAUDE.md や skill には、コードを読めば分かることを書かない。大文字の IMPORTANT を重ねても守られない規則は、hook にする。
