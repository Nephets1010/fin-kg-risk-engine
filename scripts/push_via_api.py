"""通过 GitHub Git Data API 推送本地提交。

用途：在 git 的 HTTPS 端点被网络策略阻断、但 ``api.github.com`` 可达时，
仍然能把一次完整提交推送到远端。

流程与 ``git push`` 等价：

1. 为每个文件创建一个 blob（``POST /repos/{owner}/{repo}/git/blobs``）
2. 用这些 blob 构造一棵 tree（``POST .../git/trees``）
3. 创建一个 commit（``POST .../git/commits``）
4. 更新分支引用（``PATCH .../git/refs/heads/{branch}``）

用法::

    python scripts/push_via_api.py --repo Nephets1010/fin-kg-risk-engine
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path


def run_gh(*args: str, stdin: str | None = None) -> str:
    """Run a gh command and return its stdout."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def git(*args: str) -> str:
    """Run a git command and return its stdout."""
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def list_files(root: Path) -> list[Path]:
    """Return the files tracked by git, relative to the repository root."""
    output = git("-C", str(root), "ls-files", "-z")
    return [Path(item) for item in output.split("\0") if item]


def create_blob(repo: str, path: Path, root: Path) -> str:
    """Upload one file as a blob and return its SHA."""
    data = (root / path).read_bytes()
    payload = json.dumps({"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
    response = run_gh(
        "api",
        f"repos/{repo}/git/blobs",
        "--method",
        "POST",
        "--input",
        "-",
        stdin=payload,
    )
    return json.loads(response)["sha"]


def build_tree(repo: str, entries: list[dict[str, str]]) -> str:
    """Create the tree and return its SHA."""
    payload = json.dumps({"tree": entries})
    response = run_gh(
        "api",
        f"repos/{repo}/git/trees",
        "--method",
        "POST",
        "--input",
        "-",
        stdin=payload,
    )
    return json.loads(response)["sha"]


def create_commit(repo: str, tree_sha: str, message: str, parents: list[str]) -> str:
    """Create the commit and return its SHA."""
    payload = json.dumps({"message": message, "tree": tree_sha, "parents": parents})
    response = run_gh(
        "api",
        f"repos/{repo}/git/commits",
        "--method",
        "POST",
        "--input",
        "-",
        stdin=payload,
    )
    return json.loads(response)["sha"]


def update_ref(repo: str, branch: str, commit_sha: str, force: bool = False) -> None:
    """Point the branch at the commit."""
    payload = json.dumps({"sha": commit_sha, "force": force})
    run_gh(
        "api",
        f"repos/{repo}/git/refs/heads/{branch}",
        "--method",
        "PATCH",
        "--input",
        "-",
        stdin=payload,
    )


def get_ref(repo: str, branch: str) -> dict[str, object] | None:
    """Return the branch reference, or ``None`` when the branch is absent."""
    try:
        response = run_gh("api", f"repos/{repo}/git/ref/heads/{branch}")
    except RuntimeError:
        return None
    return json.loads(response)


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="通过 Git Data API 推送提交")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--root", default=".")
    parser.add_argument("--message", default=None, help="提交信息，默认取 HEAD 的")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    files = list_files(root)
    if not files:
        print("no tracked files — nothing to push")
        return 1

    message = args.message
    if not message:
        message = git("-C", str(root), "log", "-1", "--pretty=%B").strip()

    parents: list[str] = []
    reference = get_ref(args.repo, args.branch)
    if reference:
        parents.append(str(reference["object"]["sha"]))

    print(f"repo:   {args.repo}")
    print(f"branch: {args.branch}")
    print(f"files:  {len(files)}")
    print(f"parent: {parents[0] if parents else '(none — initial commit)'}")

    entries: list[dict[str, str]] = []
    for index, path in enumerate(files, start=1):
        sha = create_blob(args.repo, path, root)
        entries.append({"path": path.as_posix(), "mode": "100644", "type": "blob", "sha": sha})
        if index % 20 == 0 or index == len(files):
            print(f"  blobs {index}/{len(files)}")

    tree_sha = build_tree(args.repo, entries)
    print(f"tree:   {tree_sha}")

    commit_sha = create_commit(args.repo, tree_sha, message, parents)
    print(f"commit: {commit_sha}")

    update_ref(args.repo, args.branch, commit_sha, force=args.force)
    print(f"\npushed to https://github.com/{args.repo}/tree/{args.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
