#!/usr/bin/env python3
"""Autonomous-run harness engine (stdlib only, Python 3.9+).

Subcommands:
  start              archive the previous run's state and start the clock
  check [--label L]  run the configured check, record the result in state/scores.jsonl
  status             print time left, scores and the stop rule
  stop-rule          print the stop-rule decision as JSON
  report             one line per archived run (for improving the harness itself)
  hook <event>       Claude Code hook entry point: stop | post-tool | session-start | pre-compact

Everything lives next to this file: harness.json (config) and state/ (gitignored).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT_TAIL_CHARS = 3000  # what a hook hands back to Claude; full output stays in state/


# ---------------------------------------------------------------- basics

def now() -> dt.datetime:
    # Tests override HARNESS_NOW so time-based rules are reproducible.
    fake = os.environ.get("HARNESS_NOW")
    return parse_time(fake) if fake else dt.datetime.now(dt.timezone.utc)


def parse_time(value: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:  # naive times are local wall-clock, like what a person types
        t = t.astimezone()
    return t


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    rows = []
    # errors="replace": a crash can cut the last line inside a multibyte char; that must not hide every row.
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a half-written last line after a crash
    return rows


class Harness:
    def __init__(self, root: Path = HERE):
        self.root = root
        self.state = root / "state"
        self.project = root.parent
        cfg_path = root / "harness.json"
        try:
            self.cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"harness: cannot read {cfg_path}: {e}")
        if self.cfg.get("mode") not in ("pass_fail", "score"):
            raise SystemExit('harness: harness.json "mode" must be "pass_fail" or "score"')
        # final_counts only matters when scores are compared; a pass_fail harness may leave it null.
        if self.cfg["mode"] == "score" and self.cfg.get("final_counts") not in ("last", "best"):
            raise SystemExit('harness: harness.json "final_counts" must be "last" or "best" when mode is "score"')
        if self.cfg.get("direction", "max") not in ("max", "min"):
            raise SystemExit('harness: harness.json "direction" must be "max" or "min"')
        ignore = self.cfg.get("label_ignore", [])
        if not (isinstance(ignore, list) and all(isinstance(x, str) and x for x in ignore)):
            raise SystemExit('harness: harness.json "label_ignore" must be a list of paths')
        self.state.mkdir(exist_ok=True)

    # ---- small persistence helpers (append-only JSONL so a crash never loses history)

    def _jsonl(self, name: str) -> list[dict]:
        return read_jsonl(self.state / name)

    def _append(self, name: str, row: dict) -> None:
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        with (self.state / name).open("a+b") as f:
            if f.seek(0, os.SEEK_END):
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":  # a crash left a cut line; without this the new row is glued to it and lost
                    line = b"\n" + line
            f.write(line)

    def scores(self) -> list[dict]:
        return self._jsonl("scores.jsonl")

    def events(self) -> list[dict]:
        return self._jsonl("events.jsonl")

    # ---- time

    def started_at(self) -> dt.datetime | None:
        p = self.state / "started_at"
        return parse_time(p.read_text().strip()) if p.exists() else None

    def ensure_started(self) -> None:
        p = self.state / "started_at"
        if not p.exists():
            p.write_text(now().isoformat())

    def deadline(self) -> dt.datetime | None:
        if self.cfg.get("deadline"):
            return parse_time(self.cfg["deadline"])
        limit = self.cfg.get("time_limit_minutes")
        start = self.started_at()
        if limit and start:
            return start + dt.timedelta(minutes=limit)
        return None

    def remaining_seconds(self) -> float | None:
        d = self.deadline()
        return None if d is None else (d - now()).total_seconds()

    # ---- scores

    def _key(self, score: float) -> float:
        # Internally "bigger is better" so min-direction tasks reuse the same math.
        return score if self.cfg.get("direction", "max") == "max" else -score

    def scored(self) -> list[dict]:
        return [r for r in self.scores() if r.get("score") is not None]

    def best(self) -> dict | None:
        if self.cfg["mode"] == "pass_fail":
            passed = [r for r in self.scores() if r.get("passed")]
            return passed[-1] if passed else None
        rows = [r for r in self.scored() if r.get("passed")]
        return max(rows, key=lambda r: self._key(r["score"])) if rows else None

    def last_improvement_time(self) -> dt.datetime | None:
        """When the best result last got better (pass_fail: first check of the run)."""
        rows = self.scores()
        if not rows:
            return None
        if self.cfg["mode"] == "pass_fail":
            passed = [r for r in rows if r.get("passed")]
            return parse_time((passed[-1] if passed else rows[0])["ts"])
        best_key, best_ts = -math.inf, None
        for r in rows:
            if r.get("passed") and r.get("score") is not None and self._key(r["score"]) > best_key:
                best_key, best_ts = self._key(r["score"]), r["ts"]
        return parse_time(best_ts if best_ts else rows[0]["ts"])

    def run_seconds(self) -> float | None:
        if self.cfg.get("run_seconds"):
            return float(self.cfg["run_seconds"])
        durations = [r["duration_s"] for r in self.scores() if r.get("duration_s")]
        return statistics.median(durations) if durations else None


# ---------------------------------------------------------------- stop rule

def continuation_value(samples: list[float], runs_left: int) -> float:
    """Expected final result if we keep re-running and play optimally.

    Optimal stopping when the LAST run is what counts and each run is a draw
    from the empirical distribution `samples` (bigger is better):
      C_1 = E[X]            (one run left: we must accept it)
      C_k = E[max(X, C_{k-1})]
    Stop now iff current >= C_k. C_k shrinks as k shrinks, so the bar drops
    near the deadline, which is the behaviour described for ISUCON 2026.
    """
    if runs_left <= 0:
        return -math.inf
    c = statistics.fmean(samples)
    for _ in range(runs_left - 1):
        nxt = statistics.fmean(max(x, c) for x in samples)
        if nxt == c:  # fixed point: further k give the same value, and k can be in the millions
            break
        c = nxt
    return c


def stop_rule(h: Harness) -> dict:
    cfg = h.cfg
    out: dict = {"should_stop": False}
    rem = h.remaining_seconds()
    if rem is not None and rem <= 0:
        return {"should_stop": True, "reason": "deadline passed"}

    if cfg["mode"] == "pass_fail":
        rows = h.scores()
        if rows and rows[-1].get("passed"):
            return {"should_stop": True, "reason": "latest check passed"}
        out["reason"] = "latest check has not passed"
        return out

    rows = [r for r in h.scored() if r.get("passed")]
    if not rows:
        out["reason"] = "no passing scored run yet"
        return out
    counts_last = cfg.get("final_counts", "last") == "last"
    newest = h.scores()[-1]
    if counts_last and not (newest.get("passed") and newest.get("score") is not None):
        # The newest run is what counts, and it has no passing score: an older good run must not end the task.
        out["reason"] = "latest check failed or printed no score; with final_counts=last it would be the final result"
        return out
    target = cfg.get("target_score")
    latest = rows[-1]
    if target is not None and h._key(latest["score"]) >= h._key(target):
        return {"should_stop": True, "reason": f"latest score {latest['score']} reached target {target}"}

    if not counts_last:
        # The best run counts, so another run can never hurt: keep going until the clock runs out.
        out["reason"] = "final_counts=best: keep improving until the deadline"
        return out
    if rem is None:
        out["reason"] = "no deadline set: cannot compute runs left"
        return out

    run_s = h.run_seconds()
    margin = float(cfg.get("safety_margin_minutes", 5)) * 60
    if not run_s:
        out["reason"] = "run duration unknown"
        return out
    runs_left = max(0, math.floor((rem - margin) / run_s))
    same = [h._key(r["score"]) for r in rows if r.get("label") == latest.get("label")]
    out.update(runs_left=runs_left, samples=len(same), label=latest.get("label"))
    if runs_left == 0:
        out.update(should_stop=True, reason="no time for another run")
        return out
    min_samples = int(cfg.get("min_samples", 3))
    if len(same) < min_samples:
        out["reason"] = f"only {len(same)}/{min_samples} runs of this configuration: measure variance first"
        return out
    threshold = continuation_value(same, runs_left)
    out["threshold"] = threshold if cfg.get("direction", "max") == "max" else -threshold
    if h._key(latest["score"]) >= threshold:
        out.update(should_stop=True, reason=f"latest {latest['score']} >= expected value of continuing")
    else:
        out["reason"] = f"latest {latest['score']} < expected value of continuing"
    return out


# ---------------------------------------------------------------- check

def git_label(project: Path, ignore: list[str]) -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=project,
                             capture_output=True, text=True, timeout=10)
        if sha.returncode != 0:
            return "no-git"
        # harness/ is excluded: notes in decisions.md must not split one configuration in two.
        # label_ignore does the same for tracked files the check itself rewrites (dumps, generated output):
        # otherwise every run gets a new label and no label ever reaches min_samples.
        # The diff hash keeps different uncommitted experiments on one commit from pooling as one configuration.
        diff = subprocess.run(["git", "diff", "HEAD", "--binary", "--no-color", "--no-ext-diff", "--no-textconv",
                               "--", ".", ":!harness", *(":(exclude)" + p for p in ignore)],
                              cwd=project, capture_output=True, timeout=10)
        if diff.returncode != 0:
            # An empty stdout here would read as "clean" and pool every uncommitted experiment under one label.
            err = diff.stderr.decode("utf-8", errors="replace").strip()
            raise SystemExit(f'harness: git diff failed (check harness.json "label_ignore"): {err}')
        dirty = "-dirty-" + hashlib.sha1(diff.stdout).hexdigest()[:8] if diff.stdout.strip() else ""
        return sha.stdout.strip() + dirty
    except (OSError, subprocess.TimeoutExpired):
        return "no-git"


def parse_score(output: str) -> float | None:
    score = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("SCORE="):
            try:
                value = float(line[len("SCORE="):])
            except ValueError:
                continue  # a malformed line is ignored; the last valid one wins
            if math.isfinite(value):  # inf would beat every stop threshold; nan compares false with all
                score = value
    return score


def run_check(h: Harness, label: str | None = None) -> dict:
    cmd = h.cfg.get("check_command")
    if not cmd:
        raise SystemExit('harness: harness.json has no "check_command"')
    timeout = float(h.cfg.get("check_timeout_seconds", 1800))
    # Before the run: the agent may commit while a long check runs.
    label = label or git_label(h.project, h.cfg.get("label_ignore", []))
    start = now()
    t0 = dt.datetime.now().timestamp()
    timed_out = False
    try:
        p = subprocess.run(cmd, shell=True, cwd=h.project, capture_output=True, timeout=timeout)
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out, err, code, timed_out = e.stdout, e.stderr, 124, True
    # Decode ourselves: one invalid or cut UTF-8 byte must not crash the check and lose the result.
    output = ((out or b"") + (err or b"")).decode("utf-8", errors="replace")
    if timed_out:
        output += f"\n[harness] check timed out after {timeout:.0f}s"
    duration = dt.datetime.now().timestamp() - t0
    n = len(h.scores()) + 1
    (h.state / "checks").mkdir(exist_ok=True)
    while True:  # exclusive create: a concurrent check may already own this number
        log = h.state / "checks" / f"{n:04d}.log"
        try:
            with log.open("x", encoding="utf-8") as f:
                f.write(output)
            break
        except FileExistsError:
            n += 1
    row = {"ts": start.isoformat(), "n": n, "label": label,
           "passed": code == 0, "exit_code": code, "score": parse_score(output),
           "duration_s": round(duration, 2), "log": str(log.relative_to(h.root))}
    h._append("scores.jsonl", row)
    row["output_tail"] = output[-OUTPUT_TAIL_CHARS:]
    return row


# ---------------------------------------------------------------- status text

def fmt_minutes(seconds: float | None) -> str:
    return "なし" if seconds is None else f"{seconds / 60:.0f} 分"


def status_text(h: Harness) -> str:
    rows = h.scores()
    best = h.best()
    rule = stop_rule(h)
    lines = [
        f"[harness] mode={h.cfg['mode']} 残り時間={fmt_minutes(h.remaining_seconds())} check 実行回数={len(rows)}",
    ]
    if rows:
        last = rows[-1]
        lines.append(f"直近: #{last['n']} passed={last['passed']} score={last.get('score')} label={last.get('label')}")
    if best:
        lines.append(f"最良: #{best['n']} score={best.get('score')} label={best.get('label')}")
    lines.append("停止判定: " + json.dumps(rule, ensure_ascii=False))
    result = last_run_result(h)
    if result:
        lines.append(f"最新の claude -p の結果: is_error={result.get('is_error')} "
                     f"cost_usd={result.get('total_cost_usd')} {str(result.get('result', ''))[:300]}")
    return "\n".join(lines)


def last_run_result(h: Harness) -> dict | None:
    """The final `result` event of the newest run.sh log, if that run has finished."""
    runs = sorted((h.state / "runs").glob("*.jsonl")) if (h.state / "runs").exists() else []
    if not runs:
        return None
    lines = runs[-1].read_text(encoding="utf-8", errors="replace").strip().splitlines()
    try:
        last = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        return None
    return last if last.get("type") == "result" else None


# ---------------------------------------------------------------- hooks

def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _context(event: str, text: str) -> None:
    _emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def _fired(h: Harness, key: str) -> bool:
    return any(e.get("key") == key for e in h.events())


def _fire(h: Harness, key: str) -> None:
    h._append("events.jsonl", {"ts": now().isoformat(), "key": key})


def hook_stop(h: Harness, payload: dict) -> int:
    # Waiting for a background benchmark/subagent to wake us is not "done".
    if any(t.get("status") in ("running", "pending") for t in payload.get("background_tasks") or []):
        return 0
    if h.cfg["mode"] == "pass_fail" and h.cfg.get("stop_runs_check", True):
        rem = h.remaining_seconds()
        if rem is not None and rem <= 0:
            return 0
        row = run_check(h)
        if row["passed"]:
            return 0
        _emit({"decision": "block", "reason":
               f"check が失敗しています (exit {row['exit_code']}, 全文は harness/{row['log']})。"
               f"直してから終了してください。\n--- 出力の末尾 ---\n{row['output_tail']}"})
        return 0
    rule = stop_rule(h)
    if rule["should_stop"]:
        return 0
    _emit({"decision": "block", "reason":
           "まだ終了条件を満たしていません。harness/CONDITIONS.md の目標に向けて作業を続けてください。"
           "直前の作業が進展だったかを harness/SYSTEM.md の「進んでいるかの確かめ」で判断してから、次の手を選んでください。\n"
           + status_text(h)})
    return 0


def hook_post_tool(h: Harness, payload: dict) -> int:
    # Subagent tool calls (the auditor's Reads) fire this hook too; a one-shot nudge spent there never reaches the main agent.
    if payload.get("agent_id"):
        return 0
    notes = []
    rem = h.remaining_seconds()
    final_min = h.cfg.get("final_check_minutes")
    if rem is not None and final_min and 0 < rem <= final_min * 60 and not _fired(h, "final-check"):
        _fire(h, "final-check")
        notes.append(f"残り {rem / 60:.0f} 分です。新しい最適化はやめ、harness/SYSTEM.md の「最終確認」を実行してください。"
                     " auditor サブエージェントに最終監査を依頼してください。")
    stall_min = h.cfg.get("stall_minutes")
    since = h.last_improvement_time()
    if stall_min and since:
        bucket = int((now() - since).total_seconds() // (stall_min * 60))
        key = f"stall:{since.isoformat()}:{bucket}"
        if bucket >= 1 and not _fired(h, key):
            _fire(h, key)
            notes.append(f"{bucket * stall_min} 分以上、結果が改善していません。auditor サブエージェントを起動し、"
                         "harness/decisions.md を harness/checklist.md と突き合わせて見直させてください。")
    if notes:
        _context("PostToolUse", "\n".join(notes) + "\n" + status_text(h))
    return 0


def hook_session_start(h: Harness, payload: dict) -> int:
    h.ensure_started()
    text = [status_text(h), "作業条件は harness/CONDITIONS.md、判断の記録は harness/decisions.md にあります。"]
    if payload.get("source") in ("compact", "resume"):
        dec = h.root / "decisions.md"
        if dec.exists():
            tail = dec.read_text(encoding="utf-8").splitlines()[-40:]
            text.append("--- decisions.md の末尾 ---\n" + "\n".join(tail))
    _context("SessionStart", "\n".join(text))
    return 0


def hook_pre_compact(h: Harness, payload: dict) -> int:
    # PreCompact stdout becomes instructions for the compaction summary.
    print("要約には次を必ず残す: harness/CONDITIONS.md の目標と禁止事項、現在の最良の構成（commit と score）、"
          "試して効かなかった手とその根拠、検証中の仮説、残り時間。詳細は harness/decisions.md を読み直せば復元できる。")
    return 0


HOOKS = {"stop": hook_stop, "post-tool": hook_post_tool,
         "session-start": hook_session_start, "pre-compact": hook_pre_compact}


def run_hook(event: str) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    try:
        return HOOKS[event](Harness(), payload)
    except (SystemExit, Exception) as e:
        # Never let a bad config or a harness bug wedge the agent, and leave a trace for the post-mortem:
        # a headless run may never show hook stderr.
        msg = str(e) if isinstance(e, SystemExit) else f"harness hook {event} failed: {e!r}"
        try:
            (HERE / "state").mkdir(exist_ok=True)
            with (HERE / "state" / "hook-errors.log").open("a", encoding="utf-8") as f:
                f.write(f"{now().isoformat()} {event}: {msg}\n")
        except OSError:
            pass
        print(msg, file=sys.stderr)
        return 1


# ---------------------------------------------------------------- start / report

def validate_setup(h: Harness) -> None:
    """Refuse to launch a run whose safety net would silently be missing.

    A crashing Stop hook lets the agent stop (Stop has no fail-closed option), and `claude -p`
    silently ignores a settings file that fails validation, so both must be caught before launch.
    """
    # Checked by type, not via deadline(): time_limit_minutes is only used once started_at exists.
    limit = h.cfg.get("time_limit_minutes")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float))):
        raise SystemExit('harness: harness.json "time_limit_minutes" must be a number or null')
    try:
        h.deadline()
    except (ValueError, TypeError, AttributeError) as e:
        raise SystemExit(f"harness: harness.json deadline is not an ISO 8601 time: {e}")
    if not h.cfg.get("check_command"):
        raise SystemExit('harness: harness.json has no "check_command"')
    path = h.root / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"harness: cannot read {path}: {e}")
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    hooks = hooks if isinstance(hooks, dict) else {}
    missing = [ev for ev in ("SessionStart", "PostToolUse", "Stop", "PreCompact") if not hooks.get(ev)]
    if missing:
        raise SystemExit(f"harness: {path} has no hook for {', '.join(missing)}")
    agents_path = h.root / "agents.json"
    try:
        agents = json.loads(agents_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"harness: cannot read {agents_path}: {e}")
    if not (isinstance(agents, dict) and isinstance(agents.get("auditor"), dict)):
        raise SystemExit(f"harness: {agents_path} must define an \"auditor\" agent")


def start(h: Harness) -> None:
    validate_setup(h)
    keep = {".gitignore"}
    old = [p for p in h.state.iterdir() if p.name not in keep and p.name != "archive"]
    if old:
        dest = h.state / "archive" / now().strftime("%Y%m%d-%H%M%S")
        dest.mkdir(parents=True)
        for p in old:
            shutil.move(str(p), str(dest / p.name))
    (h.state / "started_at").write_text(now().isoformat())
    print(f"[harness] started at {now().isoformat()}, deadline {h.deadline()}")


def report(h: Harness) -> None:
    arch = h.state / "archive"
    runs = sorted(arch.iterdir()) if arch.exists() else []
    print("run\tchecks\tpassed\tbest\tlast")
    for d in runs + [h.state]:
        rows = read_jsonl(d / "scores.jsonl")
        scored = [r["score"] for r in rows if r.get("passed") and r.get("score") is not None]
        pick = max if h.cfg.get("direction", "max") == "max" else min
        best = pick(scored) if scored else "-"
        last = rows[-1].get("score") if rows else "-"
        name = "current" if d == h.state else d.name
        print(f"{name}\t{len(rows)}\t{sum(1 for r in rows if r.get('passed'))}\t{best}\t{last}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start")
    c = sub.add_parser("check")
    c.add_argument("--label")
    sub.add_parser("status")
    sub.add_parser("stop-rule")
    sub.add_parser("report")
    k = sub.add_parser("hook")
    k.add_argument("event", choices=sorted(HOOKS))
    a = ap.parse_args(argv)

    if a.cmd == "hook":
        return run_hook(a.event)
    h = Harness()
    if a.cmd == "start":
        start(h)
    elif a.cmd == "check":
        row = run_check(h, a.label)
        print(row["output_tail"])
        print(f"[harness] #{row['n']} passed={row['passed']} score={row['score']} "
              f"label={row['label']} duration={row['duration_s']}s log=harness/{row['log']}")
        print(status_text(h))
        return 0 if row["passed"] else 1
    elif a.cmd == "status":
        print(status_text(h))
    elif a.cmd == "stop-rule":
        print(json.dumps(stop_rule(h), ensure_ascii=False, indent=2))
    elif a.cmd == "report":
        report(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
