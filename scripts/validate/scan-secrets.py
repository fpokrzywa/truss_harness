"""Hard-coded secret scan over git-tracked files (stdlib only). PHASE_9_CONTRACT §3.

Detects
  * well-known credential formats (private key blocks, AWS/GitHub/Slack/Google/OpenAI/Anthropic-style keys,
    JWTs),
  * credentials embedded in URLs (scheme://user:password@host),
  * assignments of password/secret/token/key-like names to literal values,
  * high-entropy literals (base64 / hex, Shannon entropy) assigned to secret-like names.

Allowlisting (keep it narrow and reviewed):
  * path globs in scripts/validate/scan-secrets.allow (`path:<glob>`), e.g. lockfiles whose integrity
    hashes are not secrets;
  * value regexes in the same file (`value:<regex>`) for documented placeholders;
  * an inline marker `scan-secrets: allow` (with a reason) on the line itself;
  * built-in placeholder detection: example/placeholder/changeme/redacted/<...>/${...}/*** etc.
  Test files are scanned too, but findings there are reported as `test-fixture` (informational) unless
  they match a real-credential format.

Usage: python scripts/validate/scan-secrets.py [--root .] [--json out.json] [--all-tracked]
Exit 0 = no findings (informational test-fixture findings do not fail), 1 = findings.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

STRONG = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]
URL_CRED = re.compile(r"\b[a-z][a-z0-9+.\-]*://([^\s:/@'\"`<>{}]+):([^\s@/'\"`<>{}]{3,})@[^\s'\"`]+", re.I)
SECRET_NAME = r"(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|pepper|client[_-]?secret|auth[_-]?key|credential|kek)"
ASSIGN = re.compile(
    rf"""(?ix)
    (?<![.\w])(?P<name>[A-Za-z0-9_\-]*{SECRET_NAME}[A-Za-z0-9_\-]*)
    ["']?\s*(?:=|:|:=|=>)\s*
    (?P<q>["'])(?P<value>[^"'\s]{{8,}})(?P=q)
    """
)
ENV_ASSIGN = re.compile(rf"(?m)^\s*(?:export\s+)?(?P<name>[A-Z0-9_]*{SECRET_NAME.upper()}[A-Z0-9_]*)\s*=\s*(?P<value>[^\s#]{{8,}})\s*$")
ENV_SUFFIXES = {".env", ".example", ".service", ".conf", ".ini", ".cfg", ".sh", ".toml", ".yaml", ".yml"}
# names that carry identifiers, counts, paths or metadata - not secret values
BENIGN_NAME = re.compile(r"(?i)(_file|_id|_ids|_ttl|_hash|_url|_uri|_endpoint|_fields|_header|_name|_type|_mode|_path|"
                         r"tokens|_count|_limit|_length|_len|_max|_min|_prefix|_ref|_refs|_label)$")
HIGH_ENTROPY = re.compile(r"(?<![A-Za-z0-9+/=_\-])([A-Za-z0-9+/_\-]{32,}={0,2})(?![A-Za-z0-9+/=_\-])")

PLACEHOLDER = re.compile(
    r"(?i)(example|placeholder|changeme|change[-_]me|redacted|dummy|sample|fake|not[-_]?a[-_]?secret|"
    r"not[-_]used|your[-_]|<[^>]*>|\$\{[^}]*\}|\{\{[^}]*\}\}|\*{3,}|x{6,}|\.{3}|test[-_]?only|"
    r"definitely[-_]wrong|wrong[-_]password|^\$|^%|os\.environ|getenv|settings\.|secret_ref|_FILE\b)"
)
CODE_VALUE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(\(.*\))?$")  # identifiers / calls, not literals
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                 ".env", ".example", ".ps1", ".psm1", ".sh", ".md", ".txt", ".sql", ".mako", ".service", ".conf",
                 ".html", ".css", ".xml", ".pem", ".key"}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|fixtures?)/|\.test\.[tj]sx?$|(^|/)test_[^/]+\.py$|(^|/)conftest\.py$")


def entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / len(s)) * math.log2(c / len(s)) for c in counts.values())


def load_allow(path: Path) -> tuple[list[str], list[re.Pattern[str]]]:
    globs: list[str] = []
    values: list[re.Pattern[str]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kind, _, rest = line.partition(":")
            if kind == "path":
                globs.append(rest.strip())
            elif kind == "value":
                values.append(re.compile(rest.strip()))
    return globs, values


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, check=True)
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def is_placeholder(value: str, allow_values: list[re.Pattern[str]]) -> bool:
    v = value.strip()
    if PLACEHOLDER.search(v) or any(p.search(v) for p in allow_values):
        return True
    if CODE_VALUE.match(v) and not re.search(r"\d", v) and len(v) < 40:
        return True
    return len(set(v)) <= 3  # "aaaaaaa", "--------"


def scan_file(rel: str, text: str, allow_values: list[re.Pattern[str]]) -> list[dict]:
    hits: list[dict] = []
    is_test = bool(TEST_PATH.search(rel))
    name = rel.rsplit("/", 1)[-1]
    envish = Path(rel).suffix.lower() in ENV_SUFFIXES or name.startswith(".env")
    for lineno, line in enumerate(text.splitlines(), 1):
        if "scan-secrets: allow" in line or len(line) > 4000:
            continue
        found: list[tuple[str, str]] = []
        for kind, rx in STRONG:
            for m in rx.finditer(line):
                found.append((kind, m.group(0)))
        for m in URL_CRED.finditer(line):
            pw = m.group(2)
            if not is_placeholder(pw, allow_values) and not is_placeholder(m.group(0), allow_values):
                found.append(("url-credentials", m.group(0)))
        rxs = [ASSIGN] + ([ENV_ASSIGN] if envish else [])
        for rx in rxs:
            for m in rx.finditer(line):
                val = m.group("value")
                if BENIGN_NAME.search(m.group("name")) or is_placeholder(val, allow_values):
                    continue
                if entropy(val) < 3.0 and len(val) < 20:
                    continue  # short human words ("password", "hunter")
                found.append(("secret-assignment", f"{m.group('name')}=<{len(val)} chars>"))
        for m in HIGH_ENTROPY.finditer(line):
            tok = m.group(1)
            if is_placeholder(tok, allow_values) or not re.search(SECRET_NAME, line, re.I):
                continue
            if "_" in tok and "/" in tok:
                continue  # mixes the url-safe and standard base64 alphabets: a path, not a key
            if not (re.search(r"[0-9]", tok) and re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok)):
                continue
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tok) and re.search(r"(?i)sha|digest|checksum|hash|commit", line):
                continue
            if entropy(tok) >= 4.0:
                found.append(("high-entropy", f"<{len(tok)} chars, H={entropy(tok):.2f}>"))
        seen: set[str] = set()
        for kind, sample in found:
            if kind in seen:
                continue
            seen.add(kind)
            # tests deliberately carry fake credentials (scrubbing / rejection tests): informational
            severity = "test-fixture" if is_test else "finding"
            redacted = sample if sample.startswith(("<",)) or "=<" in sample else (sample[:6] + "…" + f"<{len(sample)} chars>")
            hits.append({"path": rel, "line": lineno, "kind": kind, "severity": severity, "sample": redacted})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--json")
    ap.add_argument("--allow", default=str(Path(__file__).with_name("scan-secrets.allow")))
    a = ap.parse_args()
    root = Path(a.root).resolve()
    globs, values = load_allow(Path(a.allow))
    files = tracked_files(root)
    scanned = 0
    hits: list[dict] = []
    skipped_allow = 0
    for rel in files:
        if rel.startswith(("dist/", ".devdata/", "node_modules/")) or "/node_modules/" in rel:
            continue
        if any(fnmatch.fnmatch(rel, g) for g in globs):
            skipped_allow += 1
            continue
        p = root / rel
        if not p.is_file() or (p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {"Dockerfile", ".gitignore"}
                               and not p.name.startswith(".env")):
            continue
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        hits.extend(scan_file(rel, text, values))
    findings = [h for h in hits if h["severity"] == "finding"]
    fixtures = [h for h in hits if h["severity"] == "test-fixture"]
    for h in findings + fixtures:
        print(f"[{h['severity']}] {h['path']}:{h['line']} {h['kind']} {h['sample']}")
    summary = {"scanned_files": scanned, "allowlisted_paths": skipped_allow, "findings": len(findings),
               "test_fixture_findings": len(fixtures)}
    print(json.dumps(summary))
    if a.json:
        Path(a.json).write_text(json.dumps({"summary": summary, "hits": hits}, indent=2), encoding="utf-8")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
