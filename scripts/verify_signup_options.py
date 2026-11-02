#!/usr/bin/env python3
"""Verify what the sign-up form is told by Flask: the schools to choose from, read from model/dataconfigs/schools.json.

Runs the real app against a throwaway SQLite database, and checksums the real dev DB before and
after to prove it was never touched.

Usage:  python scripts/verify_signup_options.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import copy, hashlib, json, os, sys, tempfile
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-signup-options")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
# the schools the sign-up form listed before they became data
FORM_SCHOOLS = [("Abraxas High School", "Abraxas"), ("Del Norte High School", "Del Norte"), ("Mt Carmel High School", "Mt Carmel"),
                ("Poway High School", "Poway"), ("Poway to Palomar", "Poway to Palomar"),
                ("Rancho Bernardo High School", "Rancho Bernardo"), ("Westview High School", "Westview")]
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None

    import __init__ as pkg
    from sqlalchemy import create_engine
    app, db = pkg.app, pkg.db
    with app.app_context():
        db.engines[None] = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='signupopts-'), 'new.db')}")
    import main  # noqa: F401  registers every model and blueprint
    from model import schools
    app.config["TESTING"] = True

    with open(os.path.join(REPO, "model", "dataconfigs", "schools.json"), encoding="utf-8") as fh:
        on_disk = json.load(fh)

    with app.test_client() as c:  # nobody is logged in: the sign-up form is shown to people with no account
        r = c.get("/api/signup-options")
        body = r.get_json() or {}
        check("anyone can ask what the sign-up form offers, with no login (200)", r.status_code == 200, r.status_code)
        check("it lists the schools from model/dataconfigs/schools.json, in that order", body.get("schools") == on_disk["schools"], str(body.get("schools"))[:120])
        check("each school has a name to send and a label to show", all(set(s) == {"name", "label"} and s["name"] and s["label"] for s in body.get("schools", [])))
        check("the list is the seven schools the form had before it was data",
              [(s["name"], s["label"]) for s in body.get("schools", [])] == FORM_SCHOOLS, str(body.get("schools"))[:160])
        check("it offers nothing else yet", set(body) == {"schools"}, str(sorted(body)))
        check("it only answers GET (POST 405, DELETE 405)", c.post("/api/signup-options", json={}).status_code == 405 and c.delete("/api/signup-options").status_code == 405)

        changed = copy.deepcopy(schools._data)
        changed["schools"] = [{"name": "Test Academy", "label": "Test"}]
        with patch.object(schools, "_data", changed):
            check("the list follows the data: change the data and the answer changes", c.get("/api/signup-options").get_json() == {"schools": [{"name": "Test Academy", "label": "Test"}]})
        check("and follows it back again", c.get("/api/signup-options").get_json()["schools"] == on_disk["schools"])

    def refused(mutate):
        bad = copy.deepcopy(schools._data)
        mutate(bad)
        try:
            schools._check(bad)
            return False
        except ValueError:
            return True
    check("startup refuses data with no schools key, an empty list, or a list that is not a list",
          refused(lambda d: d.pop("schools")) and refused(lambda d: d.update(schools=[])) and refused(lambda d: d.update(schools="Poway")))
    check("startup refuses a school that is not a name and a label", refused(lambda d: d["schools"].append("Poway"))
          and refused(lambda d: d["schools"].append({"name": "X"})) and refused(lambda d: d["schools"].append({"name": "X", "label": "X", "extra": 1})))
    check("startup refuses a blank or non-text name or label",
          refused(lambda d: d["schools"].append({"name": " ", "label": "X"})) and refused(lambda d: d["schools"].append({"name": "X", "label": ""}))
          and refused(lambda d: d["schools"].append({"name": 7, "label": "X"})))
    check("startup refuses two schools with the same name", refused(lambda d: d["schools"].append(dict(d["schools"][0]))))

    real_after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("the real dev database was not touched", real_before == real_after)

    print(f"\n{'ALL PASSED' if not _failures else f'{len(_failures)} FAILED'}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
