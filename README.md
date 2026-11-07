# moyuu-claude-plugins

moyuu の Claude Code plugin marketplace。

## 追加と導入

```bash
claude plugin marketplace add moyuu-az/moyuu-claude-plugins
```

```bash
claude plugin install harness@moyuu
```

ローカルの clone から追加するときは、`claude plugin marketplace add <clone したディレクトリ>` を使う。

## plugin

| plugin | 内容 |
| --- | --- |
| [harness](plugins/harness) | Claude Code を人の介入なしで最後まで走らせるハーネスを、対象プロジェクトに作る。`/harness:build` で作り、`/harness:build improve` で実行結果から直す |

## harness の使い方

1. ハーネスを置きたいプロジェクトで `/harness:build` を実行する。課題の種類、評価の方法、締め切りなどを決め、`harness/` と `.claude/agents/auditor.md` を作る。
2. `harness/run.sh` で無人実行を始める。経過は `python3 harness/harness.py status` で見られる。
3. 終わったら差分と `harness/decisions.md` を読み、取り込むかを決める。

## 開発

```bash
python3 -m unittest discover -s plugins/harness/tests
```

```bash
claude plugin validate .
```
