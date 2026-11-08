"""Tests for the eval-suite audit. Run: python3 -m unittest discover -s plugins/skill-eval/tests"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "audit" / "scripts" / "audit.py"
spec = importlib.util.spec_from_file_location("eval_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit  # dataclasses look the module up while the class is built
spec.loader.exec_module(audit)

NEGATIVE = """---
type: tool_used
tool: Skill
input_match: '"skill"\\s*:\\s*"(?:[\\w-]+:)?build"'
min: 0
max: 0
arm: both
---
"""


class Suite:
    """A throwaway plugin with an eval suite; cases are written as {name: {file: text}}."""

    def __init__(self, cases: dict[str, dict[str, str]], manifest: dict | None = None):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / ".claude-plugin").mkdir()
        (self.dir / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest or {"name": "p"}))
        root = audit.eval_dir_of(self.dir)
        for name, files in cases.items():
            for rel, text in files.items():
                p = root / name / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(textwrap.dedent(text).lstrip("\n") if not rel.endswith(".md") else text, encoding="utf-8")

    def run(self, *args) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = audit.main([str(self.dir), *args])
        return code, buf.getvalue()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def prompt(body: str, **front) -> str:
    lines = "".join(f"{k}: {json.dumps(v) if not isinstance(v, str) else v}\n" for k, v in front.items())
    return f"---\n{lines}---\n{body}\n"


def grader(**spec) -> str:
    body = spec.pop("body", "")
    lines = []
    for k, v in spec.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            lines += [f"  {kk}: {vv}" for kk, vv in v.items()]
        else:
            lines.append(f"{k}: {v}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


NEG_CASE = {"prompt.md": prompt("Stop hook とは？"), "graders/skill-not-fired.md": NEGATIVE}


class TestCase(unittest.TestCase):
    def suite(self, cases, **kw) -> Suite:
        s = Suite({"negative": NEG_CASE, **cases}, **kw)
        self.addCleanup(s.cleanup)
        return s

    def assertFinding(self, out: str, level: str, where: str, text: str):
        lines = [ln for ln in out.splitlines() if ln.startswith(f"[{level}] {where}") and text in ln]
        self.assertTrue(lines, f"no [{level}] {where} … {text} in:\n{out}")

    def assertNoFinding(self, out: str, where: str, text: str = ""):
        lines = [ln for ln in out.splitlines() if f"] {where}" in ln and text in ln]
        self.assertFalse(lines, f"unexpected finding for {where}:\n{out}")


class ParseYaml(unittest.TestCase):
    def test_grader_frontmatter_forms(self):
        front, body = audit.split_front(textwrap.dedent("""\
            ---
            type: regex
            target:
              source: file
              path: harness/check.sh
            pattern: '(?:^|\\n)(?![ \\t]*#)[^\\n]*it''s'
            min: 0
            weight: 2.5
            flags: i  # case-insensitive
            allowed_tools: [Read, Glob, "Bash(npm test *)"]
            ---
            PASS if x.
            """))
        self.assertEqual(front["target"], {"source": "file", "path": "harness/check.sh"})
        self.assertEqual(front["pattern"], "(?:^|\\n)(?![ \\t]*#)[^\\n]*it's")
        self.assertEqual((front["min"], front["weight"], front["flags"]), (0, 2.5, "i"))
        self.assertEqual(front["allowed_tools"], ["Read", "Glob", "Bash(npm test *)"])
        self.assertEqual(body, "PASS if x.\n")

    def test_block_lists(self):
        data = audit.parse_yaml("context:\n  add_dirs:\n    - resources\n    - more\ngraders:\n  - name: a\n    type: llm\n")
        self.assertEqual(data["context"]["add_dirs"], ["resources", "more"])
        self.assertIs(data["graders"][0], audit.Unsupported)

    def test_no_frontmatter_is_all_body(self):
        self.assertEqual(audit.split_front("hello\n"), ({}, "hello\n"))

    def test_unreadable_lines_raise(self):
        with self.assertRaises(audit.Unsupported):
            audit.parse_yaml("key: 'unterminated\n")
        with self.assertRaises(audit.Unsupported):
            audit.parse_yaml("just text\n")

    def test_unreadable_grader_is_reported_not_crashed(self):
        s = Suite({"c": {"prompt.md": prompt("x"), "graders/bad.md": "---\npattern: 'oops\n---\n"}})
        self.addCleanup(s.cleanup)
        code, out = s.run()
        self.assertEqual(code, 1)
        self.assertIn("[ERROR] c / bad: frontmatter を読めません", out)


class EvalDir(unittest.TestCase):
    def test_manifest_setting_is_used(self):
        s = Suite({"c": NEG_CASE}, manifest={"name": "p", "experimental": {"evals": "quality/evals"}})
        self.addCleanup(s.cleanup)
        self.assertTrue((s.dir / "quality" / "evals" / "c" / "prompt.md").is_file())
        code, out = s.run()
        self.assertEqual(code, 0, out)

    def test_unsafe_manifest_setting_falls_back_like_the_cli(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / ".claude-plugin").mkdir()
        for bad in ("../x", "/abs"):
            (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"experimental": {"evals": bad}}))
            self.assertEqual(audit.eval_dir_of(d), d / "evals")

    def test_results_and_mocks_are_not_cases_and_groups_nest(self):
        s = Suite({"group/inner": NEG_CASE, "results/2026": {"prompt.md": prompt("x")}, "mocks/srv": {"prompt.md": prompt("x")}})
        self.addCleanup(s.cleanup)
        names = [d.name for d in audit.find_case_dirs(audit.eval_dir_of(s.dir))]
        self.assertEqual(names, ["inner"])

    def test_missing_eval_dir(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(audit.main([str(d)]), 1)
        self.assertIn("claude plugin eval init", buf.getvalue())


class StaticChecks(TestCase):
    def test_empty_suite(self):
        s = Suite({})
        self.addCleanup(s.cleanup)
        (s.dir / "evals").mkdir()
        code, out = s.run()
        self.assertEqual(code, 1)
        self.assertIn("ケースがありません", out)

    def test_no_negative_case(self):
        s = Suite({"c": {"prompt.md": prompt("x"), "graders/g.md": grader(type="regex", pattern="ok")}})
        self.addCleanup(s.cleanup)
        self.assertFinding(s.run()[1], "WARN", "(suite)", "呼んではいけない")
        self.assertNoFinding(self.suite({}).run()[1], "(suite)", "呼んではいけない")  # suite() adds a negative case

    def test_case_without_graders_is_an_error(self):
        code, out = self.suite({"c": {"prompt.md": prompt("x")}}).run()
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c", "grader がありません")

    def test_absolute_dates(self):
        for text in ("締め切りは 2036-05-01 です", "2027年3月4日まで", "by 2027/03/04"):
            out = self.suite({"c": {"prompt.md": prompt(text), "graders/g.md": grader(type="regex", pattern="ok")}}).run()[1]
            self.assertFinding(out, "WARN", "c", "絶対日付")
        for text in ("今日から 3 日後まで", "12 時間以内に", "v2026.1 を使う", "port 8080-9090"):
            out = self.suite({"c": {"prompt.md": prompt(text), "graders/g.md": grader(type="regex", pattern="ok")}}).run()[1]
            self.assertNoFinding(out, "c", "絶対日付")

    def test_slash_invocation_with_skill_grader(self):
        skill = grader(type="tool_used", tool="Skill", input_match="build")
        out = self.suite({"c": {"prompt.md": prompt("/harness:build お願いします"), "graders/fired.md": skill}}).run()[1]
        self.assertFinding(out, "WARN", "c / fired", "slash")
        out = self.suite({"c": {"prompt.md": prompt("ハーネスを作って"), "graders/fired.md": skill}}).run()[1]
        self.assertNoFinding(out, "c / fired", "slash")
        read = grader(type="tool_used", tool="Read", input_match="skills/build")
        out = self.suite({"c": {"prompt.md": prompt("/harness:build"), "graders/loaded.md": read}}).run()[1]
        self.assertNoFinding(out, "c / loaded", "slash")

    def test_default_max_turns_with_write_tools(self):
        g = {"graders/g.md": grader(type="regex", pattern="ok")}
        out = self.suite({"c": {"prompt.md": prompt("x", allowed_tools="[Read, Write]"), **g}}).run()[1]
        self.assertFinding(out, "WARN", "c", "max_turns")
        out = self.suite({"c": {"prompt.md": prompt("x", allowed_tools="[Read, Write]", max_turns=40), **g}}).run()[1]
        self.assertNoFinding(out, "c", "max_turns")
        out = self.suite({"c": {"prompt.md": prompt("x", allowed_tools="[Read, Grep]"), **g}}).run()[1]
        self.assertNoFinding(out, "c", "max_turns")

    def test_case_yaml_execution_fields_count(self):
        case = {"case.yaml": "schema_version: \"1.1\"\nname: c\nexecution:\n  prompt: x\n  allowed_tools: [Write]\n  max_turns: 30\n",
                "graders/g.md": grader(type="regex", pattern="ok")}
        self.assertNoFinding(self.suite({"c": case}).run()[1], "c", "max_turns")

    def test_inline_regex_flags(self):
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/g.md": grader(type="regex", pattern="'(?i)done'")}}).run()
        self.assertEqual(out[0], 1)
        self.assertFinding(out[1], "ERROR", "c / g", "flags: i")
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/g.md": grader(type="regex", pattern="'(?:a|b)(?=c)'")}}).run()[1]
        self.assertNoFinding(out, "c / g", "flags")

    def test_trace_quotes(self):
        bad = grader(type="regex", target="trace", pattern="""'"command":"npm test'""")
        good = grader(type="regex", target="trace", pattern="""'\\\\"command\\\\":\\\\"npm test'""")
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/bad.md": bad, "graders/good.md": good}}).run()[1]
        self.assertFinding(out, "WARN", "c / bad", '\\"')
        self.assertNoFinding(out, "c / good", '\\"')

    def test_negative_skill_grader_needs_arm_both_only_beside_others(self):
        no_arm = NEGATIVE.replace("arm: both\n", "")
        answer = grader(type="llm", body="PASS if it explains. FAIL if not.")
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/never.md": no_arm, "graders/answer.md": answer}}).run()[1]
        self.assertFinding(out, "WARN", "c / never", "arm: both")
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/never.md": no_arm}}).run()[1]
        self.assertNoFinding(out, "c / never", "arm: both")
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/never.md": NEGATIVE, "graders/answer.md": answer}}).run()[1]
        self.assertNoFinding(out, "c / never", "arm: both")

    def test_llm_rubric_needs_pass_and_fail(self):
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/j.md": grader(type="llm", body="The answer is good.")}}).run()[1]
        self.assertFinding(out, "WARN", "c / j", "PASS と FAIL")

    def test_result_and_process_graders(self):
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/g.md": grader(type="regex", pattern="ok")}}).run()[1]
        self.assertFinding(out, "INFO", "c", "手順を見る grader")
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/t.md": grader(type="tool_used", tool="Read")}}).run()[1]
        self.assertFinding(out, "INFO", "c", "結果を見る grader")

    def test_bash_cases_and_aws_credential_process(self):
        cfg = Path(tempfile.mkdtemp()) / "config"
        self.addCleanup(shutil.rmtree, cfg.parent)
        cfg.write_text("[profile x]\ncredential_process = /usr/bin/get-creds\n")
        bash = {"prompt.md": prompt("x", allowed_tools='[Read, "Bash(npm test *)"]', max_turns=20), "graders/g.md": grader(type="regex", pattern="ok")}
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": str(cfg)}):
            self.assertFinding(self.suite({"c": bash}).run()[1], "WARN", "(suite)", "credential_process")
            no_bash = {**bash, "prompt.md": prompt("x", allowed_tools="[Read]")}
            self.assertNoFinding(self.suite({"c": no_bash}).run()[1], "(suite)", "credential_process")
        cfg.write_text("[profile x]\nregion = us-east-1\n")
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": str(cfg)}):
            self.assertNoFinding(self.suite({"c": bash}).run()[1], "(suite)", "credential_process")


FIXTURE = "#!/usr/bin/env bash\nset -eu\nmkdir -p app\necho 'TODO: fill me' > app/notes.md\necho 'mode: draft' > app/config.yml\n"


class LazyRun(TestCase):
    def case(self, **graders) -> dict:
        files = {"case.yaml": "schema_version: \"1.1\"\nname: c\ncontext:\n  scaffold_script: fixture.sh\n",
                 "fixture.sh": FIXTURE, "prompt.md": prompt("設定を書いて", max_turns=20)}
        files.update({f"graders/{k}.md": v for k, v in graders.items()})
        return files

    def test_scaffold_is_not_run_without_the_flag(self):
        s = self.suite({"c": self.case(g=grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode"))})
        out = s.run()[1]
        self.assertFinding(out, "INFO", "c", "--scaffold")
        self.assertNoFinding(out, "c / g", "怠けた")

    def test_grader_that_passes_on_the_scaffold_alone(self):
        weak = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode")
        strong = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="'mode: final'")
        out = self.suite({"c": self.case(weak=weak, strong=strong)}).run("--scaffold")[1]
        self.assertFinding(out, "WARN", "c / weak", "怠けた実行")
        self.assertNoFinding(out, "c / strong", "怠けた実行")

    def test_case_that_passes_doing_nothing_is_an_error(self):
        weak = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode")
        quiet = grader(type="regex", pattern="error", match="not_contains")
        code, out = self.suite({"c": self.case(weak=weak, quiet=quiet)}).run("--scaffold")
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c", "何もしなくても 1.00")
        self.assertNoFinding(out, "c / quiet", "怠けた")  # a "never" check is meant to pass here

    def test_case_of_never_checks_only_is_a_warning(self):
        quiet = grader(type="regex", pattern="error", match="not_contains")
        code, out = self.suite({"c": self.case(quiet=quiet)}).run("--scaffold")
        self.assertEqual(code, 0)
        self.assertFinding(out, "WARN", "c", "「しないこと」")

    def test_threshold(self):
        weak = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode")
        strong = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="final")
        s = self.suite({"c": self.case(weak=weak, strong=strong)})
        self.assertNoFinding(s.run("--scaffold")[1], "c", "何もしなくても")
        self.assertFinding(s.run("--scaffold", "--threshold", "0.5")[1], "ERROR", "c", "何もしなくても 0.50")

    def test_not_contains_on_a_scaffold_file_still_holding_todo_fails(self):
        filled = grader(type="regex", target={"source": "file", "path": "app/notes.md"}, pattern="TODO", match="not_contains")
        missing = grader(type="regex", target={"source": "file", "path": "app/absent.md"}, pattern="TODO", match="not_contains")
        out = self.suite({"c": self.case(filled=filled, missing=missing)}).run("--scaffold")[1]
        self.assertNoFinding(out, "c / filled", "怠けた")
        self.assertFinding(out, "INFO", "c", "判定できない grader: missing")

    def test_file_exists_on_a_scaffold_file_never_passes(self):
        g = grader(type="file_exists", path="app/*.yml")
        code, out = self.suite({"c": self.case(made=g)}).run("--scaffold")
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c / made", "必ず落ちます")

    def test_overlay_counts_as_created_files(self):
        ov = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ov)
        (ov / "app").mkdir()
        (ov / "app" / "config.yml").write_text("mode: final\n")   # modified, not created
        (ov / "app" / "plan.md").write_text("TODO\n")              # created by the lazy agent
        made = grader(type="file_exists", path="app/plan.md")
        final = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="final")
        listed = grader(type="regex", target="files", pattern="config")
        out = self.suite({"c": self.case(made=made, final=final, listed=listed)}).run("--scaffold", "--overlay", str(ov))[1]
        self.assertFinding(out, "WARN", "c / made", "重ねた状態")
        self.assertFinding(out, "WARN", "c / final", "重ねた状態")
        self.assertNoFinding(out, "c / listed", "怠けた")  # config.yml existed before: it was only edited

    def test_skill_and_with_only_graders_are_left_out_of_the_score(self):
        skill = grader(type="tool_used", tool="Skill", input_match="build", min=0)
        with_only = grader(type="tool_used", tool="Read", min=0, arm="with-only")
        real = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="final")
        out = self.suite({"c": self.case(skill=skill, with_only=with_only, real=real)}).run("--scaffold")[1]
        self.assertNoFinding(out, "c", "何もしなくても")
        c = audit.Case("c", Path("."), graders=[audit.Grader("s", {"type": "tool_used", "tool": "Skill"}, "")])
        self.assertEqual([g.name for g in audit.scored(c)], ["s"])  # all excluded: scored normally

    def test_weights_and_counts(self):
        lazy = audit.Lazy(Path("."), [])
        g = lambda **s: audit.Grader("g", s, "")
        self.assertTrue(audit.lazy_verdict(g(type="regex", pattern="x", match="count:0"), lazy))
        self.assertFalse(audit.lazy_verdict(g(type="regex", pattern="x", match="count:1"), lazy))
        self.assertFalse(audit.lazy_verdict(g(type="tool_used", tool="Read"), lazy))
        self.assertTrue(audit.lazy_verdict(g(type="tool_used", tool="Write", min=0, max=0), lazy))
        self.assertFalse(audit.lazy_verdict(g(type="tool_order", before="Read", after="Write"), lazy))
        self.assertTrue(audit.lazy_verdict(g(type="file_exists", path="*.md", exists=False), lazy))
        self.assertIsNone(audit.lazy_verdict(g(type="llm"), lazy))
        heavy = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode", weight=3)
        light = grader(type="regex", pattern="done")
        out = self.suite({"c": self.case(heavy=heavy, light=light)}).run("--scaffold", "--threshold", "0.75")[1]
        self.assertFinding(out, "ERROR", "c", "0.75")

    def test_scaffold_failure_and_minimal_env(self):
        bad = self.case(g=grader(type="regex", pattern="ok"))
        bad["fixture.sh"] = "echo boom >&2; exit 3\n"
        code, out = self.suite({"c": bad}).run("--scaffold")
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c", "終了コード 3")
        env = self.case(g=grader(type="regex", target={"source": "file", "path": "env.txt"}, pattern="SECRET"))
        env["fixture.sh"] = 'echo "${AUDIT_TEST_SECRET:-}" > env.txt\n'
        with mock.patch.dict(os.environ, {"AUDIT_TEST_SECRET": "SECRET"}):
            out = self.suite({"c": env}).run("--scaffold")[1]
        self.assertNoFinding(out, "c / g", "怠けた")  # the shell's other variables do not reach the scaffold

    def test_case_glob_limits_the_lazy_run_only(self):
        weak = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode")
        out = self.suite({"c": self.case(weak=weak)}).run("--scaffold", "--case", "other*")[1]
        self.assertNoFinding(out, "c / weak", "怠けた")


class RealSuite(unittest.TestCase):
    def test_harness_suite_has_no_errors(self):
        harness = REPO / "plugins" / "harness"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = audit.main([str(harness), "--scaffold"])
        self.assertEqual(code, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
