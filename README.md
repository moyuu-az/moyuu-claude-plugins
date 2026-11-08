# moyuu-claude-plugins

moyuu の Claude Code plugin marketplace。

## 追加と導入

```bash
claude plugin marketplace add moyuu-az/moyuu-claude-plugins
```

```bash
claude plugin install harness@moyuu
```

```bash
claude plugin install skill-eval@moyuu
```

ローカルの clone から追加するときは、`claude plugin marketplace add <clone したディレクトリ>` を使う。

## plugin

| plugin | 内容 |
| --- | --- |
| [harness](plugins/harness) | Claude Code を人の介入なしで最後まで走らせるハーネスを、対象プロジェクトに作る。`/harness:build` で作り、`/harness:build improve` で実行結果から直す |
| [skill-eval](plugins/skill-eval) | `claude plugin eval` のケースを点検する。`claude plugin eval init` で作ったケースに `/skill-eval:audit` を使い、何もしなくても合格する grader などを見つける |

## harness の使い方

1. ハーネスを置きたいプロジェクトで `/harness:build` を実行する。課題の種類、評価の方法、締め切りなどを決め、`harness/` を作る。
2. `harness/run.sh` で無人実行を始める。経過は `python3 harness/harness.py status` で見られる。
3. 終わったら差分と `harness/decisions.md` を読み、取り込むかを決める。

## 開発

```bash
python3 -m unittest discover -s plugins/harness/tests
```

```bash
python3 -m unittest discover -s plugins/skill-eval/tests
```

```bash
claude plugin validate .
```

skill の振る舞いは `claude plugin eval` で測る。ケースは `plugins/harness/evals/` にあり、plugin ありとなしで 3 回ずつ実行して比べる。モデルを呼ぶので費用がかかる（全ケースで $25〜30）。

```bash
cd plugins/harness && claude plugin eval . --scaffold --trust-plugin --allow-tools Write Edit -j 2 --max-cost-usd 40
```

ケースは Bash を許可していない。`~/.aws/config` に `credential_process` があると、Bash を許可した eval は動かないためである。`cp` と `harness.py check` の実行は、単体テストで確かめる。
