"""
Run this from inside Mystique-Java/mystique-opensource.github.io/src/, since
it imports Patch/Project/Method/Language from that package.

Turns custom-java.json ({id: [old_sha, new_sha]}) + custom-java-repos.json
({id: "owner/repo"}) into the nested structure batch_run_multiprocess_java()
expects:

{
  "<id>": {
    "patch": {
      "<file_path>#<method_name>": {
        "origin_before_func_code": ..., "origin_after_func_code": ...,
        "target_before_func_code": ..., "target_after_func_code": ...,
        "origin_before_file_code": ..., "origin_after_file_code": ...,
        "target_before_file_code": ..., "target_after_file_code": ...,
        "origin_before_func_signature": ..., "origin_after_func_signature": ...,
        "target_before_func_signature": ..., "target_after_func_signature": ...
      }
    }
  }
}

Usage:
    python build_mystique_java_input.py
"""

import json
import os
import subprocess

from common import Language
from patch import Patch

REPOS_DIR = "repos"
SHA_FILE = "../../custom-java.json"          
REPO_MAP_FILE = "../../custom-java-repos.json"
OUTPUT_FILE = "../../cve-java-custom-full.json"


def ensure_repo_cloned(owner_repo: str) -> str:
    """Clone owner/repo into REPOS_DIR if not already present. Returns local path."""
    safe_name = owner_repo.replace("/", "__")
    local_path = os.path.join(REPOS_DIR, safe_name)
    if os.path.isdir(os.path.join(local_path, ".git")):
        return local_path
    os.makedirs(REPOS_DIR, exist_ok=True)
    url = f"https://github.com/{owner_repo}.git"
    print(f"Cloning {url} ...")
    subprocess.run(["git", "clone", "--quiet", url, local_path], check=True)
    return local_path


def method_key(pre_method) -> str:
    """file_path#method_name, matching how batch_run_multiprocess_java splits keys."""
    return f"{pre_method.file.path}#{pre_method.name}"


def build_record_for_id(cve_id: str, old_sha: str, new_sha: str, owner_repo: str):
    repo_path = ensure_repo_cloned(owner_repo)

    try:
        origin_patch = Patch(repo_path, old_sha, Language.JAVA)
        target_patch = Patch(repo_path, new_sha, Language.JAVA)
    # except Exception as e:
    #     print(f"[{cve_id}] Failed to build Patch objects: {e}")
    #     return None
    except Exception:
        import traceback
        print(f"[{cve_id}] Failed to build Patch objects:")
        traceback.print_exc()
        return None

    patch_dict = {}
    for sig in origin_patch.changed_methods:
        pre_method = origin_patch.pre_project.get_method(sig)
        post_method = origin_patch.post_project.get_method(sig)
        if pre_method is None or post_method is None:
            continue

        # Same repo, different commit -> try the same signature in the target commit
        target_pre_method = target_patch.pre_project.get_method(sig)
        target_post_method = target_patch.post_project.get_method(sig)
        if target_pre_method is None or target_post_method is None:
            print(f"[{cve_id}] No matching target method for signature: {sig}")
            continue

        key = method_key(pre_method)
        patch_dict[key] = {
            "origin_before_func_code": pre_method.code,
            "origin_after_func_code": post_method.code,
            "target_before_func_code": target_pre_method.code,
            "target_after_func_code": target_post_method.code,
            "origin_before_file_code": pre_method.file.code,
            "origin_after_file_code": post_method.file.code,
            "target_before_file_code": target_pre_method.file.code,
            "target_after_file_code": target_post_method.file.code,
            "origin_before_func_signature": pre_method.signature,
            "origin_after_func_signature": post_method.signature,
            "target_before_func_signature": target_pre_method.signature,
            "target_after_func_signature": target_post_method.signature,
        }

    if not patch_dict:
        print(f"[{cve_id}] No usable method-level patches found, skipping.")
        return None

    return {"patch": patch_dict}


def main():
    with open(SHA_FILE) as f:
        sha_data = json.load(f)
    with open(REPO_MAP_FILE) as f:
        repo_map = json.load(f)

    output = {}
    for cve_id, (old_sha, new_sha) in sha_data.items():
        owner_repo = repo_map.get(cve_id)
        if not owner_repo:
            print(f"[{cve_id}] No repo mapping found, skipping.")
            continue
        record = build_record_for_id(cve_id, old_sha, new_sha, owner_repo)
        if record is not None:
            output[cve_id] = record

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {len(output)} CVE records (of {len(sha_data)} attempted) to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()