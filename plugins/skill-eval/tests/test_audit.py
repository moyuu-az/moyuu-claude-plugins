"""Tests for the eval-suite audit. Run: python3 -m unittest discover -s plugins/skill-eval/tests"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
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

    def test_flow_maps_and_lists(self):
        # The docs write a file target as a flow map; a comma inside quotes is not a separator.
        data = audit.parse_yaml("target: {source: file, path: \"a b.md\"}  # the report\n"
                                "allowed_tools: [Read, 'Bash(git commit -m *, x)', \"Edit\"]\n")
        self.assertEqual(data["target"], {"source": "file", "path": "a b.md"})
        self.assertEqual(data["allowed_tools"], ["Read", "Bash(git commit -m *, x)", "Edit"])

    def test_block_scalars(self):
        data = audit.parse_yaml(textwrap.dedent("""\
            execution:
              prompt: |
                締め切り: 2027-03-04
                # not a comment

                最後の行
              append_system_prompt: >-
                one
                two

                three
              max_turns: 30
            """))
        self.assertEqual(data["execution"]["prompt"], "締め切り: 2027-03-04\n# not a comment\n\n最後の行\n")
        self.assertEqual(data["execution"]["append_system_prompt"], "one two\nthree")
        self.assertEqual(data["execution"]["max_turns"], 30)

    def test_comment_after_a_key_keeps_its_children(self):
        data = audit.parse_yaml("context:  # set up the workspace\n  scaffold_script: fixture.sh\nname: c\n")
        self.assertEqual(data, {"context": {"scaffold_script": "fixture.sh"}, "name": "c"})

    def test_compact_block_list(self):
        data = audit.parse_yaml("allowed_tools:\n- Read\n- Write\nmax_turns: 3\n")
        self.assertEqual(data, {"allowed_tools": ["Read", "Write"], "max_turns": 3})

    def test_double_quoted_escapes_follow_yaml(self):
        self.assertEqual(audit.parse_yaml('p: "\\x41\\t\\\\d\\u00e9"\n')["p"], "A\t\\dé")
        with self.assertRaises(audit.Unsupported):
            audit.parse_yaml('p: "\\d+"\n')  # not a YAML escape: the CLI fails to load it too

    def test_core_schema_scalars(self):
        data = audit.parse_yaml("a: False\nb: TRUE\nc: ~\nd: Null\ne: 1e3\nf: -2\ng: 'False'\nh: a:b\n")
        self.assertEqual(data, {"a": False, "b": True, "c": None, "d": None, "e": 1000.0, "f": -2, "g": "False", "h": "a:b"})

    def test_tabs_inside_block_text_are_content(self):
        self.assertEqual(audit.parse_yaml("prompt: |\n  all:\n  \tmake\n")["prompt"], "all:\n\tmake\n")

    def test_unterminated_frontmatter(self):
        s = Suite({"c": {"prompt.md": prompt("x"), "graders/g.md": "---\ntype: regex\npattern: ok\n"}})
        self.addCleanup(s.cleanup)
        code, out = s.run()
        self.assertEqual(code, 1)
        self.assertIn("[ERROR] c / g: frontmatter を読めません（--- で閉じていません）", out)

    def test_input_it_cannot_read_is_never_silently_misread(self):
        for text in ("pattern: a\n  b: c\n",          # a deeper line after a scalar
                     "pattern: |x\n",                 # not a block scalar header
                     "pattern: a: b\n",               # YAML rejects ': ' in a plain scalar
                     "pattern: 'a' b\n",              # text after a quoted value
                     "pattern: &x a\n",               # anchors are not supported
                     "\tpattern: a\n",                # tab indentation
                     "list: [a, [b\n"):               # an unclosed flow list
            with self.subTest(text=text), self.assertRaises(audit.Unsupported):
                audit.parse_yaml(text)

    def test_unreadable_files_and_shapes_are_reported_not_crashed(self):
        s = Suite({"c": {"prompt.md": prompt("x"), "graders/g.md": grader(type="regex", pattern="ok")},
                   "d": {"case.yaml": "schema_version: \"1.1\"\nname: d\nexecution: run it\n", "graders/g.md": grader(type="regex", pattern="ok")}})
        self.addCleanup(s.cleanup)
        (s.dir / "evals" / "c" / "graders" / "latin1.md").write_bytes(b"---\ntype: regex\npattern: caf\xe9\n---\n")
        code, out = s.run()
        self.assertEqual(code, 1)
        self.assertIn("[ERROR] c / latin1:", out)
        self.assertIn("[ERROR] d: ケースのファイルを読めません", out)


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
        for ok in ("'(?:a|b)(?=c)'", "'(?i:done)'"):  # a modifier group is valid JavaScript
            out = self.suite({"c": {"prompt.md": prompt("x"), "graders/g.md": grader(type="regex", pattern=ok)}}).run()[1]
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

    def test_body_is_the_pattern_or_criteria_like_the_cli(self):
        # The CLI moves the body into `pattern` (regex) or `criteria` (llm) when the key is absent.
        body_pattern = "---\ntype: regex\ntarget: files\n---\nreport\\.md\n"
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/g.md": body_pattern}}).run()[1]
        self.assertNoFinding(out, "c / g", "でも合格します")  # an empty pattern would match the empty reply
        self.assertNoFinding(out, "c", "でも 1.00")
        criteria = grader(type="llm", criteria="'PASS if it explains. FAIL if not.'")
        out = self.suite({"c": {"prompt.md": prompt("x"), "graders/j.md": criteria}}).run()[1]
        self.assertNoFinding(out, "c / j", "PASS と FAIL")

    def test_grader_fields_the_cli_rejects(self):
        for bad, text in (({"weight": "heavy"}, "weight"), ({"weight": 0}, "weight"), ({"weight": -1}, "weight"),
                          ({"match": "count:x"}, "match"), ({"match": "'count: 2'"}, "match"), ({"pattern": "[a]"}, "pattern")):
            spec = {"type": "regex", "pattern": "ok", **bad}
            code, out = self.suite({"c": {"prompt.md": prompt("x"), "graders/g.md": grader(**spec)}}).run()
            with self.subTest(bad=bad):
                self.assertEqual(code, 1, out)
                self.assertFinding(out, "ERROR", "c / g", text)
        for bad in ({"min": "a"}, {"max": 1.5}, {"min": -1}):
            code, out = self.suite({"c": {"prompt.md": prompt("x"), "graders/t.md": grader(type="tool_used", tool="Read", **bad)}}).run()
            with self.subTest(bad=bad):
                self.assertEqual(code, 1, out)
                self.assertFinding(out, "ERROR", "c / t", "整数")
        code, out = self.suite({"c": {"prompt.md": prompt("x"), "graders/g.md": grader(type="regexp", pattern="ok")}}).run()
        self.assertFinding(out, "ERROR", "c / g", "type")
        ok = grader(type="tool_used", tool="Read", min=1.0, max=2, weight=0.5)  # 1.0 is an integer in JavaScript
        self.assertNoFinding(self.suite({"c": {"prompt.md": prompt("x"), "graders/t.md": ok}}).run()[1], "c / t")

    def test_files_the_cli_reads_differently(self):
        notes = "notes for humans, no frontmatter\n"   # the CLI skips a graders/*.md without frontmatter
        bom = "\ufeff" + grader(type="regex", pattern="ok")  # and strips a BOM
        code, out = self.suite({"c": {"prompt.md": prompt("x"), "graders/README.md": notes, "graders/g.md": bom}}).run()
        self.assertEqual(code, 0, out)
        self.assertNoFinding(out, "c / README", "type がありません")
        self.assertNoFinding(out, "c / g", "type がありません")
        self.assertFinding(out, "WARN", "c / README", "frontmatter")

    def test_graders_only_in_case_yaml(self):
        case = {"case.yaml": "schema_version: \"1.1\"\nname: c\nexecution:\n  prompt: x\ngraders:\n  - name: judge\n    type: llm\n    criteria: |\n      PASS if x.\n      FAIL if not.\n"}
        code, out = self.suite({"c": case}).run()
        self.assertNoFinding(out, "c", "grader がありません")
        self.assertEqual(code, 0, out)
        quiet = grader(type="regex", pattern="error", match="not_contains")
        out = self.suite({"c": {**case, "graders/quiet.md": quiet}}).run()[1]
        self.assertNoFinding(out, "c", "「しないこと」")  # the case.yaml grader is not scored here, so no case verdict
        self.assertFinding(out, "INFO", "c", "case.yaml の graders:")

    def test_prompt_md_without_a_body_keeps_the_case_yaml_prompt(self):
        case = {"case.yaml": "schema_version: \"1.1\"\nname: c\nexecution:\n  prompt: 2027-03-04 までに\n",
                "prompt.md": "---\nmax_turns: 5\n---\n", "graders/g.md": grader(type="regex", pattern="ok")}
        self.assertFinding(self.suite({"c": case}).run()[1], "WARN", "c", "絶対日付")

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
        self.assertNoFinding(out, "c / g", "でも合格します")

    def test_grader_that_passes_on_the_scaffold_alone(self):
        weak = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode")
        strong = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="'mode: final'")
        out = self.suite({"c": self.case(weak=weak, strong=strong)}).run("--scaffold")[1]
        self.assertFinding(out, "WARN", "c / weak", "でも合格します")
        self.assertNoFinding(out, "c / strong", "でも合格します")

    def test_case_that_passes_doing_nothing_is_an_error(self):
        weak = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode")
        quiet = grader(type="regex", pattern="error", match="not_contains")
        code, out = self.suite({"c": self.case(weak=weak, quiet=quiet)}).run("--scaffold")
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c", "何もしなかった実行でも 1.00")
        self.assertNoFinding(out, "c / quiet", "でも合格します")  # a "never" check is meant to pass here

    def test_case_of_never_checks_only_is_a_warning(self):
        quiet = grader(type="regex", pattern="error", match="not_contains")
        code, out = self.suite({"c": self.case(quiet=quiet)}).run("--scaffold")
        self.assertEqual(code, 0)
        self.assertFinding(out, "WARN", "c", "「しないこと」")

    def test_threshold(self):
        weak = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode")
        strong = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="final")
        s = self.suite({"c": self.case(weak=weak, strong=strong)})
        self.assertNoFinding(s.run("--scaffold")[1], "c", "でも 1.00 点")
        self.assertFinding(s.run("--scaffold", "--threshold", "0.5")[1], "ERROR", "c", "何もしなかった実行でも 0.50")

    def test_not_contains_on_a_scaffold_file_still_holding_todo_fails(self):
        filled = grader(type="regex", target={"source": "file", "path": "app/notes.md"}, pattern="TODO", match="not_contains")
        missing = grader(type="regex", target={"source": "file", "path": "app/absent.md"}, pattern="TODO", match="not_contains")
        out = self.suite({"c": self.case(filled=filled, missing=missing)}).run("--scaffold")[1]
        self.assertNoFinding(out, "c / filled", "でも合格します")
        self.assertFinding(out, "INFO", "c", "判定できない grader: missing")

    def test_file_exists_on_a_scaffold_file_never_passes(self):
        g = grader(type="file_exists", path="app/config.yml")
        code, out = self.suite({"c": self.case(made=g)}).run("--scaffold")
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c / made", "必ず落ちます")

    def test_file_exists_glob_that_also_hits_a_scaffold_file_can_still_pass(self):
        # app/new.yml made during the run would pass `app/*.yml`, so this is not "always fails".
        code, out = self.suite({"c": self.case(made=grader(type="file_exists", path="'app/*.yml'"))}).run("--scaffold")
        self.assertEqual(code, 0, out)
        self.assertFinding(out, "WARN", "c / made", "app/config.yml")
        # `*` stays inside one directory, as in the CLI, so `*.yml` does not reach app/config.yml at all.
        out = self.suite({"c": self.case(made=grader(type="file_exists", path="'*.yml'"))}).run("--scaffold")[1]
        self.assertNoFinding(out, "c / made")

    def test_file_exists_globs_follow_the_cli(self):
        rx = audit.glob_regex
        cases = [("*.md", "plan.md", True), ("*.md", "a/plan.md", False), ("**/*.md", "plan.md", True),
                 ("**/*.md", "a/b/plan.md", True), ("a/**", "a/b/c", True), ("?.md", "/.md", True),
                 ("[ab].md", "a.md", False), ("[ab].md", "[ab].md", True), ("{a,b}.md", "a.md", False), ("a.md", "aXmd", False)]
        for pattern, path, hit in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bool(rx(pattern).fullmatch(path)), hit)

    def test_regex_follows_javascript_not_python(self):
        g = lambda **s: audit.Grader("g", s, "")
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws)
        (ws / "r.md").write_text("## 結果\nTODO\n", encoding="utf-8")
        lazy = audit.Lazy(ws, [])
        file = {"source": "file", "path": "r.md"}
        self.assertFalse(audit.lazy_verdict(g(type="regex", target=file, pattern="## \\w"), lazy))   # \w is ASCII in JavaScript
        self.assertFalse(audit.lazy_verdict(g(type="regex", target=file, pattern="TODO$"), lazy))    # $ is the very end
        self.assertTrue(audit.lazy_verdict(g(type="regex", target=file, pattern="TODO$", flags="m"), lazy))
        self.assertTrue(audit.lazy_verdict(g(type="regex", target=file, pattern="todo", flags="i"), lazy))
        self.assertTrue(audit.lazy_verdict(g(type="regex", target=file, pattern="[$]|TODO\\n$"), lazy))

    def test_trace_is_never_empty_so_it_is_unknown(self):
        # The trace holds the session's own start and result messages even when Claude did nothing.
        lazy = audit.Lazy(Path("."), [])
        for match in ("contains", "not_contains", "count:0"):
            g = audit.Grader("g", {"type": "regex", "target": "trace", "pattern": "Skill", "match": match}, "")
            self.assertIsNone(audit.lazy_verdict(g, lazy), match)

    def test_count_and_missing_files(self):
        g = lambda **s: audit.Grader("g", {"type": "regex", **s}, "")
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws)
        (ws / "r.md").write_text("a a a\n")
        lazy = audit.Lazy(ws, [])
        file, missing = {"source": "file", "path": "r.md"}, {"source": "file", "path": "absent.md"}
        self.assertTrue(audit.lazy_verdict(g(target=file, pattern="a", match="count:3"), lazy))
        self.assertFalse(audit.lazy_verdict(g(target=file, pattern="a", match="count:2"), lazy))
        self.assertFalse(audit.lazy_verdict(g(target=file, pattern="a", match="count:4"), lazy))
        self.assertTrue(audit.lazy_verdict(g(target=file, pattern="b", match="count:0"), lazy))
        self.assertFalse(audit.lazy_verdict(g(target=missing, pattern="a"), lazy))
        self.assertIsNone(audit.lazy_verdict(g(target=missing, pattern="a", match="not_contains"), lazy))
        self.assertIsNone(audit.lazy_verdict(g(target=missing, pattern="a", match="count:0"), lazy))
        self.assertTrue(audit.is_guard(g(pattern="error", match="count:0")))   # the same "never" check as not_contains
        self.assertFalse(audit.is_guard(g(pattern="error", match="count:1")))

    def test_file_targets_stay_inside_the_workspace(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (outside / "secret.txt").write_text("mode\n")
        g = lambda path: audit.Grader("g", {"type": "regex", "target": {"source": "file", "path": path}, "pattern": "mode"}, "")
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws)
        (ws / "link.txt").symlink_to(outside / "secret.txt")
        lazy = audit.Lazy(ws, [])
        for path in (str(outside / "secret.txt"), os.path.relpath(outside / "secret.txt", ws), "link.txt", "."):
            with self.subTest(path=path):
                self.assertFalse(audit.lazy_verdict(g(path), lazy))  # the CLI refuses these, so the grader fails

    def test_overlay_counts_as_created_files(self):
        ov = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ov)
        (ov / "app").mkdir()
        (ov / "app" / "config.yml").write_text("mode: final\n")   # modified, not created
        (ov / "app" / "plan.md").write_text("TODO\n")              # created by the lazy agent
        made = grader(type="file_exists", path="app/plan.md")
        final = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="final")
        listed = grader(type="regex", target="files", pattern="config")
        edited = grader(type="file_exists", path="app/config.yml")
        out = self.suite({"c": self.case(made=made, final=final, listed=listed, edited=edited)}).run("--scaffold", "--overlay", str(ov))[1]
        self.assertFinding(out, "WARN", "c / made", "重ねた状態")
        self.assertFinding(out, "WARN", "c / final", "重ねた状態")
        self.assertNoFinding(out, "c / listed", "でも合格します")  # config.yml existed before: it was only edited
        self.assertNoFinding(out, "c / edited", "でも合格します")
        self.assertFinding(out, "ERROR", "c / edited", "必ず落ちます")

    def test_overlay_does_not_write_through_a_scaffold_link(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        ov = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ov)
        (ov / "out").mkdir()
        (ov / "out" / "x.txt").write_text("lazy\n")
        case = self.case(g=grader(type="regex", pattern="ok"))
        case["fixture.sh"] = f"ln -s '{outside}' out\n"
        code, out = self.suite({"c": case}).run("--scaffold", "--overlay", str(ov))
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c", "--overlay")
        self.assertEqual(list(outside.iterdir()), [])
        # A linked directory in the overlay is copied through, so what it holds is checked too.
        real = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, real)
        (real / "x.txt").write_text("lazy\n")
        shutil.rmtree(ov / "out")
        (ov / "app").symlink_to(real)
        case["fixture.sh"] = f"mkdir -p app\nln -s '{outside}/x.txt' app/x.txt\n"
        code, out = self.suite({"c": case}).run("--scaffold", "--overlay", str(ov))
        self.assertFinding(out, "ERROR", "c", "--overlay の app/x.txt")
        self.assertEqual(list(outside.iterdir()), [])

    def test_skill_and_with_only_graders_are_left_out_of_the_score(self):
        skill = grader(type="tool_used", tool="Skill", input_match="build", min=0)
        with_only = grader(type="tool_used", tool="Read", min=0, arm="with-only")
        real = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="final")
        out = self.suite({"c": self.case(skill=skill, with_only=with_only, real=real)}).run("--scaffold")[1]
        self.assertNoFinding(out, "c", "でも 1.00 点")
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
        s = self.suite({"c": self.case(heavy=heavy, light=light)})
        self.assertFinding(s.run("--scaffold", "--threshold", "0.75")[1], "ERROR", "c", "0.75")   # 3 / (3 + 1), exactly at it
        self.assertNoFinding(s.run("--scaffold", "--threshold", "0.76")[1], "c", "でも 0.75")       # just above it
        half = grader(type="regex", pattern="done", weight=0.5)
        out = self.suite({"c": self.case(heavy=heavy, half=half)}).run("--scaffold", "--threshold", "0.8")[1]
        self.assertFinding(out, "ERROR", "c", "0.86")  # 3 / 3.5: weights need not be integers
        for bad in ("1.5", "-0.1"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                s.run("--threshold", bad)

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
        self.assertNoFinding(out, "c / g", "でも合格します")  # the shell's other variables do not reach the scaffold

    def test_scaffold_that_cannot_start_is_reported(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            code, out = self.suite({"c": self.case(g=grader(type="regex", pattern="ok"))}).run("--scaffold")
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c", "scaffold を起動できません")

    def test_unreadable_file_target_is_not_a_crash(self):
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws)
        (ws / "locked.txt").write_text("mode\n")
        g = audit.Grader("g", {"type": "regex", "target": {"source": "file", "path": "locked.txt"}, "pattern": "mode"}, "")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertFalse(audit.lazy_verdict(g, audit.Lazy(ws, [])))

    def test_scaffold_does_not_read_the_users_stdin(self):
        # The CLI gives the scaffold no stdin; one that reads it must not block on, or consume, the terminal.
        case = self.case(g=grader(type="regex", target={"source": "file", "path": "got.txt"}, pattern="typed"))
        case["fixture.sh"] = "if read -r line; then echo \"$line\" > got.txt; fi\n"
        s = self.suite({"c": case})
        r = subprocess.run([sys.executable, str(SCRIPT), str(s.dir), "--scaffold"], input="typed\n",
                           capture_output=True, text=True, timeout=60, check=False)
        self.assertNotIn("でも合格します", r.stdout)

    def test_scaffold_timeout_kills_what_it_started(self):
        marker = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, marker)
        case = self.case(g=grader(type="regex", pattern="ok"))
        case["fixture.sh"] = f"sleep 30 &\necho $! > '{marker}/pid'\nsleep 30\n"
        with mock.patch.object(audit, "SCAFFOLD_TIMEOUT", 1):
            code, out = self.suite({"c": case}).run("--scaffold")
        self.assertEqual(code, 1)
        self.assertFinding(out, "ERROR", "c", "1 秒で終わりません")
        pid = int((marker / "pid").read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail("the scaffold's background process outlived the audit")

    def test_scaffold_that_leaves_a_background_process_still_finishes(self):
        case = self.case(g=grader(type="regex", pattern="ok"))
        case["fixture.sh"] = "sleep 30 &\necho done\n"
        with mock.patch.object(audit, "SCAFFOLD_TIMEOUT", 10):
            code, out = self.suite({"c": case}).run("--scaffold")
        self.assertEqual(code, 0, out)
        self.assertNoFinding(out, "c", "秒で終わりません")

    def test_case_glob_limits_the_lazy_run_only(self):
        weak = grader(type="regex", target={"source": "file", "path": "app/config.yml"}, pattern="mode")
        out = self.suite({"c": self.case(weak=weak)}).run("--scaffold", "--case", "other*")[1]
        self.assertNoFinding(out, "c / weak", "でも合格します")


class RealSuite(unittest.TestCase):
    def test_finds_every_fault_planted_in_the_eval_fixture(self):
        # The skill's own eval cases grade replies on these four faults; the script must see them too.
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws)
        fixture = REPO / "plugins" / "skill-eval" / "evals" / "_fixture" / "myplug.sh"
        subprocess.run(["bash", str(fixture)], cwd=ws, check=True)
        (ws / "lazy").mkdir()
        shutil.copy(ws / "myplug" / "skills" / "report" / "templates" / "report.md", ws / "lazy")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = audit.main([str(ws / "myplug"), "--overlay", str(ws / "lazy"), "--case", "weekly"])
        out = buf.getvalue()
        self.assertEqual(code, 1, out)
        for expected in ("[WARN] weekly / skill-fired: プロンプトが /myplug:report", "[WARN] weekly: プロンプトに絶対日付",
                         "[WARN] off-topic / never: Skill の grader は既定で点数から外れます", "[WARN] weekly / has-result: 手を抜いた出力",
                         "[ERROR] weekly: 手を抜いた出力でも 1.00"):
            self.assertIn(expected, out)

    def test_harness_suite_has_no_errors(self):
        harness = REPO / "plugins" / "harness"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = audit.main([str(harness), "--scaffold"])
        self.assertEqual(code, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
