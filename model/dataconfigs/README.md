# dataconfigs (model/dataconfigs/)

The values that make this deployment *this* deployment, kept as data instead of code. A school or
organisation that forks OCS changes the files here, not the Python. Code that decides who may do
something, or what a form offers, reads these files; it does not name a role, a school or an email domain itself.

| File | What it holds | Read by | Served to the pages by |
|---|---|---|---|
| `account_types.json` | The kinds of account, their roles, who counts as staff, how long a verification lasts, what each type may do, and how each behaves on the sign-up form | `model/account_types.py` | `GET /api/signup-options` (the form's part only) |
| `schools.json` | The schools a student can choose at sign-up | `model/schools.py` | `GET /api/signup-options` |
| `permissions.json` | Who may make each request: open to anyone, any signed-in account, or only some roles | `model/permissions.py` | not sent |

Each file is added to this folder in the change that makes its values data. Data files that are not
here yet: `model/presence_schedules.json`, the starter bell schedules for the presence system.

## How they are loaded

- **Once, at startup.** The files here are read and checked when the app starts. **A change needs a
  restart**, and in production a redeploy, because the files are copied into the image.
- **Bad data stops the app at startup**, with a message naming the file and the field
  (`account_types.json: guest.lifetime_hours must be a whole number, 1 or more`). That is on
  purpose: a typo fails where you see it, not later on somebody's login. The rules are in the
  `_check` function of each loader.

## account_types.json

Top level:

| Key | Meaning |
|---|---|
| `admin_role` | The one role that can do everything, and the only one that can make another account an Admin. Must be one of `staff_roles`. |
| `staff_roles` | Roles that manage accounts. They see the staff controls in Account Administration and can use the staff endpoints. |
| `essential_roles` | Roles of which at least one active account must always remain. |
| `verification_interval_days` | How long a verification stays current, and the most a Teacher/Admin can grant. |
| `default_verification_days` | The number suggested when a Teacher/Admin is asked how long to verify for. |
| `verification_exempt_roles` | Roles that are never re-verified. |
| `default_account_type` | The type used when a sign-up names none, or one that is not listed. It must be offered on the sign-up form (have a `signup_label`). |
| `account_types` | The types, by name. See below. |

Each entry in `account_types` has a `role` (every type needs its own) and any of these. The comments
at the top of `model/account_types.py` are the full reference; the groups are:

- **Who it is:** `google_verified_domains` (an email in one of these, verified by Google, activates the
  account at sign-up), `staff_created`, `verification_days`.
- **Limits:** `github_check` (default true), `active_on_signup`, `lifetime_hours`,
  `allowed_requests` (the only `"METHOD /path"` requests the type may make),
  `requests_per_minute`.
- **The Guest Window:** `requires_open_window` (the type can be created or come back only while a
  Teacher/Admin has the window open) and `window_max_minutes`.
- **Spring:** `spring_role` (the role Spring is told these accounts have at login, such as `ROLE_MENTOR`: granted while the account is active, taken away when it is not; see `model/role_attestation.py`). Never an Admin or Teacher role.
- **The sign-up form:** `signup_label` (offered, with this text; a type without one is not offered),
  `signup_note`, `asks_school`, `email_required`, `spring_account`, and for the Google step `google_signin_domains` (the email domains the form accepts, which can be more than the ones that activate at once) and `google_prompt`.

A type that skips the GitHub check must have a list of allowed requests and the Guest Window, and its
role cannot be a staff role. The loader refuses anything else.

## schools.json

A list of `{"name": ..., "label": ...}`. The **name** is saved on the account; the **label** is what the
form shows. Names must be unique. Nothing checks an account's school against the list: it is what the
form offers, not a rule about who can have an account.

## permissions.json

One file that answers "who may call this?" for every request the app has. A request is written
`"METHOD route"`, with the route as Flask knows it: `"GET /api/id"`, `"POST /api/user/<string:uid>/verify"`.
Every request is in exactly one of these lists. **A request in none of them is one nobody may make.**

| List | Who may make the request |
|---|---|
| `public` | Anyone, with or without an account, on purpose: the home page, login, sign-up. |
| `public_restricted` | Anyone, on purpose, but the route runs code or spends a provider's credit. Nothing goes here until it has been reviewed to run in a sandbox and under a rate limit. Empty today. |
| `legacy_open` | Anyone, because it has always been so and nobody has yet decided that it should be. Groups of requests, each with an `owner`. The aim is for this list to get shorter: each request moves to `public`, or to a list that needs an account. |
| `device_key` | A device that sends the device key. The route checks the key. |
| `session` | A page behind the Flask session login. |
| `signed_in` | Any active account. |
| `roles` | For each role, the requests only some roles may make. A request for Teachers and Admins is in both lists. |

**What is still in `legacy_open`:** the NPC game's five requests (`/api/ainpc/...`), which the game builder needs for
visitors with no account, and `POST /api/feedback/`, which the feedback form allows from "Anonymous". Both spend
something (a provider's credit, a GitHub issue), so they move to `public_restricted` once they have a rate limit.
Everything else that was here has been sorted into `public`, `signed_in` or `roles`.

A rule about whose record a route may touch is not in this file. `GET /api/feedback/user/<uid>` is `signed_in`, and
the route itself returns 403 unless the account is that uid or an Admin.

`open_in_development` is not a list of its own kind: it names requests that are open while `IS_PRODUCTION`
is not true. Each must also be in `signed_in` or `roles`, which says who may make it in production.

**Every request is checked against this file before it reaches a route** (`api/access.py`). The guards on the
routes stay, and a check written inside a route (an account may change only its own record) stays in the route.
What the check does with its decision depends on `enforce`:

| `enforce` | What happens to a caller the file does not allow |
|---|---|
| `[]` (shadow mode) | Nothing changes. If the route answers anyway, a line is written to the log: `permissions (shadow): would refuse GET /api/x for anonymous`. |
| `["anonymous"]` | A caller with no usable login is refused here, with the answer the route's own guard gives. Signed-in callers are still in shadow mode. |
| `["anonymous", "Guest", "User", ...]` | The same for each role named. |

Once a caller is in `enforce`, the file is what decides for them:

- **A route in no list refuses them**, even if the route itself has no guard. A new route cannot be open by accident.
- **Moving an entry closes a route** with no change to the code: a `legacy_open` request moved to
  `signed_in` now needs a login. Opening a route that has a guard takes a change to the guard as well, and
  `verify_permissions.py` reports the two disagreeing until it is made.

In shadow mode a mistake in the check is logged and the request goes on, so the check cannot be the reason a
request fails.

The loader refuses a request that is in two lists, a role that `account_types.json` does not have, an entry
that is not `"METHOD /route"`, and a `legacy_open` group with no owner.

`python scripts/verify/verify_permissions.py` is the check to run whenever a route is added or changed. It fails when:

- a route has no entry, or an entry has no route;
- the file and the guard on a route say different things, in development or in production. Once `enforce`
  names every caller the file may hold a route tighter than its guard does (that is how a route is closed from
  data); it may never be looser;
- a request not listed as open answers a caller with no account, when really called;

It calls the routes on a throwaway copy of the app with no network and no provider keys
(`scripts/rbac/rbac_prober.py`), so it is safe to run anywhere.

`python scripts/verify/verify_access.py` is the check to run before adding a caller to `enforce`. It calls every
route as every kind of caller, in development and production mode, once in shadow mode and once enforced
for everyone, and fails if any answer differs, except for the requests the file holds tighter than their guard on
purpose (`HELD_TIGHTER` in the script): those may change only to a 401 or 403 from the check, and the list must be exact.
It takes about four minutes.

## What the sign-up page is told

`GET /api/signup-options` is public, because the form is shown to people with no account. It sends the
schools, the offered account types with how each behaves, and the type to start on. It sends nothing about roles, lifetimes or limits. The pages build the form from it. They keep their
own list of schools and of the student and mentor types, used only when Flask cannot be asked (an older Flask, an error), so sign-up
never ends up with nothing to choose.

## Changing something

1. **Add a school:** add an entry to `schools.json`, restart.
2. **Add an account type:** add it to `account_types.json` with its own `role` and, to offer it on the
   form, a `signup_label`. Say how it behaves with the flags above. Restart. No page change.
3. **Rename a role** (say `Teacher` becomes `Instructor`): change it in `staff_roles`, in the type, and in
   the accounts already saved (a database update: roles are stored on each account). Code does not name
   the roles, so that is all.
4. **Add or change a route:** add its request to the list that says who may make it in `permissions.json`,
   and guard the route to match (`@token_required()` for `signed_in`, `@token_required([...])` for `roles`).
   `python scripts/verify/verify_permissions.py` fails until the two agree. A new route goes in `public` only when it
   is meant for people with no account, and never in `legacy_open`.
5. **Run the checks** from the repo root, each against a throwaway database:
   `python scripts/verify/verify_signup_options.py` (what the form is told),
   `python scripts/verify/verify_account_admin.py`,
   `python scripts/verify/verify_activation.py`,
   `python scripts/verify/verify_dataconfigs.py` (that these files are here and explained),
   `python scripts/verify/verify_permissions.py` (that `permissions.json` matches the routes),
   `python scripts/verify/verify_feedback_access.py` (who may read feedback),
   `python scripts/verify/verify_access.py` (that enforcing `permissions.json` changes no answer). Set
   `DEFAULT_PASSWORD`, `ADMIN_PASSWORD`, `TEACHER_PASSWORD`, `USER_PASSWORD` and `MY_PASSWORD` to
   passwords that follow the password rule if your `.env` has weak ones.

## What does not belong here

- **Secrets and per-environment values** (keys, passwords, URLs, the seeded accounts): those are
  environment variables, in `.env`.
- **Anything about one person.** These files are the same for everyone.
- **Spring.** The Java backend has its own roles and settings and does not read these files yet.

## Not yet data

Still in code, and candidates to move here: the default role `"User"` in the model and the restore code,
`User.is_teacher()`, the seeded accounts' roles, the password rule (it must stay in step with Spring's),
the starter sections (CSP, CSSE, CSA), and the allowed origins for cross-origin requests.
