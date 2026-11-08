#!/usr/bin/env python3
"""Audit a `claude plugin eval` suite: graders that pass without the work, and pitfalls seen in real runs.

Usage:
  audit.py [PLUGIN_DIR] [--case GLOB] [--scaffold] [--overlay DIR]

It reads the suite and, for every case, scores the free graders (regex, tool_used, tool_order, file_exists)
against a "lazy" run: Claude made no tool call, said nothing, and the workspace holds only what the
scaffold made, plus --overlay DIR copied on top as files Claude created. A grader that passes there
does not show the work was done. llm and baseline graders need a judge and are reported as unknown.

Exit status: 1 if any ERROR finding, else 0. Only the standard library is used.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
FREE = {"regex", "tool_used", "tool_order", "file_exists"}


# ---------------------------------------------------------------- a YAML subset (no PyYAML on most hosts)

class Unsupported(ValueError):
    pass


def _scalar(s: str):
    s = s.strip()
    if not s:
        return None
    if s[0] == "'":
        end = 1
        while True:
            end = s.find("'", end)
            if end < 0:
                raise Unsupported(f"閉じていない引用符: {s}")
            if s[end + 1:end + 2] == "'":
                end += 2
                continue
            return s[1:end].replace("''", "'")
    if s[0] == '"':
        m = re.match(r'"(?:[^"\\]|\\.)*"', s)
        if not m:
            raise Unsupported(f"閉じていない引用符: {s}")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return m.group(0)[1:-1]
    if s[0] == "[":
        if not s.endswith("]"):
            raise Unsupported(f"複数行のリスト: {s}")
        inner = s[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    s = re.split(r"\s+#", s, maxsplit=1)[0].strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d*", s):
        return float(s)
    return {"true": True, "false": False, "null": None, "~": None}.get(s, s)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(lines: list[str], i: int, indent: int):
    out: dict = {}
    while i < len(lines) and _indent(lines[i]) >= indent:
        s = lines[i].strip()
        key, sep, rest = s.partition(":")
        if not sep or s.startswith("- "):
            raise Unsupported(f"読めない行: {s}")
        key, rest = key.strip(), rest.strip()
        i += 1
        if rest:
            out[key] = _scalar(rest)
        elif i < len(lines) and _indent(lines[i]) > _indent(lines[i - 1]):
            child = _indent(lines[i])
            if lines[i].strip().startswith("- "):
                items = []
                while i < len(lines) and _indent(lines[i]) == child and lines[i].strip().startswith("- "):
                    item = lines[i].strip()[2:]
                    i += 1
                    # A list of maps (case.yaml `graders:`) is kept as a marker; this audit reads graders/*.md only.
                    while i < len(lines) and _indent(lines[i]) > child:
                        item, i = None, i + 1
                    items.append(_scalar(item) if item is not None and ": " not in item else Unsupported)
                out[key] = items
            else:
                out[key], i = _block(lines, i, child)
        else:
            out[key] = None
    return out, i


def parse_yaml(text: str) -> dict:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return {}
    data, i = _block(lines, 0, _indent(lines[0]))
    if i != len(lines):
        raise Unsupported(f"読めない行: {lines[i].strip()}")
    return data


def split_front(text: str) -> tuple[dict, str]:
    """`---` frontmatter and the body after it. A file without frontmatter is all body."""
    m = re.match(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?\n?", text, re.DOTALL | re.MULTILINE)
    if not m:
        return {}, text
    return parse_yaml(m.group(1)), text[m.end():]


# ---------------------------------------------------------------- the suite

@dataclass
class Grader:
    name: str
    spec: dict
    body: str

    @property
    def type(self):
        return self.spec.get("type")

    def is_skill_check(self) -> bool:
        return self.type == "tool_used" and self.spec.get("tool") == "Skill"

    def asserts_never(self) -> bool:
        return self.type == "tool_used" and self.spec.get("max") == 0


@dataclass
class Case:
    name: str
    dir: Path
    prompt: str = ""
    front: dict = field(default_factory=dict)
    yaml: dict = field(default_factory=dict)
    graders: list[Grader] = field(default_factory=list)

    def opt(self, key, default=None):
        """A run field: prompt.md frontmatter wins over case.yaml `execution:` (as the CLI does)."""
        if key in self.front:
            return self.front[key]
        return (self.yaml.get("execution") or {}).get(key, default)

    @property
    def scaffold(self) -> str | None:
        return (self.yaml.get("context") or {}).get("scaffold_script")


@dataclass
class Finding:
    level: str
    case: str
    grader: str | None
    message: str

    def __str__(self):
        where = self.case + (f" / {self.grader}" if self.grader else "")
        return f"[{self.level}] {where}: {self.message}"


def eval_dir_of(plugin: Path) -> Path:
    for manifest in (plugin / ".claude-plugin" / "plugin.json", plugin / "plugin.json"):
        if manifest.is_file():
            try:
                configured = (json.loads(manifest.read_text(encoding="utf-8")).get("experimental") or {}).get("evals")
            except (json.JSONDecodeError, AttributeError):
                configured = None
            if isinstance(configured, str) and configured and not Path(configured).is_absolute() and ".." not in Path(configured).parts:
                return plugin / configured
            break
    return plugin / "evals"


def find_case_dirs(root: Path) -> list[Path]:
    found = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if d.name in ("results", "mocks"):
            continue
        if (d / "prompt.md").is_file() or (d / "case.yaml").is_file():
            found.append(d)  # anything inside a case directory belongs to it
        else:
            found.extend(find_case_dirs(d))
    return found


def load_case(d: Path, findings: list[Finding]) -> Case | None:
    case = Case(name=d.name, dir=d)
    try:
        if (d / "case.yaml").is_file():
            case.yaml = parse_yaml((d / "case.yaml").read_text(encoding="utf-8"))
            case.name = str(case.yaml.get("name") or d.name)
            case.prompt = str((case.yaml.get("execution") or {}).get("prompt") or "")
        if (d / "prompt.md").is_file():
            case.front, body = split_front((d / "prompt.md").read_text(encoding="utf-8"))
            case.name = str(case.front.get("name") or case.name)
            case.prompt = body.strip()
    except Unsupported as e:
        findings.append(Finding(ERROR, d.name, None, f"ケースのファイルを読めません（{e}）"))
        return None
    for g in sorted((d / "graders").glob("*.md")):
        try:
            spec, body = split_front(g.read_text(encoding="utf-8"))
        except Unsupported as e:
            findings.append(Finding(ERROR, case.name, g.stem, f"frontmatter を読めません（{e}）"))
            continue
        case.graders.append(Grader(g.stem, spec, body.strip()))
    if case.yaml.get("graders"):
        findings.append(Finding(INFO, case.name, None, "case.yaml の graders: はこの点検の対象外です（graders/*.md だけを見ます）"))
    return case


# ---------------------------------------------------------------- static checks

SLASH = re.compile(r"\A/[\w-]+(?::[\w-]+)?(?:\s|$)")
ABS_DATE = re.compile(r"(?<!\d)(?:19|20)\d\d\s*(?:[-/.]|年)\s*\d{1,2}\s*(?:[-/.]|月)\s*\d{1,2}(?!\d)")
INLINE_FLAG = re.compile(r"\(\?[aiLmsux]+[):]")
UNESCAPED_QUOTE = re.compile(r'(?<!\\)"')


def aws_config_follows_a_command() -> bool:
    path = Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")
    try:
        return re.search(r"^\s*(credential_process|credential_source)\s*=", path.read_text(encoding="utf-8"), re.MULTILINE) is not None
    except OSError:
        return False


def static_checks(cases: list[Case]) -> list[Finding]:
    out: list[Finding] = []
    if not cases:
        return [Finding(ERROR, "(suite)", None, "ケースがありません。`claude plugin eval init` で作ってから点検してください")]
    if not any(g.is_skill_check() and g.asserts_never() for c in cases for g in c.graders):
        out.append(Finding(WARN, "(suite)", None,
                           "skill を呼んではいけない依頼のケースがありません。Skill の tool_used を min: 0 / max: 0 で見るケースを足してください"))
    shell_cases = []
    for c in cases:
        tools = c.opt("allowed_tools") or []
        if not c.graders:
            out.append(Finding(ERROR, c.name, None, "grader がありません（読み込みに失敗します）"))
        if ABS_DATE.search(c.prompt):
            out.append(Finding(WARN, c.name, None,
                               "プロンプトに絶対日付があります。過ぎると正解が変わり、遠い未来の日付は打ち間違いと読まれて聞き返されます。「今日から 3 日後」のように相対で書いてください"))
        slash = SLASH.match(c.prompt)
        if slash:
            for g in c.graders:
                if g.is_skill_check() and not g.asserts_never() and (g.spec.get("min", 1) or 0) >= 1:
                    out.append(Finding(WARN, c.name, g.name,
                                       f"プロンプトが {slash.group(0).strip()} で始まります。slash で読み込んだ skill は Skill の tool 呼び出しとして記録されず、この grader は落ちます（実測）。skill のファイルの Read を見るか、結果を見る grader にしてください"))
        if any(t in ("Write", "Edit", "Bash", "NotebookEdit") for t in tools) and c.opt("max_turns") is None:
            out.append(Finding(WARN, c.name, None, "max_turns が既定の 10 です。ファイルを作るケースでは足りず、上限に達すると実行エラーになります"))
        if any(str(t).split("(")[0] == "Bash" for t in tools):
            shell_cases.append(c.name)

        kinds = {g.type for g in c.graders}
        if kinds and not kinds & {"tool_used", "tool_order"}:
            out.append(Finding(INFO, c.name, None, "手順を見る grader（tool_used / tool_order）がありません。結果が合っていても、plugin が出したのかは分かりません"))
        if kinds and not kinds & {"regex", "file_exists", "llm", "baseline"}:
            out.append(Finding(INFO, c.name, None, "結果を見る grader（regex / file_exists / llm）がありません"))
        scored_others = [g for g in c.graders if not g.is_skill_check() and g.spec.get("arm") != "with-only"]

        for g in c.graders:
            s = g.spec
            if g.type is None:
                out.append(Finding(ERROR, c.name, g.name, "type がありません"))
            if g.type == "regex":
                pattern = str(s.get("pattern", ""))
                if INLINE_FLAG.search(pattern):
                    out.append(Finding(ERROR, c.name, g.name, "パターンにインラインのフラグ (?i) などがあります。JavaScript の正規表現では使えないので flags: i に移してください"))
                if s.get("target") == "trace" and UNESCAPED_QUOTE.search(pattern):
                    out.append(Finding(WARN, c.name, g.name, 'target: trace は JSON の文字列なので、本文の " は \\" になっています。パターンの " は一致しません'))
            if g.is_skill_check() and g.asserts_never() and s.get("arm") != "both" and scored_others:
                out.append(Finding(WARN, c.name, g.name, "Skill の grader は既定で点数から外れます。呼ばないことを点数にするなら arm: both を付けてください"))
            if g.type == "llm" and not ("PASS" in g.body and "FAIL" in g.body):
                out.append(Finding(WARN, c.name, g.name, "llm の基準に PASS と FAIL の両方の条件がありません。判定がぶれやすくなります"))
    if shell_cases and aws_config_follows_a_command():
        out.append(Finding(WARN, "(suite)", None,
                           f"Bash を使うケース（{', '.join(shell_cases)}）がありますが、AWS の設定に credential_process / credential_source があります。"
                           "この状態では、Bash を許可した eval は sandbox を作れず起動を断ります（実測）"))
    return out


# ---------------------------------------------------------------- the lazy run

@dataclass
class Lazy:
    workspace: Path
    created: list[str]  # posix paths relative to the workspace


def run_scaffold(case: Case, ws: Path) -> str | None:
    """Run the case's scaffold like the CLI does: bash in the empty workspace, a small env, 120 s. Error text or None."""
    script = case.dir / str(case.scaffold)
    if not script.is_file():
        return f"scaffold_script が見つかりません: {case.scaffold}"
    home = ws.parent / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TMPDIR": str(ws.parent), "TERM": "dumb"}
    try:
        r = subprocess.run(["bash", str(script)], cwd=ws, env=env, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        return "scaffold が 120 秒で終わりません"
    return None if r.returncode == 0 else f"scaffold が終了コード {r.returncode} で失敗しました: {r.stderr.strip()[-300:]}"


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts}


def target_text(g: Grader, lazy: Lazy) -> str | None:
    target = g.spec.get("target", "last_message")
    if target in ("last_message", "trace"):
        return ""
    if target == "files":
        return "\n".join(lazy.created)
    if isinstance(target, dict) and target.get("source") == "file":
        p = lazy.workspace / str(target.get("path", ""))
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None  # the CLI's verdict on a missing file is not documented
    return None


def js_flags(flags) -> int:
    return sum({"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}.get(ch, 0) for ch in str(flags or ""))


def lazy_verdict(g: Grader, lazy: Lazy) -> bool | None:
    """Pass/fail of a free grader on the lazy run; None when it cannot be known here."""
    s = g.spec
    if g.type == "regex":
        text = target_text(g, lazy)
        match = str(s.get("match", "contains"))
        if text is None:
            # A missing file cannot contain the pattern whatever the CLI does with it; absence is undocumented.
            return False if match == "contains" else None
        try:
            rx = re.compile(str(s.get("pattern", "")), js_flags(s.get("flags")))
        except re.error:
            return None  # JavaScript-only syntax; reported by the caller
        if match == "not_contains":
            return rx.search(text) is None
        if match.startswith("count:"):
            return sum(1 for _ in rx.finditer(text)) == int(match.split(":", 1)[1])
        return rx.search(text) is not None
    if g.type == "tool_used":
        lo, hi = s.get("min", 1), s.get("max")
        return lo <= 0 and (hi is None or hi >= 0)
    if g.type == "tool_order":
        return False
    if g.type == "file_exists":
        pattern = str(s.get("path", ""))
        hit = any(fnmatch.fnmatchcase(f, pattern) for f in lazy.created)
        return hit if s.get("exists", True) is not False else not hit
    return None


def is_guard(g: Grader) -> bool:
    """A grader that is meant to pass when nothing happens: a "never" check."""
    s = g.spec
    if g.type == "tool_used" and s.get("min", 1) == 0:
        return True
    if g.type == "file_exists" and s.get("exists") is False:
        return True
    return g.type == "regex" and s.get("match") == "not_contains" and s.get("target", "last_message") in ("last_message", "trace", "files")


def scored(case: Case) -> list[Grader]:
    """Graders that count in the with-arm of a two-arm run (the default)."""
    def excluded(g):
        return g.spec.get("arm") == "with-only" or (g.is_skill_check() and g.spec.get("arm") != "both")
    kept = [g for g in case.graders if not excluded(g)]
    return kept or case.graders  # all excluded: the CLI scores them normally


def lazy_checks(case: Case, scaffold_ok: bool, overlay: Path | None, threshold: float) -> list[Finding]:
    out: list[Finding] = []
    if case.scaffold and not scaffold_ok:
        return [Finding(INFO, case.name, None, "scaffold があるので、何もしなかった実行の点検を飛ばしました。--scaffold を付けると点検します")]
    tmp = Path(tempfile.mkdtemp(prefix="eval-audit-"))
    try:
        ws = tmp / "workspace"
        ws.mkdir()
        if case.scaffold:
            err = run_scaffold(case, ws)
            if err:
                return [Finding(ERROR, case.name, None, err)]
        before = files_under(ws)
        if overlay:
            shutil.copytree(overlay, ws, dirs_exist_ok=True)
        lazy = Lazy(ws, sorted(files_under(ws) - before))

        for g in case.graders:
            s = g.spec
            if g.type == "file_exists" and s.get("exists", True) is not False:
                pattern = str(s.get("path", ""))
                if any(fnmatch.fnmatchcase(f, pattern) for f in before):
                    out.append(Finding(ERROR, case.name, g.name,
                                       f"{pattern} は scaffold が作るファイルです。file_exists は実行中に作られたファイルしか数えないので、この grader は必ず落ちます。中身を regex で見るか、Edit の tool_used にしてください"))
            if g.type == "regex":
                try:
                    re.compile(str(s.get("pattern", "")))
                except re.error:
                    out.append(Finding(INFO, case.name, g.name, "Python で読めない正規表現なので、この点検での判定を飛ばしました"))

        weights = {g.name: float(g.spec.get("weight", 1) or 1) for g in case.graders}
        counted = scored(case)
        passed = [g for g in counted if lazy_verdict(g, lazy) is True]
        unknown = [g.name for g in counted if lazy_verdict(g, lazy) is None]
        total = sum(weights[g.name] for g in counted)
        score = sum(weights[g.name] for g in passed) / total if total else 0.0
        state = "scaffold の状態" + (f"に {overlay.name}/ を重ねた状態" if overlay else "のまま")
        lead = "手を抜いた出力" if overlay else "何もしなかった実行"
        if score >= threshold and all(is_guard(g) for g in passed):
            out.append(Finding(WARN, case.name, None,
                               "「しないこと」を見る grader だけで合格します。依頼に応えたかを見る grader（返答の llm など）を足してください"))
        elif score >= threshold:
            out.append(Finding(ERROR, case.name, None,
                               f"{lead}でも {score:.2f} 点で合格します（{state}、tool 呼び出しなし、返答なし）"))
        for g in passed:
            if not is_guard(g):
                out.append(Finding(WARN, case.name, g.name, f"{lead}（{state}）でも合格します。作業をした証拠になっていません"))
        if unknown:
            out.append(Finding(INFO, case.name, None, f"ここでは判定できない grader: {', '.join(unknown)}（{lead}の点数 {score:.2f} は、これらが落ちるとしたときの値）"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="claude plugin eval のケースを点検する")
    ap.add_argument("plugin", nargs="?", default=".", help="plugin のディレクトリ（既定: カレント）")
    ap.add_argument("--case", default="*", help="点検するケース名の glob")
    ap.add_argument("--scaffold", action="store_true", help="ケースの scaffold_script を実行する（自分で書いたケースにだけ使う）")
    ap.add_argument("--overlay", type=Path, help="手を抜いた出力として scaffold の上に重ねるディレクトリ（中身は Claude が作ったファイルとして扱う）")
    ap.add_argument("--threshold", type=float, default=1.0, help="合格とみなす点数（claude plugin eval の --threshold と同じ。既定 1.0）")
    args = ap.parse_args(argv)

    plugin = Path(args.plugin).resolve()
    root = eval_dir_of(plugin)
    if not root.is_dir():
        print(f"[ERROR] (suite): eval のディレクトリがありません: {root}。`claude plugin eval init` で作ってください")
        return 1
    if args.overlay and not args.overlay.is_dir():
        print(f"[ERROR] (suite): --overlay のディレクトリがありません: {args.overlay}")
        return 1

    findings: list[Finding] = []
    cases = [c for c in (load_case(d, findings) for d in find_case_dirs(root)) if c]
    findings += static_checks(cases)
    for c in cases:
        if fnmatch.fnmatchcase(c.name, args.case):
            findings += lazy_checks(c, args.scaffold, args.overlay.resolve() if args.overlay else None, args.threshold)

    order = {ERROR: 0, WARN: 1, INFO: 2}
    for f in sorted(findings, key=lambda f: (order[f.level], f.case)):
        print(f)
    counts = {lv: sum(f.level == lv for f in findings) for lv in order}
    print(f"\n{len(cases)} ケース · ERROR {counts[ERROR]} · WARN {counts[WARN]} · INFO {counts[INFO]}")
    return 1 if counts[ERROR] else 0


if __name__ == "__main__":
    sys.exit(main())
