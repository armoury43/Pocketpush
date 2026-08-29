#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pocketpush.py — نسخه‌ی خط‌فرمانی PocketPush

آپلود کل یه پوشه‌ی پروژه به یه ریپوی گیت‌هاب، با ساختار کامل، در یه commit واحد.
نسخه‌ی محکم‌کاری‌شده‌ی ابزار وب — بدون نیاز به مرورگر، بدون نیاز به نصب پکیج
(فقط کتابخونه‌ی استاندارد پایتون: urllib, json, base64).

استفاده:
    python3 pocketpush.py \
        --token ghp_xxxxxxxx \
        --owner USERNAME \
        --repo REPO_NAME \
        --path /path/to/project \
        --branch main \
        --message "Upload project folder"

یا فقط:
    python3 pocketpush.py
و بعد به‌صورت تعاملی همه‌چیز رو می‌پرسه.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

API_ROOT = "https://api.github.com"

ALWAYS_EXCLUDE_DIRS = {".git"}
HEAVY_EXCLUDE_DIRS = {
    "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".next", "target", ".gradle", ".idea", ".cache",
}
JUNK_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
JUNK_DIRNAMES = {"__MACOSX"}


# ---------------------------------------------------------------------------
# کمکی‌ها
# ---------------------------------------------------------------------------

def eprint(msg):
    print(msg, flush=True)


def human_size(num_bytes):
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes/1024:.1f} KB"
    return f"{num_bytes/(1024*1024):.1f} MB"


def normalize_rel_path(path):
    """بک‌اسلش، اسلش تکراری و './' اضافی رو پاک می‌کنه."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts)


def count_files_in(dir_path):
    n = 0
    for _, _, filenames in os.walk(dir_path):
        n += len(filenames)
    return n


def collect_files(root_path, exclude_heavy=True):
    """
    کل فایل‌های داخل root_path رو با مسیر نسبی برمی‌گردونه،
    و .git / پوشه‌های سنگین / فایل‌های زائد سیستمی رو حذف می‌کنه.
    """
    kept = []
    skipped_git = skipped_heavy = skipped_junk = 0
    total_bytes = 0

    for dirpath, dirnames, filenames in os.walk(root_path):
        pruned = []
        for d in list(dirnames):
            full_d = os.path.join(dirpath, d)
            if d in ALWAYS_EXCLUDE_DIRS:
                skipped_git += count_files_in(full_d)
                pruned.append(d)
            elif d in JUNK_DIRNAMES:
                skipped_junk += count_files_in(full_d)
                pruned.append(d)
            elif exclude_heavy and d in HEAVY_EXCLUDE_DIRS:
                skipped_heavy += count_files_in(full_d)
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)

        for fname in filenames:
            if fname in JUNK_FILENAMES:
                skipped_junk += 1
                continue
            full_path = os.path.join(dirpath, fname)
            rel_path = normalize_rel_path(os.path.relpath(full_path, root_path))
            if not rel_path:
                continue
            try:
                size = os.path.getsize(full_path)
            except OSError:
                skipped_junk += 1
                continue
            total_bytes += size
            kept.append((rel_path, full_path))

    return {
        "kept": kept,
        "skipped_git": skipped_git,
        "skipped_heavy": skipped_heavy,
        "skipped_junk": skipped_junk,
        "total_bytes": total_bytes,
    }


# ---------------------------------------------------------------------------
# لایه‌ی HTTP با retry خودکار
# ---------------------------------------------------------------------------

class GitHubError(Exception):
    pass


def api_request(method, url, token, body=None, retries=4):
    """
    درخواست به GitHub API با retry خودکار روی 403/429/404/5xx
    (چون این کدها بعد از نوشتن سریع پشت‌سرهم، اغلب موقتی و مربوط به
    تأخیر sync سرورهای گیت‌هاب هستن، نه خطای واقعی).
    """
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "github-folder-push-script",
    }

    last_status = None
    last_body = None

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"message": raw.decode("utf-8", "ignore")}
            last_status = e.code
            last_body = parsed

            transient = e.code in (403, 429, 404) or e.code >= 500
            if not transient or attempt == retries:
                return e.code, parsed

            wait = 1.5 * (attempt + 1)
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    pass
            eprint(f"  ⏳ پاسخ موقت {e.code}، {wait:.0f} ثانیه صبر و تلاش دوباره...")
            time.sleep(wait)

    raise GitHubError(f"خطا بعد از چند تلاش: {last_status} {last_body}")


def gh_get(url, token):
    status, body = api_request("GET", url, token)
    return status, body


def gh_post(url, token, body):
    return api_request("POST", url, token, body)


def gh_put(url, token, body):
    return api_request("PUT", url, token, body)


def gh_patch(url, token, body):
    return api_request("PATCH", url, token, body)


# ---------------------------------------------------------------------------
# منطق اصلی push
# ---------------------------------------------------------------------------

def push_folder(token, owner, repo, branch, message, root_path,
                 target_folder="", exclude_heavy=True):

    api = f"{API_ROOT}/repos/{owner}/{repo}"

    eprint(f"در حال خوندن فایل‌های {root_path} ...")
    result = collect_files(root_path, exclude_heavy=exclude_heavy)
    files = result["kept"]
    if not files:
        raise GitHubError("هیچ فایلی برای آپلود پیدا نشد (شاید همه‌چیز فیلتر شده).")

    eprint(f"{len(files)} فایل، حجم کل {human_size(result['total_bytes'])} آماده‌ست.")
    if result["skipped_git"]:
        eprint(f"  (.git نادیده گرفته شد — {result['skipped_git']} فایل)")
    if result["skipped_heavy"]:
        eprint(f"  ({result['skipped_heavy']} فایل از پوشه‌های سنگین نادیده گرفته شد)")
    if result["skipped_junk"]:
        eprint(f"  ({result['skipped_junk']} فایل زائد سیستمی نادیده گرفته شد)")

    target_folder = target_folder.strip("/")

    def repo_path_of(rel_path):
        return f"{target_folder}/{rel_path}" if target_folder else rel_path

    # --- وضعیت شاخه رو چک کن ---
    eprint(f"اتصال به {owner}/{repo} ...")
    status, ref_data = gh_get(f"{api}/git/ref/heads/{branch}", token)

    base_commit_sha = None
    base_tree_sha = None
    ref_exists = (status == 200)
    truly_empty_repo = False

    if ref_exists:
        base_commit_sha = ref_data["object"]["sha"]
        _, commit_data = gh_get(f"{api}/git/commits/{base_commit_sha}", token)
        base_tree_sha = commit_data["tree"]["sha"]
        eprint(f"شاخه‌ی {branch} پیدا شد.")
    elif status == 404:
        b_status, branches = gh_get(f"{api}/branches?per_page=1", token)
        if b_status == 404:
            raise GitHubError(f"ریپوی {owner}/{repo} پیدا نشد. اسمش رو چک کن.")
        if b_status != 200:
            raise GitHubError("دسترسی به ریپو ممکن نشد. توکن رو چک کن (باید دسترسی repo داشته باشه).")

        if branches:
            eprint(f"شاخه‌ی {branch} پیدا نشد، از روی شاخه‌ی پیش‌فرض ساخته می‌شه...")
            _, repo_info = gh_get(api, token)
            default_branch = repo_info["default_branch"]
            _, def_ref = gh_get(f"{api}/git/ref/heads/{default_branch}", token)
            base_commit_sha = def_ref["object"]["sha"]
            _, def_commit = gh_get(f"{api}/git/commits/{base_commit_sha}", token)
            base_tree_sha = def_commit["tree"]["sha"]
        else:
            truly_empty_repo = True
            eprint("ریپو کاملاً خالیه، اولین commit ساخته می‌شه.")
    else:
        raise GitHubError(f"خطا در بررسی شاخه (کد {status}).")

    upload_files = files

    # --- اگه ریپو کاملاً خالیه، اول یه فایل رو با Contents API بفرست ---
    if truly_empty_repo:
        first_rel, first_full = files[0]
        with open(first_full, "rb") as fh:
            first_b64 = base64.b64encode(fh.read()).decode("ascii")
        first_repo_path = repo_path_of(first_rel)

        eprint(f"  فعال‌سازی ریپو با {first_repo_path} ...")
        encoded_path = "/".join(urllib.parse.quote(seg) for seg in first_repo_path.split("/"))
        status, resp = gh_put(
            f"{api}/contents/{encoded_path}",
            token,
            {"message": message, "content": first_b64, "branch": branch},
        )
        if status not in (200, 201):
            raise GitHubError(f"خطا در فعال‌سازی ریپو: {resp.get('message')}")
        eprint(f"  ✓ {first_repo_path} (فایل اولیه)")

        _, ref2 = gh_get(f"{api}/git/ref/heads/{branch}", token)
        base_commit_sha = ref2["object"]["sha"]
        _, commit2 = gh_get(f"{api}/git/commits/{base_commit_sha}", token)
        base_tree_sha = commit2["tree"]["sha"]
        ref_exists = True

        upload_files = files[1:]

    # --- بقیه‌ی فایل‌ها رو به‌صورت blob آپلود کن ---
    eprint(f"در حال آپلود {len(upload_files)} فایل باقی‌مونده...")
    tree_entries = []
    for i, (rel_path, full_path) in enumerate(upload_files, start=1):
        with open(full_path, "rb") as fh:
            content_b64 = base64.b64encode(fh.read()).decode("ascii")

        status, blob = gh_post(f"{api}/git/blobs", token,
                                {"content": content_b64, "encoding": "base64"})
        if status not in (200, 201):
            raise GitHubError(f"خطا در آپلود {rel_path}: {blob.get('message')}")

        tree_entries.append({
            "path": repo_path_of(rel_path),
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })
        eprint(f"  ✓ [{i}/{len(upload_files)}] {rel_path}")

    # --- tree بساز ---
    eprint("در حال ساخت tree...")
    tree_body = {"tree": tree_entries}
    if base_tree_sha:
        tree_body["base_tree"] = base_tree_sha
    status, tree_data = gh_post(f"{api}/git/trees", token, tree_body)
    if status not in (200, 201):
        raise GitHubError(f"خطا در ساخت tree: {tree_data.get('message')}")

    # --- commit بساز ---
    eprint("در حال ساخت commit...")
    commit_body = {"message": message, "tree": tree_data["sha"]}
    if base_commit_sha:
        commit_body["parents"] = [base_commit_sha]
    status, commit_data = gh_post(f"{api}/git/commits", token, commit_body)
    if status not in (200, 201):
        raise GitHubError(f"خطا در ساخت commit: {commit_data.get('message')}")

    # --- شاخه رو آپدیت کن ---
    eprint("در حال آپدیت شاخه...")
    if ref_exists:
        status, resp = gh_patch(f"{api}/git/refs/heads/{branch}", token,
                                 {"sha": commit_data["sha"], "force": False})
    else:
        status, resp = gh_post(f"{api}/git/refs", token,
                                {"ref": f"refs/heads/{branch}", "sha": commit_data["sha"]})
    if status not in (200, 201):
        raise GitHubError(f"خطا در آپدیت شاخه: {resp.get('message')}")

    eprint(f"\n✅ تمام! {len(files)} فایل روی {owner}/{repo}@{branch} پوش شد.")
    eprint(f"https://github.com/{owner}/{repo}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="آپلود کامل یه پوشه به یه ریپوی گیت‌هاب.")
    parser.add_argument("--token", help="GitHub Personal Access Token (دسترسی repo)")
    parser.add_argument("--owner", help="یوزرنیم یا سازمان گیت‌هاب")
    parser.add_argument("--repo", help="اسم ریپو")
    parser.add_argument("--path", help="مسیر پوشه‌ی پروژه روی دیسک", default=".")
    parser.add_argument("--branch", help="اسم شاخه", default="main")
    parser.add_argument("--message", help="پیام commit", default="Upload project folder")
    parser.add_argument("--target-folder", help="زیرپوشه‌ی مقصد داخل ریپو (اختیاری)", default="")
    parser.add_argument("--include-heavy", action="store_true",
                         help="node_modules/venv/__pycache__/... رو هم آپلود کن (پیش‌فرض: نادیده گرفته می‌شن)")
    args = parser.parse_args()

    token = args.token or input("GitHub token: ").strip()
    owner = args.owner or input("Owner (یوزرنیم گیت‌هاب): ").strip()
    repo = args.repo or input("Repo: ").strip()
    path = args.path or input("مسیر پوشه‌ی پروژه [.]: ").strip() or "."
    branch = args.branch or "main"
    message = args.message or "Upload project folder"

    if not os.path.isdir(path):
        eprint(f"مسیر {path} یه پوشه‌ی معتبر نیست.")
        sys.exit(1)

    try:
        push_folder(
            token=token, owner=owner, repo=repo, branch=branch, message=message,
            root_path=path, target_folder=args.target_folder,
            exclude_heavy=not args.include_heavy,
        )
    except GitHubError as e:
        eprint(f"\n❌ خطا: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        eprint("\nمتوقف شد.")
        sys.exit(1)


if __name__ == "__main__":
    main()
