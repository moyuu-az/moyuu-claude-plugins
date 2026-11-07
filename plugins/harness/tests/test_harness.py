"""Tests for the harness engine template. Run: python3 -m unittest discover -s plugins/harness/tests"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "skills" / "build" / "templates"
ENGINE = TEMPLATES / "harness" / "harness.py"

spec = importlib.util.spec_from_file_location("harness_engine", ENGINE)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

T0 = "2026-11-07T10:00:00+00:00"


def at(minutes: float) -> str:
    return engine.parse_time(T0).__add__(engine.dt.timedelta(minutes=minutes)).isoformat()


class Project:
    """A throwaway project with the harness template copied in, like the skill does."""

    def __init__(self, **cfg):
        self.dir = Path(tempfile.mkdtemp())
        shutil.copytree(TEMPLATES / "harness", self.dir / "harness")
        self.root = self.dir / "harness"
        base = json.loads((self.root / "harness.json").read_text())
        base.update(cfg)
        (self.root / "harness.json").write_text(json.dumps(base))

    def write_scores(self, rows):
        (self.root / "state").mkdir(exist_ok=True)
        with (self.root / "state" / "scores.jsonl").open("w") as f:
            for i, r in enumerate(rows, 1):
                r = {"n": i, "passed": True, "label": "a", "duration_s": 60, **r}
                f.write(json.dumps(r) + "\n")

    def start_at(self, ts=T0):
        (self.root / "state" / "started_at").write_text(ts)

    def run(self, *args, stdin="", now=T0):
        env = {**os.environ, "HARNESS_NOW": now}
        return subprocess.run([sys.executable, str(self.root / "harness.py"), *args], input=stdin,
                              capture_output=True, text=True, cwd=self.dir, env=env, timeout=60)

    def hook(self, event, payload=None, now=T0):
        p = self.run("hook", event, stdin=json.dumps(payload or {}), now=now)
        out = json.loads(p.stdout) if p.stdout.strip().startswith("{") else None
        return p, out

    def harness(self):
        return engine.Harness(self.root)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Base(unittest.TestCase):
    def project(self, **cfg):
        p = Project(**cfg)
        self.addCleanup(p.cleanup)
        return p

    def setUp(self):
        os.environ["HARNESS_NOW"] = T0
        self.addCleanup(os.environ.pop, "HARNESS_NOW", None)


class ContinuationValue(unittest.TestCase):
    def test_no_runs_left_means_stop_at_any_score(self):
        self.assertEqual(engine.continuation_value([1, 2, 3], 0), -math.inf)

    def test_one_run_left_is_the_mean(self):
        self.assertAlmostEqual(engine.continuation_value([1, 2, 3], 1), 2.0)

    def test_two_runs_left_matches_hand_calculation(self):
        # C1 = 2; C2 = mean(max(x, 2)) = (2 + 2 + 3) / 3
        self.assertAlmostEqual(engine.continuation_value([1, 2, 3], 2), 7 / 3)

    def test_bar_rises_with_more_runs_but_never_above_best_sample(self):
        s = [474987, 520000, 580000, 629638]
        values = [engine.continuation_value(s, k) for k in range(1, 12)]
        self.assertEqual(values, sorted(values))
        self.assertLessEqual(values[-1], max(s))

    def test_constant_samples(self):
        self.assertAlmostEqual(engine.continuation_value([5, 5, 5], 7), 5)


class StopRule(Base):
    def test_deadline_passed_stops(self):
        p = self.project(mode="score", time_limit_minutes=60)
        p.write_scores([{"ts": at(1), "score": 1}])
        p.start_at()
        os.environ["HARNESS_NOW"] = at(61)
        self.assertTrue(engine.stop_rule(p.harness())["should_stop"])

    def test_no_scores_keeps_going(self):
        p = self.project(mode="score", time_limit_minutes=60)
        p.start_at()
        r = engine.stop_rule(p.harness())
        self.assertFalse(r["should_stop"])

    def test_failed_runs_do_not_count_as_scores(self):
        p = self.project(mode="score", time_limit_minutes=60)
        p.write_scores([{"ts": at(1), "score": 999, "passed": False}])
        p.start_at()
        self.assertEqual(engine.stop_rule(p.harness())["reason"], "no passing scored run yet")

    def test_target_reached_stops(self):
        p = self.project(mode="score", target_score=100)
        p.write_scores([{"ts": at(1), "score": 100}])
        self.assertTrue(engine.stop_rule(p.harness())["should_stop"])

    def test_target_for_min_direction(self):
        p = self.project(mode="score", target_score=100, direction="min")
        p.write_scores([{"ts": at(1), "score": 120}])
        self.assertFalse(engine.stop_rule(p.harness())["should_stop"])
        p.write_scores([{"ts": at(1), "score": 90}])
        self.assertTrue(engine.stop_rule(p.harness())["should_stop"])

    def test_best_counts_never_stops_early(self):
        p = self.project(mode="score", final_counts="best", time_limit_minutes=60)
        p.write_scores([{"ts": at(1), "score": 10}] * 5)
        p.start_at()
        self.assertFalse(engine.stop_rule(p.harness())["should_stop"])

    def test_needs_min_samples_of_same_label(self):
        p = self.project(mode="score", time_limit_minutes=600, min_samples=3)
        p.write_scores([{"ts": at(1), "score": 10, "label": "a"},
                        {"ts": at(2), "score": 10, "label": "a"},
                        {"ts": at(3), "score": 10, "label": "b"}])
        p.start_at()
        r = engine.stop_rule(p.harness())
        self.assertFalse(r["should_stop"])
        self.assertEqual(r["samples"], 1)

    def test_good_latest_with_few_runs_left_stops(self):
        # 60 min left, 5 min margin, 10 min per run -> 5 runs left.
        p = self.project(mode="score", time_limit_minutes=60, run_seconds=600)
        p.write_scores([{"ts": at(0), "score": s} for s in (100, 200, 300, 400)])
        p.start_at()
        r = engine.stop_rule(p.harness())
        self.assertEqual(r["runs_left"], 5)
        self.assertTrue(r["should_stop"], r)

    def test_mediocre_latest_with_many_runs_left_continues(self):
        p = self.project(mode="score", time_limit_minutes=600, run_seconds=600)
        p.write_scores([{"ts": at(0), "score": s} for s in (400, 300, 100, 200)])
        p.start_at()
        r = engine.stop_rule(p.harness())
        self.assertFalse(r["should_stop"], r)
        self.assertGreater(r["threshold"], 200)

    def test_bar_drops_near_deadline(self):
        p = self.project(mode="score", time_limit_minutes=600, run_seconds=600)
        p.write_scores([{"ts": at(0), "score": s} for s in (400, 100, 250, 300)])
        p.start_at()
        early = engine.stop_rule(p.harness())
        os.environ["HARNESS_NOW"] = at(580)  # 20 min left, 5 min margin -> 1 run left, bar = mean 262.5
        late = engine.stop_rule(p.harness())
        self.assertFalse(early["should_stop"], early)
        self.assertEqual(late["runs_left"], 1)
        self.assertTrue(late["should_stop"], late)

    def test_no_time_for_another_run_stops(self):
        p = self.project(mode="score", time_limit_minutes=60, run_seconds=600)
        p.write_scores([{"ts": at(0), "score": 1}] * 3)
        p.start_at()
        os.environ["HARNESS_NOW"] = at(50)  # 10 min left, 5 margin -> 0 runs
        self.assertTrue(engine.stop_rule(p.harness())["should_stop"])

    def test_pass_fail(self):
        p = self.project(mode="pass_fail")
        p.write_scores([{"ts": at(0), "passed": False}])
        self.assertFalse(engine.stop_rule(p.harness())["should_stop"])
        p.write_scores([{"ts": at(0), "passed": False}, {"ts": at(1), "passed": True}])
        self.assertTrue(engine.stop_rule(p.harness())["should_stop"])

    def test_invalid_mode_is_rejected(self):
        p = self.project(mode="whatever")
        with self.assertRaises(SystemExit):
            p.harness()


class Check(Base):
    def test_records_score_exit_code_and_log(self):
        p = self.project(check_command="echo noise; echo SCORE=12.5; echo SCORE=bad")
        r = p.run("check")
        self.assertEqual(r.returncode, 0, r.stderr)
        row = json.loads((p.root / "state" / "scores.jsonl").read_text().splitlines()[-1])
        self.assertTrue(row["passed"])
        self.assertEqual(row["score"], 12.5)
        self.assertEqual(row["label"], "no-git")
        self.assertIn("noise", (p.root / row["log"]).read_text())

    def test_failure_propagates_exit_code(self):
        p = self.project(check_command="echo broken >&2; exit 3")
        r = p.run("check")
        self.assertEqual(r.returncode, 1)
        row = json.loads((p.root / "state" / "scores.jsonl").read_text())
        self.assertFalse(row["passed"])
        self.assertEqual(row["exit_code"], 3)

    def test_timeout_is_recorded_as_failure(self):
        p = self.project(check_command="sleep 5", check_timeout_seconds=0.5)
        r = p.run("check")
        self.assertEqual(r.returncode, 1)
        row = json.loads((p.root / "state" / "scores.jsonl").read_text())
        self.assertEqual(row["exit_code"], 124)

    def test_default_check_sh_fails_until_written(self):
        p = self.project()
        self.assertEqual(p.run("check").returncode, 1)

    def test_label_uses_git_commit_and_ignores_harness_dir(self):
        p = self.project(check_command="true")
        git = lambda *a: subprocess.run(["git", *a], cwd=p.dir, check=True, capture_output=True)
        git("init", "-q")
        git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        (p.root / "decisions.md").write_text("note")  # tracked, but must not mark the config dirty
        p.run("check")
        row = json.loads((p.root / "state" / "scores.jsonl").read_text())
        self.assertNotIn("dirty", row["label"])
        self.assertNotEqual(row["label"], "no-git")

    def test_truncated_jsonl_line_is_skipped(self):
        p = self.project(check_command="true")
        (p.root / "state" / "scores.jsonl").write_text('{"n": 1, "ts": "%s", "passed": true}\n{"n": 2, "ts' % T0)
        self.assertEqual(len(p.harness().scores()), 1)


class StopHook(Base):
    def test_blocks_while_running_and_reports_status(self):
        p = self.project(mode="score", time_limit_minutes=60)
        p.start_at()
        proc, out = p.hook("stop")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["decision"], "block")
        self.assertIn("残り時間", out["reason"])

    def test_allows_when_background_task_will_wake_session(self):
        p = self.project(mode="score", time_limit_minutes=60)
        p.start_at()
        proc, out = p.hook("stop", {"background_tasks": [{"id": "1", "status": "running"}]})
        self.assertIsNone(out)

    def test_allows_after_deadline(self):
        p = self.project(mode="score", time_limit_minutes=60)
        p.start_at()
        _, out = p.hook("stop", now=at(61))
        self.assertIsNone(out)

    def test_pass_fail_runs_check_and_blocks_with_output(self):
        p = self.project(mode="pass_fail", check_command="echo 'FAILED test_x' ; exit 1")
        _, out = p.hook("stop")
        self.assertEqual(out["decision"], "block")
        self.assertIn("FAILED test_x", out["reason"])

    def test_pass_fail_allows_when_check_passes(self):
        p = self.project(mode="pass_fail", check_command="true")
        _, out = p.hook("stop")
        self.assertIsNone(out)

    def test_broken_config_fails_open_with_message(self):
        p = self.project()
        (p.root / "harness.json").write_text("{not json")
        proc, out = p.hook("stop")
        self.assertEqual(proc.returncode, 1)
        self.assertIsNone(out)
        self.assertIn("cannot read", proc.stderr)


class PostToolHook(Base):
    def test_stall_nudge_fires_once_per_period(self):
        p = self.project(mode="score", stall_minutes=30, final_check_minutes=None)
        p.write_scores([{"ts": at(0), "score": 10}, {"ts": at(5), "score": 9}])
        _, out = p.hook("post-tool", now=at(20))
        self.assertIsNone(out)
        _, out = p.hook("post-tool", now=at(31))
        self.assertIn("auditor", out["hookSpecificOutput"]["additionalContext"])
        _, out = p.hook("post-tool", now=at(40))
        self.assertIsNone(out)
        _, out = p.hook("post-tool", now=at(61))
        self.assertIn("60 分以上", out["hookSpecificOutput"]["additionalContext"])

    def test_improvement_resets_stall_clock(self):
        p = self.project(mode="score", stall_minutes=30, final_check_minutes=None)
        p.write_scores([{"ts": at(0), "score": 10}, {"ts": at(25), "score": 11}])
        _, out = p.hook("post-tool", now=at(40))
        self.assertIsNone(out)

    def test_final_check_nudge_fires_once(self):
        p = self.project(mode="score", time_limit_minutes=120, final_check_minutes=30, stall_minutes=None)
        p.start_at()
        _, out = p.hook("post-tool", now=at(80))
        self.assertIsNone(out)
        _, out = p.hook("post-tool", now=at(95))
        self.assertIn("最終確認", out["hookSpecificOutput"]["additionalContext"])
        _, out = p.hook("post-tool", now=at(100))
        self.assertIsNone(out)


class SessionHooks(Base):
    def test_session_start_sets_clock_once_and_restores_notes_after_compact(self):
        p = self.project(mode="score", time_limit_minutes=60)
        (p.root / "decisions.md").write_text("## 10:30 indexes\n- 結果: #3 score=500\n")
        p.hook("session-start", {"source": "startup"}, now=at(0))
        p.hook("session-start", {"source": "startup"}, now=at(10))
        self.assertEqual((p.root / "state" / "started_at").read_text(), T0)
        _, out = p.hook("session-start", {"source": "compact"}, now=at(30))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("indexes", ctx)
        self.assertIn("残り時間=30 分", ctx)

    def test_pre_compact_prints_summary_instructions(self):
        p = self.project()
        proc, _ = p.hook("pre-compact", {"trigger": "auto"})
        self.assertIn("decisions.md", proc.stdout)


class StartAndReport(Base):
    def test_start_archives_previous_run(self):
        p = self.project(mode="score")
        p.write_scores([{"ts": at(0), "score": 10}])
        r = p.run("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        archives = list((p.root / "state" / "archive").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "scores.jsonl").exists())
        self.assertFalse((p.root / "state" / "scores.jsonl").exists())
        self.assertTrue((p.root / "state" / ".gitignore").exists())
        rep = p.run("report").stdout
        self.assertIn(archives[0].name, rep)


class Status(Base):
    def test_status_shows_failed_headless_run(self):
        p = self.project()
        runs = p.root / "state" / "runs"
        runs.mkdir(parents=True)
        (runs / "20261107-100000.jsonl").write_text(
            '{"type":"system"}\n{"type":"result","is_error":true,"total_cost_usd":0,'
            '"result":"Failed to authenticate"}\n')
        out = p.run("status").stdout
        self.assertIn("is_error=True", out)
        self.assertIn("Failed to authenticate", out)

    def test_status_ignores_unfinished_run_log(self):
        p = self.project()
        runs = p.root / "state" / "runs"
        runs.mkdir(parents=True)
        (runs / "a.jsonl").write_text('{"type":"system"}\n{"type":"assist')
        r = p.run("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("claude -p", r.stdout)


class TemplateFiles(unittest.TestCase):
    def test_settings_json_is_valid_and_protects_evaluator(self):
        s = json.loads((TEMPLATES / "harness" / "settings.json").read_text())
        deny = s["permissions"]["deny"]
        for f in ("check.sh", "harness.py", "harness.json", "settings.json", "CONDITIONS.md"):
            self.assertIn(f"Edit(./harness/{f})", deny)
        for event, sub in (("Stop", "stop"), ("PostToolUse", "post-tool"),
                           ("SessionStart", "session-start"), ("PreCompact", "pre-compact")):
            self.assertIn(f"hook {sub}", s["hooks"][event][0]["hooks"][0]["command"])
            self.assertIn(sub, engine.HOOKS)

    def test_scripts_are_executable(self):
        for f in ("run.sh", "check.sh", "harness.py"):
            self.assertTrue(os.access(TEMPLATES / "harness" / f, os.X_OK), f)


if __name__ == "__main__":
    unittest.main()
