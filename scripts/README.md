# scripts/

This file lists the **access-control (RBAC) scripts**: what each one is, why it exists, and when to run it. The older
`verify_*.py` and `db_*.py` scripts are not listed here yet; each has its purpose in the docstring at the top of the file.

None of these scripts is part of the running app, and nothing in the app imports them. Run each from the repo root.

- The scripts that call routes (`rbac_prober.py`, and anything that runs it) do it on a throwaway copy of the app: a copy of
  the tracked files, a clean environment with no provider keys, no network, and fresh throwaway data for every call. They
  cannot change your database or your files, and they cannot spend a provider's credit.
- `rbac_inventory.py` calls no route. It loads the app the way starting it does, which can create the app's usual empty
  `instance/` files if they are missing.
- The `verify_*.py` scripts that need accounts use a throwaway SQLite database and leave yours alone.

## Where things are

| Folder | What is in it |
|---|---|
| `verify/` | The checks: each `verify_*.py` proves one thing about the app and exits 0 when it holds. `run_all.py` runs them all. |
| `rbac/` | The access-control tools (inventory, prober, reconcile) and the small apps and the labelled list the checks use. |
| `scripts/` (here) | Tools for running the app and its data: `db_*.py`, `init_posts.py`, `remove_user.py`, `copy_spring_preferences.py`, `venv.sh`, and `old/`. |

To run every check: `python scripts/verify/run_all.py` (five to six minutes; it names each check as it goes, and shows the end
of the output of any that fails). `python scripts/verify/run_all.py permissions cors` runs only the checks with those words
in their name, and `--list` prints the names.

## How they fit together

```
rbac/rbac_inventory.py   what the code says guards each route ----+
                                                             +--> rbac/rbac_reconcile.py --> where they differ
rbac/rbac_prober.py      what each route really answers ----------+                |
                                                                              v
model/dataconfigs/permissions.json   who may make each request (data)
      |
      +--> verify/verify_permissions.py   fails when the file and the routes disagree
      |
      +--> api/access.py   checks every request against it (shadow mode until `enforce` names callers)
                 |
                 +--> verify/verify_access.py   proves enforcing it changes no answer
```

Reports from `rbac_inventory.py`, `rbac_prober.py` and `rbac_reconcile.py` show which routes are open, so write them
outside the repo with `--out` and do not commit or post them. With no `--out` they print counts only.

## The scripts

| Script | What it does | Why it exists | When to run | Takes |
|---|---|---|---|---|
| `rbac/rbac_inventory.py` | Lists every route and what the code says guards it: the decorators, the role list the running app holds, what the function itself checks about the caller. Reads only; never calls a route. | Nobody can see the guards of all 250 method-and-route pairs at once. This is the written side of the evidence. | To see how every route is guarded, after adding or changing routes, and as input to `rbac_reconcile.py`. | 3 s |
| `rbac/rbac_fixture_app.py` | A tiny app with one route for each way this project guards (or fails to guard) a route. | Gives `verify_rbac_inventory.py` known answers to check the inventory against. | Never on its own. | n/a |
| `verify/run_all.py` | Runs every `verify_*.py` one after the other, each in its own process, and prints pass or FAIL with the seconds for each. | So there is one command that says whether anything is broken, instead of knowing the names of the scripts. | Before opening a pull request, and after changing a route, a guard or a data file. | 5 to 6 min |
| `verify/verify_rbac_inventory.py` | Checks that the inventory reads every fixture route correctly, gives the same answer twice, and can inventory this project. | So the inventory can be trusted. | After changing `rbac_inventory.py`, or the shape of `auth_required` / `token_required`. | 2 s |
| `rbac/rbac_prober.py` | Calls every route as nine kinds of caller (anonymous, invalid token, guest, user, mentor, teacher, admin, inactive mentor, expired guest) and records the status, any redirect, whether canary data came back, and whether the call wrote. | The code can only suggest who gets in: a check inside a function, a hook or an error can make the answer differ. This is what the app really does. | Before changing who may do what, and as input to `rbac_reconcile.py`. `--only TEXT` limits it to some routes and `--callers a,b` to some callers. | 55 s for both modes |
| `rbac/rbac_probe_fixture.py` | A tiny app with one route for each case the prober must get right: open, refused, leaking, writing, crashing, trying the network, hanging. | Gives `verify_rbac_prober.py` known answers. | Never on its own. | n/a |
| `verify/verify_rbac_prober.py` | Checks every prober record against what the fixture route does, and each safety rule: clean environment, network off, fresh data, inert code payload, nothing written to the repo. | So the prober can be trusted, and so running it is known to be safe. | After changing `rbac_prober.py`. | 24 s |
| `rbac/rbac_reconcile.py` | Joins the inventory and the prober results and names each way the answers differ from the guard: a caller the guard should refuse who got in, a check inside the code, a route that fails only for anonymous callers, and so on. `--sample` also checks the reading against 40 hand-labelled routes. | One kind of evidence can be wrong; the two together show which routes need a person to look. | When you want that comparison, for example before changing a route's guard. It runs both tools itself unless given saved reports with `--inventory` and `--probe`. | 1 min (0 s from saved reports) |
| `rbac/rbac_sample_labels.py` | 40 routes labelled by a person reading each handler, drawn from every kind of guard. Data, not a program. | A tool checked only against itself proves nothing; this is the person's answer. | Never on its own. Edit it only when a labelled route's guard really changes. | n/a |
| `verify/verify_rbac_reconcile.py` | Checks each comparison rule on small records written for it, then runs the tools on this project against the 40 labelled routes. | So the comparison can be trusted. | After changing `rbac_reconcile.py` or `rbac_inventory.py`. | 65 s |
| `verify/verify_permissions.py` | Checks `model/dataconfigs/permissions.json` against the app: every route has an entry and every entry has a route, the file says what the guards say, no request not listed as open answers a caller with no account (it really calls them). | So the file can never drift from the code, and a new route cannot be added without deciding who may call it. | **Whenever a route is added or changed, and before merging.** Not run by CI yet. | 23 s |
| `verify/verify_access.py` | Checks the request check in `api/access.py` on a small app, then calls every route as every kind of caller in both modes, once in shadow mode and once enforced for everyone, and fails if any answer differs, except the requests the file holds tighter than their guard on purpose, which may change only to a 401 or 403. | This is the gate for turning enforcement on: it shows that enforcing the file changes no answer except the ones the file was written to change. | Before adding a caller to `enforce` in `permissions.json`, and after changing `api/access.py` or `api/authorize.py`. | 2.5 min |
| `verify/verify_feedback_access.py` | Checks who may read feedback: all of it only for an Admin, one account's for that account or an Admin, and that sending feedback still works with no account. Runs on a throwaway database with the GitHub call replaced. | `permissions.json` lets any signed-in account reach `GET /api/feedback/user/<uid>`; the route itself must refuse another account's uid, and this is what proves it does. | After changing `api/feedback_api.py` or the feedback entries in `permissions.json`. | 30 s |
