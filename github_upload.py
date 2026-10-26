"""
github_upload.py - upload a folder to GitHub, or update files already there.

Works for any folder and any repository. Only new or changed files are sent,
so running it again after editing a few files just updates those files.

Quick start (asks for everything it needs):
    python github_upload.py

Examples:
    python github_upload.py "C:\\path\\to\\project"                  # upload / update whole folder
    python github_upload.py . --repo my-repo -m "Fix typo"         # repo in your account
    python github_upload.py . --repo my-org/site --branch dev      # another owner / branch
    python github_upload.py . --files report.pdf src/main.py       # update only these files
    python github_upload.py . --prefix docs                        # put files under docs/ in the repo
    python github_upload.py . --mirror                             # also delete repo files you deleted locally
    python github_upload.py . --dry-run                            # show what would change, upload nothing

Password: GitHub does not accept your account password from scripts.
Use a Personal Access Token instead: https://github.com/settings/tokens
("Tokens (classic)" -> tick "repo"). You can also set it once per terminal:
    $env:GITHUB_TOKEN = "ghp_..."        (PowerShell)

Skipped automatically: .git, __pycache__, *.pyc, Office lock files (~$*), *.tmp,
.env files, .DS_Store, Thumbs.db, anything matched by the folder's .gitignore
(basic patterns), and anything you pass with --exclude.

Requires: pip install requests
"""

import argparse
import base64
import fnmatch
import getpass
import hashlib
import os
import re
import sys
import time
from urllib.parse import quote

import requests

API = "https://api.github.com"
MAX_FILE_BYTES = 100 * 1024 * 1024   # GitHub rejects files larger than 100 MB
WARN_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_EXCLUDES = [".git", "__pycache__", "*.pyc", "~$*", "*.tmp", ".env", ".env.*",
                    ".DS_Store", "Thumbs.db"]
TOKEN_HELP = ("GitHub does not accept your account password here.\n"
              "Create a Personal Access Token at https://github.com/settings/tokens\n"
              "(Tokens (classic) -> Generate new token -> tick 'repo') and paste it as the password.")


class GitHubError(RuntimeError):
    pass


# --------------------------------------------------------------------------- API client
class GitHub:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self.user = None

    def call(self, method, path, ok=(200, 201), **kw):
        r = self.s.request(method, API + path, timeout=120, **kw)
        if r.status_code not in ok:
            raise GitHubError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    def login(self):
        r = self.s.get(API + "/user", timeout=30)
        if r.status_code == 401:
            raise GitHubError("Login failed (401). " + TOKEN_HELP)
        r.raise_for_status()
        self.user = r.json()["login"]
        return self.user

    def get_repo(self, owner, name):
        r = self.s.get(f"{API}/repos/{owner}/{name}", timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise GitHubError(f"Cannot read {owner}/{name}: {r.status_code} {r.text[:200]}")
        return r.json()

    def create_repo(self, owner, name, private, description=""):
        path = "/user/repos" if owner.lower() == self.user.lower() else f"/orgs/{owner}/repos"
        body = {"name": name, "private": private, "auto_init": True, "description": description}
        repo = self.call("POST", path, json=body).json()
        for _ in range(15):  # the initial commit can take a moment to appear
            if self.branch_head(owner, name, repo["default_branch"]):
                break
            time.sleep(2)
        return repo

    def branch_head(self, owner, name, branch):
        """Latest commit sha of a branch, or None if the branch doesn't exist."""
        r = self.s.get(f"{API}/repos/{owner}/{name}/git/ref/heads/{quote(branch)}", timeout=30)
        return r.json()["object"]["sha"] if r.status_code == 200 else None

    def ensure_branch(self, owner, name, branch, default_branch):
        """Return the head commit of `branch`, creating it (or a first commit) if needed."""
        head = self.branch_head(owner, name, branch)
        if head:
            return head
        if not self.branch_head(owner, name, default_branch):  # empty repository
            self.call("PUT", f"/repos/{owner}/{name}/contents/.gitkeep",
                      json={"message": "Initial commit", "content": ""})
        base = self.branch_head(owner, name, default_branch)
        if branch != default_branch:
            print(f"  Creating branch '{branch}' from '{default_branch}'")
            self.call("POST", f"/repos/{owner}/{name}/git/refs",
                      json={"ref": f"refs/heads/{branch}", "sha": base})
        return base

    def remote_files(self, owner, name, commit_sha):
        """{path: blob sha} for every file in the commit."""
        tree = self.call("GET", f"/repos/{owner}/{name}/git/commits/{commit_sha}").json()["tree"]["sha"]
        data = self.call("GET", f"/repos/{owner}/{name}/git/trees/{tree}?recursive=1").json()
        if data.get("truncated"):
            print("  ! Repository is very large; GitHub returned a partial file list. "
                  "Some unchanged files may be re-uploaded.")
        return {e["path"]: e["sha"] for e in data["tree"] if e["type"] == "blob"}

    def commit(self, owner, name, branch, head, uploads, deletes, message):
        """One commit that adds/updates `uploads` [(repo_path, local_path)] and removes `deletes`."""
        git = f"/repos/{owner}/{name}/git"
        base_tree = self.call("GET", f"{git}/commits/{head}").json()["tree"]["sha"]
        tree = []
        for i, (repo_path, local_path) in enumerate(uploads, 1):
            with open(local_path, "rb") as fh:
                content = base64.b64encode(fh.read()).decode()
            sha = self.call("POST", f"{git}/blobs",
                            json={"content": content, "encoding": "base64"}).json()["sha"]
            mode = "100755" if _is_executable(local_path) else "100644"
            tree.append({"path": repo_path, "mode": mode, "type": "blob", "sha": sha})
            print(f"  [{i}/{len(uploads)}] uploaded {repo_path}")
        for repo_path in deletes:
            tree.append({"path": repo_path, "mode": "100644", "type": "blob", "sha": None})
            print(f"  deleted {repo_path}")
        new_tree = self.call("POST", f"{git}/trees", json={"base_tree": base_tree, "tree": tree}).json()["sha"]
        commit = self.call("POST", f"{git}/commits",
                           json={"message": message, "tree": new_tree, "parents": [head]}).json()["sha"]
        self.call("PATCH", f"{git}/refs/heads/{quote(branch)}", json={"sha": commit})
        return commit


def _is_executable(path):
    if os.name == "nt":
        return False
    return os.access(path, os.X_OK)


# --------------------------------------------------------------------------- local files
def blob_sha(path):
    """The SHA git uses for a file's contents, to compare with what is on GitHub."""
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def read_gitignore(folder):
    path = os.path.join(folder, ".gitignore")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.startswith(("#", "!"))]


def is_excluded(rel_path, patterns):
    parts = rel_path.split("/")
    for pat in patterns:
        p = pat.strip("/")
        if not p:
            continue
        if "/" in p:
            if fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(rel_path, p + "/*"):
                return True
        elif any(fnmatch.fnmatch(part, p) for part in parts):
            return True
    return False


def repo_path_for(rel_path, prefix):
    prefix = prefix.strip("/").replace("\\", "/")
    return f"{prefix}/{rel_path}" if prefix else rel_path


def collect_files(folder, prefix="", excludes=(), only=None):
    """{repo_path: local_path} for the files to upload.
    `only` = list of files/folders (relative to `folder` or absolute) to limit the upload to."""
    folder = os.path.abspath(folder)
    patterns = list(DEFAULT_EXCLUDES) + read_gitignore(folder) + list(excludes)
    roots = []
    for item in (only or [folder]):
        p = os.path.abspath(item if os.path.isabs(item) else os.path.join(folder, item))
        if not os.path.exists(p):
            raise GitHubError(f"Not found: {item}")
        if os.path.commonpath([p, folder]) != folder:
            raise GitHubError(f"{item} is outside the folder {folder}")
        roots.append(p)

    out = {}
    for root in roots:
        walk = [(os.path.dirname(root), [], [os.path.basename(root)])] if os.path.isfile(root) else os.walk(root)
        for current, dirs, names in walk:
            rel_dir = os.path.relpath(current, folder).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            dirs[:] = [d for d in dirs if not is_excluded(f"{rel_dir}/{d}".lstrip("/"), patterns)]
            for n in names:
                rel = f"{rel_dir}/{n}".lstrip("/")
                if not os.path.isfile(root) and is_excluded(rel, patterns):  # files named explicitly are kept
                    continue
                local = os.path.join(current, n)
                size = os.path.getsize(local)
                if size > MAX_FILE_BYTES:
                    print(f"  ! skipped {rel}: {size / 2**20:.0f} MB is over GitHub's 100 MB limit")
                    continue
                if size > WARN_FILE_BYTES:
                    print(f"  ! {rel} is {size / 2**20:.0f} MB; GitHub recommends files under 50 MB")
                out[repo_path_for(rel, prefix)] = local
    return out


# --------------------------------------------------------------------------- sync
def plan(local, remote, prefix="", mirror=False, excludes=()):
    new, changed, same = [], [], []
    for path, local_path in sorted(local.items()):
        if path not in remote:
            new.append(path)
        elif remote[path] != blob_sha(local_path):
            changed.append(path)
        else:
            same.append(path)
    deleted = []
    if mirror:
        pre = prefix.strip("/")
        patterns = list(DEFAULT_EXCLUDES) + list(excludes)
        for path in sorted(remote):
            inside = not pre or path.startswith(pre + "/")
            rel = path[len(pre) + 1:] if pre else path
            if inside and path not in local and not is_excluded(rel, patterns):
                deleted.append(path)
    return new, changed, same, deleted


def sync(gh, owner, repo, branch, local, message, prefix="", mirror=False,
         excludes=(), confirm=True, dry_run=False):
    """Upload new/changed files (and delete removed ones if mirror). Returns commit sha or None."""
    info = gh.get_repo(owner, repo)
    head = gh.branch_head(owner, repo, branch)
    if not head and not dry_run:
        head = gh.ensure_branch(owner, repo, branch, info["default_branch"])
    remote = gh.remote_files(owner, repo, head) if head else {}
    new, changed, same, deleted = plan(local, remote, prefix, mirror, excludes)

    print(f"\n  {len(new)} new, {len(changed)} changed, {len(same)} unchanged"
          + (f", {len(deleted)} to delete" if mirror else ""))
    for label, items in (("+ new", new), ("~ changed", changed), ("- delete", deleted)):
        for p in items:
            print(f"    {label:<10} {p}")
    if not (new or changed or deleted):
        print("  Everything is already up to date on GitHub.")
        return None
    if dry_run:
        print("  Dry run: nothing was uploaded.")
        return None
    if confirm and not ask_yes(f"\n  Commit these changes to {owner}/{repo} ({branch})?", default=True):
        print("  Cancelled, nothing was uploaded.")
        return None

    print()
    uploads = [(p, local[p]) for p in new + changed]
    sha = gh.commit(owner, repo, branch, head, uploads, deleted, message)
    print(f"\n  Done: https://github.com/{owner}/{repo}/commit/{sha}")
    return sha


def file_url(owner, repo, branch, path, download=False):
    kind = "raw" if download else "blob"
    return f"https://github.com/{owner}/{repo}/{kind}/{quote(branch)}/{quote(path)}"


# --------------------------------------------------------------------------- prompts
def ask(prompt, default=""):
    shown = f" [{default}]" if default else ""
    return input(f"{prompt}{shown}: ").strip() or default


def ask_yes(prompt, default=False):
    ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not ans else ans.startswith("y")


def login_interactive(username=None, token=None):
    """Ask for username + password (token) in the terminal and log in."""
    username = username or ask("GitHub username")
    token = token or os.environ.get("GITHUB_TOKEN") or getpass.getpass(
        "GitHub password (Personal Access Token, input hidden): ").strip()
    if not token:
        raise GitHubError("A token is required. " + TOKEN_HELP)
    gh = GitHub(token)
    login = gh.login()
    if username and login.lower() != username.lower():
        print(f"  Note: this token belongs to '{login}'.")
    print(f"  Logged in as {login}")
    return gh, (username or login)


def safe_repo_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:100] or "my-repo"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Upload a folder to GitHub or update files already there.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("folder", nargs="?", help="folder to upload (asked if missing)")
    ap.add_argument("--repo", help="repository name, or owner/name")
    ap.add_argument("--user", help="GitHub username")
    ap.add_argument("--branch", help="branch to upload to (default: repository's default branch)")
    ap.add_argument("-m", "--message", help="commit message")
    ap.add_argument("--files", nargs="+", help="only upload/update these files or sub-folders")
    ap.add_argument("--prefix", default="", help="folder inside the repository to put files in")
    ap.add_argument("--exclude", action="append", default=[], help="extra pattern to skip (repeatable)")
    ap.add_argument("--mirror", action="store_true",
                    help="also delete files from GitHub that no longer exist locally")
    ap.add_argument("--private", action="store_true", help="make the repository private if it is created")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--dry-run", action="store_true", help="only show what would change")
    a = ap.parse_args(argv)
    interactive = not a.yes

    print("GitHub upload / update\n")
    folder = os.path.abspath(a.folder or ask("Folder to upload", os.getcwd()))
    if not os.path.isdir(folder):
        raise GitHubError(f"Folder not found: {folder}")

    gh, username = login_interactive(a.user)
    repo_arg = a.repo or ask("Repository (name or owner/name)", safe_repo_name(os.path.basename(folder)))
    owner, _, repo = repo_arg.rpartition("/")
    owner = owner or gh.user

    info = gh.get_repo(owner, repo)
    if info is None:
        if a.dry_run:
            raise GitHubError(f"{owner}/{repo} does not exist yet (dry run, not creating it).")
        if interactive and not ask_yes(f"  {owner}/{repo} does not exist. Create it?", default=True):
            return 1
        private = a.private or (interactive and ask_yes("  Make it private?", default=False))
        print(f"  Creating {owner}/{repo} ({'private' if private else 'public'})")
        info = gh.create_repo(owner, repo, private)
    else:
        print(f"  Using {info['full_name']} ({'private' if info['private'] else 'public'})")
    branch = a.branch or info["default_branch"]

    only = a.files
    mirror = a.mirror
    if interactive and not a.files and not a.mirror:
        picked = ask("Files to upload, comma separated (Enter = whole folder)")
        if picked:
            only = [p.strip().strip('"') for p in picked.split(",") if p.strip()]
        else:
            mirror = ask_yes("Also delete files on GitHub that you deleted locally?", default=False)
    if only and mirror:
        print("  Note: --mirror is ignored when uploading specific files.")
        mirror = False

    default_msg = f"Upload/update files from {os.path.basename(folder)}"
    message = a.message or (ask("Commit message", default_msg) if interactive else default_msg)

    local = collect_files(folder, a.prefix, a.exclude, only)
    if not local:
        raise GitHubError("No files to upload.")
    sha = sync(gh, owner, repo, branch, local, message, a.prefix, mirror, a.exclude,
               confirm=interactive, dry_run=a.dry_run)
    if sha:
        print(f"  Repository: https://github.com/{owner}/{repo}/tree/{quote(branch)}")
        if only:
            for p in sorted(local):
                print(f"  {file_url(owner, repo, branch, p)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except (GitHubError, requests.RequestException) as e:
        sys.exit(f"\nError: {e}")
