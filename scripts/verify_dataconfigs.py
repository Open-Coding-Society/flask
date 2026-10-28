#!/usr/bin/env python3
"""Verify that the data files live in model/dataconfigs/, are found there by the code that reads them, and
are explained in model/dataconfigs/README.md.

Usage:  python scripts/verify_dataconfigs.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import glob, json, os, re, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-dataconfigs")

# data files that are not in the folder yet: each moves in when its values become data, and the README says so
NOT_YET = ["model/presence_schedules.json"]
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def main():
    folder = os.path.join(REPO, "model", "dataconfigs")
    found = sorted(os.path.basename(p) for p in glob.glob(os.path.join(folder, "*.json")))
    check("model/dataconfigs/ holds the account types", "account_types.json" in found, str(found))
    for name in found:
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                json.load(fh)
            check(f"dataconfigs/{name} is valid JSON", True)
        except ValueError as e:
            check(f"dataconfigs/{name} is valid JSON", False, str(e))

    # no other data file is kept anywhere else, except the ones the README lists as not moved yet
    stray = sorted(os.path.relpath(p, REPO) for p in glob.glob(os.path.join(REPO, "**", "*.json"), recursive=True)
                   if not any(skip in p for skip in ("/model/dataconfigs/", "/venv/", "/node_modules/", "/instance/", "/.vscode/", "/.devcontainer/", "/.git/")))
    check("no other JSON data file is in the repo, except those the README says are not moved yet", all(p in NOT_YET for p in stray), str(stray))

    readme_path = os.path.join(folder, "README.md")
    readme = open(readme_path, encoding="utf-8").read() if os.path.exists(readme_path) else ""
    check("model/dataconfigs/README.md exists", bool(readme))
    for name in found:
        check(f"the README explains {name}", len(re.findall(re.escape(name), readme)) >= 2)
    for path in stray:
        check(f"the README says {path} is not moved yet", path in readme)
    needed = [("restart", "that a change needs a restart"), ("startup", "that bad data stops the app at startup"), ("verify_", "how to check a change")]
    if "schools.json" in found:
        needed.append(("signup-options", "what the sign-up form is sent"))
    for word, why in needed:
        check(f"the README says {why}", word in readme.lower())

    # the code that reads each file finds it in the folder
    import __init__  # noqa: F401  loads the app config
    from model import account_types
    check("the account types are read from model/dataconfigs/", bool(account_types.get("student")) and account_types.get("student")["role"])
    if "schools.json" in found:
        from model import schools
        check("the schools are read from model/dataconfigs/", len(schools.schools()) > 0)
    if "presence_schedules.json" in found:
        from model import presence_schedule
        check("the presence schedules seed file is read from model/dataconfigs/", os.path.exists(presence_schedule.SEED_FILE)
              and os.path.samefile(presence_schedule.SEED_FILE, os.path.join(folder, "presence_schedules.json")), presence_schedule.SEED_FILE)

    print(f"\n{'ALL PASSED' if not _failures else f'{len(_failures)} FAILED'}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
