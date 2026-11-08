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
import contextlib
import fnmatch
import json
import os
import re
import shutil
import signal
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


KEY = re.compile(r"([\w.$/][^:#]*?)[ \t]*:(?:[ \t]+(.*))?$")
SEQ = re.compile(r"-(?:[ \t]+|$)")
BLOCK_SCALAR = re.compile(r"[|>][-+1-9]*(?:[ \t]+#.*)?")
ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", "e": "\x1b",
           " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029"}


def _unescape(body: str) -> str:
    """A double-quoted YAML string. An escape YAML does not define (\\d) is an error there too."""
    def one(m):
        e = m.group(1)
        if len(e) > 1:
            return chr(int(e[1:], 16))
        if e not in ESCAPES:
            raise Unsupported(f"YAML にないエスケープ \\{e}")
        return ESCAPES[e]
    text = re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", one, body, flags=re.DOTALL)
    return text.encode("utf-16", "surrogatepass").decode("utf-16")  # joins \\u surrogate pairs


def _plain(tok: str):
    """A plain scalar under the YAML 1.2 core schema, which Bun.YAML (the CLI's parser) follows."""
    low = tok.lower()
    if low in ("true", "false", "null") and tok in (low, low.capitalize(), low.upper()):
        return {"true": True, "false": False, "null": None}[low]
    if tok == "~":
        return None
    if re.fullmatch(r"[-+]?\d+", tok):
        return int(tok)
    if re.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", tok):
        return float(tok)
    return tok


def _value(s: str, i: int, flow: bool):
    """One value from s[i:] on one line: quoted, a flow [list] or {map}, or plain. Returns (value, end)."""
    while s[i:i + 1] in (" ", "\t"):
        i += 1
    c = s[i:i + 1]
    if c == "'":
        m = re.compile(r"'((?:[^']|'')*)'").match(s, i)
        if not m:
            raise Unsupported(f"閉じていない引用符: {s}")
        return m.group(1).replace("''", "'"), m.end()
    if c == '"':
        m = re.compile(r'"((?:[^"\\]|\\.)*)"').match(s, i)
        if not m:
            raise Unsupported(f"閉じていない引用符: {s}")
        return _unescape(m.group(1)), m.end()
    if c in ("[", "{"):
        close, out = ("]", []) if c == "[" else ("}", {})
        i += 1
        while True:
            i += len(s[i:]) - len(s[i:].lstrip())
            if s[i:i + 1] == close:
                return out, i + 1
            if c == "{":
                key, i = _value(s, i, True)
                if s[i:].lstrip()[:1] != ":":
                    raise Unsupported(f"読めない対応表: {s}")
                val, i = _value(s, s.index(":", i) + 1, True)
                out[str(key)] = val
            else:
                val, i = _value(s, i, True)
                out.append(val)
            i += len(s[i:]) - len(s[i:].lstrip())
            if s[i:i + 1] == ",":
                i += 1
            elif s[i:i + 1] != close:
                raise Unsupported(f"閉じていないリストか対応表: {s}")
    if not c or c in "&*!|>%@`#" or (c in "-?:" and s[i + 1:i + 2] in ("", " ", "\t")):
        raise Unsupported(f"この点検では読めない値: {s}")
    j = i
    while j < len(s):
        if s[j] == "#" and s[j - 1] in " \t":
            break
        if s[j] == ":" and (j + 1 == len(s) or s[j + 1] in " \t" or (flow and s[j + 1] in ",[]{}")):
            break
        if flow and s[j] in ",[]{}":
            break
        j += 1
    tok = s[i:j].rstrip()
    return _plain(tok), i + len(tok)


def _scalar(s: str):
    value, end = _value(s, 0, False)
    rest = s[end:].strip()
    if rest and not rest.startswith("#"):
        raise Unsupported(f"読めない値: {s}")
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _skip(lines: list[str], i: int) -> int:
    """The next line that is not blank or a comment. YAML forbids tabs in indentation, so one there is an error."""
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    if i < len(lines) and lines[i][_indent(lines[i]):][:1] == "\t":
        raise Unsupported(f"字下げにタブがあります: {lines[i].strip()}")
    return i


def _block_scalar(lines: list[str], i: int, parent: int, header: str):
    """`|` or `>` text: the following lines indented deeper than its key."""
    body = []
    while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > parent):
        body.append(lines[i])
        i += 1
    filled = [ln for ln in body if ln.strip()]
    ind = _indent(filled[0]) if filled else 0
    if any(_indent(ln) < ind for ln in filled):
        raise Unsupported(f"字下げが合いません: {filled[0].strip()}")
    text = "\n".join(ln[ind:] for ln in body)
    kept = text[len(text.rstrip("\n")):]
    text = text.rstrip("\n")
    if header[0] == ">":
        # shortcut: more-indented lines are folded too (YAML keeps them); fine for the prompt checks here.
        text = re.sub(r"\n(\n*)", lambda m: m.group(1) or " ", text)
    chomp = header.split("#")[0].strip()
    if not text or "-" in chomp:
        return text, i
    return text + ("\n" + kept if "+" in chomp else "\n"), i


def _seq(lines: list[str], i: int, indent: int):
    items = []
    while (i := _skip(lines, i)) < len(lines) and _indent(lines[i]) == indent and SEQ.match(lines[i].strip()):
        item = SEQ.sub("", lines[i].strip(), count=1)
        i += 1
        nested = False
        while (j := _skip(lines, i)) < len(lines) and _indent(lines[j]) > indent:
            nested, i = True, j + 1
        # A list of maps (case.yaml `graders:`) is kept as a marker; this audit reads graders/*.md only.
        items.append(Unsupported if nested or not item or KEY.match(item) else _scalar(item))
    return items, i


def _block(lines: list[str], i: int, indent: int):
    out: dict = {}
    while (i := _skip(lines, i)) < len(lines) and _indent(lines[i]) >= indent:
        line = lines[i].strip()
        if _indent(lines[i]) > indent:
            raise Unsupported(f"字下げが合いません: {line}")
        m = KEY.match(line)
        if not m:
            raise Unsupported(f"読めない行: {line}")
        key, rest = m.group(1), (m.group(2) or "").strip()
        i += 1
        if rest.startswith("#"):
            rest = ""
        if BLOCK_SCALAR.fullmatch(rest):
            out[key], i = _block_scalar(lines, i, indent, rest)
        elif rest:
            out[key] = _scalar(rest)
        elif (j := _skip(lines, i)) < len(lines) and _indent(lines[j]) >= indent and SEQ.match(lines[j].strip()):
            out[key], i = _seq(lines, j, _indent(lines[j]))
        elif j < len(lines) and _indent(lines[j]) > indent:
            out[key], i = _block(lines, j, _indent(lines[j]))
        else:
            out[key] = None
    return out, i


def parse_yaml(text: str) -> dict:
    """A mapping in the YAML subset eval files use. Anything else raises Unsupported rather than being misread."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    i = _skip(lines, 0)
    if i < len(lines) and lines[i].strip() == "---":
        i = _skip(lines, i + 1)
    if i == len(lines):
        return {}
    data, i = _block(lines, i, _indent(lines[i]))
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


GRADER_TYPES = {"regex", "tool_used", "tool_order", "file_exists", "llm", "baseline"}
REQUIRED = {"regex": "pattern", "file_exists": "path", "tool_used": "tool", "llm": "criteria", "baseline": "criteria"}
FROM_BODY = {"regex": "pattern", "llm": "criteria", "baseline": "criteria"}  # the CLI fills these from the body


def grader_problem(spec: dict) -> str | None:
    """What the CLI's grader schema rejects (the case then fails to load), or None."""
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    kind = spec.get("type")
    if kind not in GRADER_TYPES:
        return f"type は {' / '.join(sorted(GRADER_TYPES))} のどれかにしてください"
    key = REQUIRED.get(kind)
    if key and not isinstance(spec.get(key), str):
        return f"{key} を文字列で書いてください（{kind} には必須です）"
    weight = spec.get("weight", 1)
    if not number(weight) or weight <= 0:
        return "weight は正の数にしてください"
    for k in ("min", "max"):
        v = spec.get(k)
        if v is not None and not (number(v) and v >= 0 and float(v).is_integer()):
            return f"{k} は 0 以上の整数にしてください"
    if kind == "regex" and not re.fullmatch(r"contains|not_contains|count:\d+", str(spec.get("match", "contains"))):
        return "match は contains / not_contains / count:N のどれかにしてください"
    if "exists" in spec and not isinstance(spec["exists"], bool):
        return "exists は true か false にしてください"
    return None


def load_case(d: Path, findings: list[Finding]) -> Case | None:
    case = Case(name=d.name, dir=d)
    try:
        if (d / "case.yaml").is_file():
            case.yaml = parse_yaml((d / "case.yaml").read_text(encoding="utf-8-sig"))
            for k in ("execution", "context"):
                if case.yaml.get(k) is not None and not isinstance(case.yaml[k], dict):
                    raise Unsupported(f"{k}: の下はキーと値の組にしてください")
            case.name = str(case.yaml.get("name") or d.name)
            case.prompt = str((case.yaml.get("execution") or {}).get("prompt") or "")
        if (d / "prompt.md").is_file():
            case.front, body = split_front((d / "prompt.md").read_text(encoding="utf-8-sig"))
            case.name = str(case.front.get("name") or case.name)
            case.prompt = body.strip() or case.prompt  # an empty body leaves execution.prompt in place, as in the CLI
    except (ValueError, OSError) as e:  # Unsupported, and files that are not UTF-8
        findings.append(Finding(ERROR, d.name, None, f"ケースのファイルを読めません（{e}）"))
        return None
    for g in sorted((d / "graders").glob("*.md")):
        try:
            text = g.read_text(encoding="utf-8-sig")
            if not re.match(r"---[ \t]*\r?\n", text):
                findings.append(Finding(WARN, case.name, g.stem, "frontmatter がないので、claude plugin eval はこのファイルを grader として読みません"))
                continue
            spec, body = split_front(text)
            if body == text:
                raise Unsupported("--- で閉じていません")
        except (ValueError, OSError) as e:
            findings.append(Finding(ERROR, case.name, g.stem, f"frontmatter を読めません（{e}）"))
            continue
        key = FROM_BODY.get(spec.get("type"))
        if key and key not in spec and body.strip():
            spec[key] = body.strip()
        problem = grader_problem(spec)
        if problem:
            findings.append(Finding(ERROR, case.name, g.stem, f"{problem}。このままでは claude plugin eval がケースを読み込めません"))
            continue
        case.graders.append(Grader(g.stem, spec, body.strip()))
    if case.yaml.get("graders"):
        findings.append(Finding(INFO, case.name, None, "case.yaml の graders: はこの点検の対象外です（graders/*.md だけを見て、ケースの点数は出しません）"))
    return case


# ---------------------------------------------------------------- static checks

SLASH = re.compile(r"\A/[\w-]+(?::[\w-]+)?(?:\s|$)")
ABS_DATE = re.compile(r"(?<!\d)(?:19|20)\d\d\s*(?:[-/.]|年)\s*\d{1,2}\s*(?:[-/.]|月)\s*\d{1,2}(?!\d)")
INLINE_FLAG = re.compile(r"(?<!\\)\(\?[a-zA-Z]+\)")  # (?i) is an error in JavaScript; the (?i:...) group is not
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
        if not c.graders and not c.yaml.get("graders"):
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
            if g.type == "regex":
                pattern = str(s.get("pattern", ""))
                if INLINE_FLAG.search(pattern):
                    out.append(Finding(ERROR, c.name, g.name, "パターンにインラインのフラグ (?i) などがあります。JavaScript の正規表現では使えないので flags: i に移してください"))
                if s.get("target") == "trace" and UNESCAPED_QUOTE.search(pattern):
                    out.append(Finding(WARN, c.name, g.name, 'target: trace は JSON の文字列なので、本文の " は \\" になっています。パターンの " は一致しません'))
            if g.is_skill_check() and g.asserts_never() and s.get("arm") != "both" and scored_others:
                out.append(Finding(WARN, c.name, g.name, "Skill の grader は既定で点数から外れます。呼ばないことを点数にするなら arm: both を付けてください"))
            if g.type == "llm" and not ("PASS" in s["criteria"] and "FAIL" in s["criteria"]):
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


SCAFFOLD_TIMEOUT = 120


def run_scaffold(case: Case, ws: Path) -> str | None:
    """Run the case's scaffold like the CLI does: bash in the empty workspace, a small env, no stdin, 120 s. Error text or None."""
    script = case.dir / str(case.scaffold)
    if not script.is_file():
        return f"scaffold_script が見つかりません: {case.scaffold}"
    home = ws.parent / "home"
    home.mkdir(exist_ok=True)
    tmp = str(ws.parent)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp, "TERM": "dumb",
           "GIT_CONFIG_NOSYSTEM": "1"}
    # stderr goes to a file, not a pipe, so a process the script leaves behind cannot hold the wait open.
    with open(ws.parent / "scaffold.stderr", "w+", encoding="utf-8", errors="replace") as err:
        try:
            proc = subprocess.Popen(["bash", str(script)], cwd=ws, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err, start_new_session=True)
        except OSError as e:
            return f"scaffold を起動できません: {e}"
        try:
            code = proc.wait(timeout=SCAFFOLD_TIMEOUT)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)  # whatever the script started dies with it before the temp dir goes
            proc.wait()
        err.seek(0)
        stderr = err.read().strip()
    if code is None:
        return f"scaffold が {SCAFFOLD_TIMEOUT} 秒で終わりません"
    return None if code == 0 else f"scaffold が終了コード {code} で失敗しました: {stderr[-300:]}"


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts}


def overlay_escape(overlay: Path, ws: Path) -> str | None:
    """A path in the overlay that a link the scaffold made would carry outside the workspace, or None."""
    root = ws.resolve()
    # Walk into linked directories too, since copytree copies what they hold.
    paths = (Path(d, n) for d, dirs, files in os.walk(overlay, followlinks=True) for n in dirs + files)
    for rel in sorted(p.relative_to(overlay) for p in paths):
        try:
            inside = (ws / rel).resolve().is_relative_to(root)
        except (OSError, RuntimeError):  # a link loop: refuse rather than guess
            inside = False
        if not inside:
            return rel.as_posix()
    return None


def target_text(g: Grader, lazy: Lazy) -> str | None:
    target = g.spec.get("target", "last_message")
    if target == "last_message":
        return ""
    if target == "files":
        return "\n".join(lazy.created)
    if isinstance(target, dict) and target.get("source") == "file":
        # The CLI refuses a path outside the workspace (.., absolute, a link out) and anything but a regular file.
        root = lazy.workspace.resolve()
        try:
            p = (root / str(target.get("path", ""))).resolve()
            if not p.is_relative_to(root) or not p.is_file():
                return None
            return p.read_text(encoding="utf-8-sig", errors="replace")
        except (OSError, ValueError, RuntimeError):  # unreadable, a NUL in the path, a link loop
            return None
    return None


def js_regex(pattern: str, flags) -> re.Pattern:
    """A JavaScript regex as Python re: \\w \\d \\b are ASCII-only there, and $ without m is the very end."""
    flags = str(flags or "")
    if "m" not in flags:
        pattern = re.sub(r"\\.|\[(?:\\.|[^\]\\])*\]|\$", lambda m: r"\Z" if m.group() == "$" else m.group(), pattern)
    bits = sum({"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}.get(ch, 0) for ch in set(flags))
    return re.compile(pattern, bits | re.ASCII)


def glob_regex(pattern: str) -> re.Pattern:
    """A file_exists path as the CLI reads it: * stays in one directory, **/ spans any, ? is any one character, the rest is literal."""
    out, i = "", 0
    while i < len(pattern):
        for token, rx in (("**/", "(?:.*/)?"), ("**", ".*"), ("*", "[^/]*"), ("?", ".")):
            if pattern.startswith(token, i):
                out, i = out + rx, i + len(token)
                break
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def lazy_verdict(g: Grader, lazy: Lazy) -> bool | None:
    """Pass/fail of a free grader on the lazy run; None when it cannot be known here."""
    s = g.spec
    if g.type == "regex":
        if s.get("target") == "trace":
            return None  # the trace holds the session's own start and result messages even when Claude did nothing
        text = target_text(g, lazy)
        match = str(s.get("match", "contains"))
        if text is None:
            # A missing file cannot contain the pattern whatever the CLI does with it; absence is undocumented.
            return False if match == "contains" else None
        try:
            rx = js_regex(str(s.get("pattern", "")), s.get("flags"))
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
        rx = glob_regex(str(s.get("path", "")))
        hit = any(rx.fullmatch(f) for f in lazy.created)
        return hit if s.get("exists", True) is not False else not hit
    return None


def is_guard(g: Grader) -> bool:
    """A grader that is meant to pass when nothing happens: a "never" check."""
    s = g.spec
    if g.type == "tool_used" and s.get("min", 1) == 0:
        return True
    if g.type == "file_exists" and s.get("exists") is False:
        return True
    match = str(s.get("match", "contains"))
    absent = match == "not_contains" or (match.startswith("count:") and int(match.split(":", 1)[1]) == 0)
    return g.type == "regex" and absent and s.get("target", "last_message") in ("last_message", "files")


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
            escape = overlay_escape(overlay, ws)
            if escape:
                return [Finding(ERROR, case.name, None,
                                f"--overlay の {escape} は、scaffold が作ったリンクをたどると作業ディレクトリの外に書き込むので、重ねずに止めました")]
            shutil.copytree(overlay, ws, dirs_exist_ok=True)
        lazy = Lazy(ws, sorted(files_under(ws) - before))

        for g in case.graders:
            s = g.spec
            if g.type == "file_exists" and s.get("exists", True) is not False:
                pattern = str(s.get("path", ""))
                made = [f for f in sorted(before) if glob_regex(pattern).fullmatch(f)]
                if made and ("*" in pattern or "?" in pattern):
                    out.append(Finding(WARN, case.name, g.name,
                                       f"{pattern} に scaffold が作る {made[0]} が当たりますが、file_exists は実行中に作られたファイルしか数えません。そのファイルを書き換えるだけの実行では落ちます"))
                elif made:
                    out.append(Finding(ERROR, case.name, g.name,
                                       f"{pattern} は scaffold が作るファイルです。file_exists は実行中に作られたファイルしか数えないので、この grader は必ず落ちます。中身を regex で見るか、Edit の tool_used にしてください"))
            if g.type == "regex":
                try:
                    js_regex(str(s.get("pattern", "")), s.get("flags"))
                except re.error:
                    out.append(Finding(INFO, case.name, g.name, "Python で読めない正規表現なので、この点検での判定を飛ばしました"))

        weights = {g.name: float(g.spec.get("weight", 1)) for g in case.graders}
        counted = scored(case)
        passed = [g for g in counted if lazy_verdict(g, lazy) is True]
        unknown = [g.name for g in counted if lazy_verdict(g, lazy) is None]
        total = sum(weights[g.name] for g in counted)
        score = sum(weights[g.name] for g in passed) / total if total else 0.0
        state = "scaffold の状態" + (f"に {overlay.name}/ を重ねた状態" if overlay else "のまま")
        lead = "手を抜いた出力" if overlay else "何もしなかった実行"
        partial = bool(case.yaml.get("graders"))  # case.yaml graders are not read, so there is no case score to judge
        if not partial and score >= threshold and all(is_guard(g) for g in passed):
            out.append(Finding(WARN, case.name, None,
                               "「しないこと」を見る grader だけで合格します。依頼に応えたかを見る grader（返答の llm など）を足してください"))
        elif not partial and score >= threshold:
            out.append(Finding(ERROR, case.name, None,
                               f"{lead}でも {score:.2f} 点で合格します（{state}、tool 呼び出しなし、返答なし）"))
        for g in passed:
            if not is_guard(g):
                out.append(Finding(WARN, case.name, g.name, f"{lead}（{state}）でも合格します。作業をした証拠になっていません"))
        if unknown and not partial:
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
    if not 0 <= args.threshold <= 1:
        ap.error("--threshold は 0 から 1 の間で指定してください")

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
