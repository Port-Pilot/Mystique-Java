"""
Export rows from backport_benchmark_results_mystique_java into the
{id: [old_sha, new_sha]} shape Mystique's cve-java.json uses, plus a
companion {id: "owner/repo"} mapping (since cve-java.json alone doesn't
carry the repo — you'll need to check how Mystique's src/ resolves the
repo for each id and adjust the companion file's shape to match).

Usage:
    pip install psycopg2-binary python-dotenv --break-system-packages
    # put NEON_DATABASE_URL=... in a .env file next to this script
    python export_to_mystique.py
"""

import json
import os
import re
import sys
from collections import defaultdict

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("NEON_DATABASE_URL")
if not DATABASE_URL:
    sys.exit("Set NEON_DATABASE_URL in your environment or a .env file.")

# Matches https://github.com/<owner>/<repo>/commit/<sha> (also works for
# /commits/, and short or full SHAs)
COMMIT_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/commits?/(?P<sha>[0-9a-f]{7,40})"
)


def parse_commit_url(url):
    """Return (owner/repo, sha) or (None, None) if it doesn't match."""
    if not url:
        return None, None
    m = COMMIT_URL_RE.search(url)
    if not m:
        return None, None
    return f"{m.group('owner')}/{m.group('repo')}", m.group("sha")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, dataset, project, old_version_patch_commit_url,
               new_version_patch_commit_url, patch_type
        FROM backport_benchmark_results_mystique_java
        WHERE lower(programming_language) = 'java'
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    cve_style = {}          # {id: [old_sha, new_sha]}
    repo_map = {}           # {id: "owner/repo"}
    skipped = []
    repo_mismatches = []

    for row_id, dataset, project, old_url, new_url, patch_type in rows:
        old_repo, old_sha = parse_commit_url(old_url)
        new_repo, new_sha = parse_commit_url(new_url)

        if not old_sha or not new_sha:
            skipped.append((row_id, old_url, new_url))
            continue

        if old_repo != new_repo:
            # Ported across forks/mirrors — flag it, don't silently drop
            repo_mismatches.append((row_id, old_repo, new_repo))

        key = f"{dataset}-{project}-{row_id}" if dataset else f"{project}-{row_id}"
        cve_style[key] = [old_sha, new_sha]
        repo_map[key] = old_repo or new_repo

    with open("custom-java.json", "w") as f:
        json.dump(cve_style, f, indent=2)

    with open("custom-java-repos.json", "w") as f:
        json.dump(repo_map, f, indent=2)

    print(f"Wrote {len(cve_style)} records to custom-java.json")
    print(f"Wrote {len(repo_map)} records to custom-java-repos.json")
    if skipped:
        print(f"\nSkipped {len(skipped)} rows (unparseable commit URL), e.g.:")
        for r in skipped[:5]:
            print(" ", r)
    if repo_mismatches:
        print(f"\n{len(repo_mismatches)} rows have different old/new repos (fork/mirror?), e.g.:")
        for r in repo_mismatches[:5]:
            print(" ", r)


if __name__ == "__main__":
    main()