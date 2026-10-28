# dataconfigs (model/dataconfigs/)

The values that make this deployment *this* deployment, kept as data instead of code. A school or
organisation that forks OCS changes the files here, not the Python. Code that decides who may do
something, or what a form offers, reads these files; it does not name a role, a school or an email domain itself.

| File | What it holds | Read by | Served to the pages by |
|---|---|---|---|
| `account_types.json` | The kinds of account, their roles, who counts as staff, how long a verification lasts, what each type may do | `model/account_types.py` | `GET /api/signup-options` (the form's part only) |
| `schools.json` | The schools a student can choose at sign-up | `model/schools.py` | `GET /api/signup-options` |

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
| `default_account_type` | The type used when a sign-up names none, or one that is not listed. |
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

A type that skips the GitHub check must have a list of allowed requests and the Guest Window, and its
role cannot be a staff role. The loader refuses anything else.

## schools.json

A list of `{"name": ..., "label": ...}`. The **name** is saved on the account; the **label** is what the
form shows. Names must be unique. Nothing checks an account's school against the list: it is what the
form offers, not a rule about who can have an account.

## What the sign-up page is told

`GET /api/signup-options` is public, because the form is shown to people with no account. It sends the
schools. It sends nothing about roles, lifetimes or limits. The pages build the form from it. They keep their
own list of schools, used only when Flask cannot be asked (an older Flask, an error), so sign-up
never ends up with nothing to choose.

## Changing something

1. **Add a school:** add an entry to `schools.json`, restart.
2. **Rename a role** (say `Teacher` becomes `Instructor`): change it in `staff_roles`, in the type, and in
   the accounts already saved (a database update: roles are stored on each account). Code does not name
   the roles, so that is all.
3. **Run the checks** from the repo root, each against a throwaway database:
   `python scripts/verify_signup_options.py` (what the form is told),
   `python scripts/verify_account_admin.py`,
   `python scripts/verify_activation.py`,
   `python scripts/verify_dataconfigs.py` (that these files are here and explained). Set
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
The sign-up page's own lists of account types and Google domains are not data yet.
