"""仓库密钥扫描。

在提交前或 CI 中运行，检查仓库里是否存在不该出现的凭证。

用法::

    python scripts/check_secrets.py                 # 扫描工作区
    python scripts/check_secrets.py --staged        # 只扫描暂存区
    python scripts/check_secrets.py --history       # 扫描全部提交历史

检查项：

1. 常见密钥格式（OpenAI / GitHub / AWS / 阿里云 / 私钥）
2. 被 git 追踪的 ``.env`` 或含密码的配置文件
3. 硬编码的数据库连接串
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Patterns of credentials that must never appear in the repository.
PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style API key"),
    (r"sk-proj-[A-Za-z0-9_-]{20,}", "OpenAI project key"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
    (r"gho_[A-Za-z0-9]{36}", "GitHub OAuth token"),
    (r"github_pat_[A-Za-z0-9_]{50,}", "GitHub fine-grained token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"LTAI[A-Za-z0-9]{12,}", "Alibaba Cloud access key"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "private key"),
    (r"mongodb(?:\+srv)?://[^\s:]+:[^\s@]+@", "MongoDB connection string"),
    (r"postgres(?:ql)?://[^\s:]+:[^\s@]+@", "PostgreSQL connection string"),
    (r"mysql://[^\s:]+:[^\s@]+@", "MySQL connection string"),
    (r"redis://[^\s:]*:[^\s@]+@", "Redis connection string"),
    (r"amqp://[^\s:]+:[^\s@]+@", "AMQP connection string"),
)

#: Files that must never be tracked by git.
FORBIDDEN_FILES: tuple[str, ...] = (".env", ".env.local", ".env.production", "secrets.yaml")

#: Paths that are allowed to contain the patterns above (documentation, fixtures).
ALLOWED_PATHS: tuple[str, ...] = (
    "scripts/check_secrets.py",
    "SECURITY.md",
    ".github/workflows/ci.yml",
    # The Compose file interpolates credentials from the environment; the
    # `${...}` placeholders are not literal secrets.
    "docker-compose.yml",
)

#: Binary or generated files that are not worth scanning.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".whl")


class Finding:
    """One potential secret."""

    def __init__(self, path: str, line_number: int, kind: str, excerpt: str) -> None:
        self.path = path
        self.line_number = line_number
        self.kind = kind
        self.excerpt = excerpt

    def __str__(self) -> str:
        return f"{self.path}:{self.line_number}  {self.kind}: {self.excerpt}"


def _redact(text: str) -> str:
    """Show only the beginning of a match, never the full secret."""
    return text[:12] + "..." if len(text) > 12 else text


def scan_text(path: str, text: str) -> list[Finding]:
    """Scan one text blob for credential patterns."""
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in PATTERNS:
            match = re.search(pattern, line)
            if match:
                findings.append(Finding(path, line_number, kind, _redact(match.group(0))))
    return findings


def scan_worktree(root: Path) -> list[Finding]:
    """Scan every tracked-eligible file in the working tree."""
    findings: list[Finding] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in ALLOWED_PATHS:
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if any(part in {".git", "node_modules", ".venv", "__pycache__"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(relative, text))
    return findings


def scan_git_history(root: Path) -> list[Finding]:
    """Scan every commit in the history."""
    findings: list[Finding] = []
    try:
        output = subprocess.run(
            ["git", "log", "--all", "--pretty=format:%H"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return findings

    for commit in output.split():
        try:
            diff = subprocess.run(
                ["git", "show", "--pretty=format:", "-p", commit],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError:
            continue
        findings.extend(scan_text(f"commit {commit[:8]}", diff))
    return findings


def check_tracked_files(root: Path) -> list[str]:
    """Return the forbidden files that git is tracking."""
    try:
        output = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return []
    tracked = {line.strip() for line in output.splitlines() if line.strip()}
    return sorted(tracked & set(FORBIDDEN_FILES))


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="仓库密钥扫描")
    parser.add_argument("--root", default=".")
    parser.add_argument("--staged", action="store_true", help="只扫描暂存区")
    parser.add_argument("--history", action="store_true", help="扫描全部提交历史")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    findings: list[Finding] = []

    if args.history:
        findings.extend(scan_git_history(root))
    elif args.staged:
        try:
            diff = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError:
            diff = ""
        findings.extend(scan_text("staged", diff))
    else:
        findings.extend(scan_worktree(root))

    tracked = check_tracked_files(root)

    print(f"scanned: {root}")
    if findings:
        print(f"\nfound {len(findings)} potential secret(s):")
        for finding in findings[:50]:
            print(f"  {finding}")
        if len(findings) > 50:
            print(f"  ... and {len(findings) - 50} more")
    else:
        print("no potential secrets found")

    if tracked:
        print(f"\nforbidden files are tracked by git: {tracked}")

    if findings or tracked:
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
