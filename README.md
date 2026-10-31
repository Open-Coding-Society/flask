# Introduction

Founded and maintained by John Mortensen, Open Coding Society's Flask project grew from a classroom need: helping students move beyond writing individual programs to building web applications that store data, communicate through APIs, and support real users.

Developed iteratively since 2020, Flask has supported AP Computer Science Principles and Data Structures coursework. What began as a learning environment has evolved into a reusable backend for student projects, with authentication, database persistence, REST APIs, administrative interfaces, and deployment workflows.

The project reflects OCS's approach to computer science education: students learn by building systems, examining how the pieces fit together, and extending working examples into their own applications. Contributions from students and educators have helped the project evolve, including former student Aiden Wu's work on authentication.

This repository serves two purposes: it is a starting point for new projects and a working codebase through which students can explore how web applications are assembled, tested, secured, and deployed.

Use it as a template for your own application, or fork it if you intend to contribute improvements back to Open Coding Society.

## History

- Used as starter code for student projects for `AP CSP 1 and 2` and `Data Structures 1` curriculum.
- Used to teach key principles in learning the Python Flask programming environment.
- Used as a backend server to service API's in a frontend-to-backend pipeline. Review the `api` folder in the project for endpoints.
- Contains a minimal frontend, mostly to support Administrative functionality using the `templates` folder and `Jinja2` to define UIs.
- Contains SQL database code in the `model` folder to introduce concepts of persistent data and storage.  Perisistence folder is `instance/volumes` for generated SQLite3 db.
- Contains capabilities for deployment and has been used with AWS, Ubuntu, Docker, docker-compose, and Nginx to `deploy a WSGI server`.
- Contains APIs to support `user authentication and cookies`, a great deal of which was contributed by Aiden Wu a former student in CSP.  

## Flask Portfolio Starter

Use this project to create a Flask Server.

- GitHub link: [flask](https://github.com/open-coding-society/flask), runtime link is published under the About on this same page.
- `Use this as template` option is availble if you plan on making your instance of the repository.
- `Fork` the repository if you plan to contribute though GitHub PRs.

## The conventional way to get started

> Quick steps that can be used with MacOS, WSL Ubuntu, or Ubuntu; this uses Python 3.9 or later as a prerequisite.

- Open a Terminal, clone a project and `cd` into the project directory.  Use a `different link` and name for `name` for clone to match your repo.

```bash
mkdir -p ~/openccs; cd ~/opencs

git clone https://github.com/open-coding-ocietyflask.git

cd flask
```

- Install python dependencies for Flask, etc.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Open project in VSCode

- Prepare VSCode and run
  - From Terminal run VSCode

  ```bash
  code .
  ```

  - Open Setting: Ctrl-Shift P or Cmd-Shift
    - Search Python: Select Interpreter.
    - Match interpreter to `which python` from terminal.
    - Shourd be ./venv/bin/python

  - From Extensions Marketplace install `SQLite3 Editor`
    - Open and view SQL database file `instance/volumes/user_management.db`

  - Make a local `.env` file in root of project to contain your secret passwords. Every password must follow the same rule as Spring (8+ characters with an uppercase letter, a lowercase letter, a number, and one of `` ` ~ ! @ # $ % ^ & * ( ) ``), or the model rejects it when it seeds that account. Run `python scripts/verify_password_rule.py` to check.

  ```shell
  # Port configuration
  # FLASK_PORT=8001
  # Admin user reset password 
  DEFAULT_PASSWORD='123Qwerty!'
  DEFAULT_PFP='default.png'
  # Admin user defaults
  ADMIN_USER='Thomas Edison'
  ADMIN_UID='toby'
  ADMIN_PASSWORD='123Toby!'
  ADMIN_PFP='toby.png'
  # Teacher user defaults
  TEACHER_USER='Nikola Tesla'
  TEACHER_UID='niko'
  TEACHER_PASSWORD='123Niko!'
  TEACHER_PFP='niko.png'
  # Default user for testing 
  USER_NAME='Grace Hopper'
  USER_UID='hop'
  USER_PASSWORD='123Grace!'
  USER_PFP='hop.png'
  # Convience user defaults
  MY_NAME='John Mortensen'
  MY_UID='jm1021'
  MY_ROLE='admin'
  # Obtain key, [Google AI Studio](https://aistudio.google.com/api-keys)
  GEMINI_API_KEY=xxxxx
  GEMINI_SERVER=https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent
  # Obtain key, [Groq Console](https://console.groq.com/keys)
  GROQ_API_KEY=xxxxx
  GROQ_SERVER=https://api.groq.com/openai/v1/chat/completions
  # GitHub Configuation
  GITHUB_TOKEN=ghp_xxx
  GITHUB_TARGET_TYPE=user  # Use 'organization' or 'user'
  GITHUB_TARGET_NAME=Open-Coding-Society
  RFID_API_KEY=xxxxx
  # Optional: true once the camera posts face-check results (default false = "RFID only")
  CAMERA_ENABLED=false
  # DB Configuration, AWS RDS
  IS_PRODUCTION=false # false = LOCAL true = DEPLOYED
  DB_USERNAME='admin'
  DB_PASSWORD='xxxxx'
  ```

  - Make the database and init data.
  
  ```bash
  ./scripts/db_init.py
  ```

  - Explore newly created SQL database
    - Be sure to install SQLite3 Editor from VSCode Marketplace
    - Navigate too instance/volumes
    - View/open `user_management.db`
    - Loook at `users` table in viewer

  - Run the Project
    - Select/open `main.py` in VSCode
    - Start with Play button
      - Play button sub option contains Debug
    - Click on localhost:8087 in terminal to launch
      - Output window will contain page to launch http://localhost:8587
    - Login using your secrets from env

  - Basic API test
    - [Jokes](http://localhost:8587/api/jokes/)

### HTTP requests in VS Code

Install the **REST Client** extension (Huachao Mao) and open
[api/user_auth.http](api/user_auth.http). Click **Send Request** to authenticate;
the request prompts for your user ID and password.

The shared `baseUrl` configuration lives in
[.vscode/settings.json](.vscode/settings.json), under
`rest-client.environmentVariables`. Run **REST Client: Switch Environment**
from the command palette (or click the REST Client environment in the status
bar) to select:

- **local**: `http://127.0.0.1:8587`, inherited from `$shared`.
- **deployed**: `https://flask.opencodingsociety.com`.

With **No Environment** selected, the shared localhost URL is used as a safe
default. All request files should use `{{baseUrl}}` rather than defining a
file-level `@baseUrl`, which would override the selected environment.
Change the deployed URL for your own deployment.

REST Client does not read Python's `app.config` directly. If you change
`FLASK_PORT` in `.env`, also update the localhost URL in the shared settings.
Keep credentials out of these version-controlled settings; use the request
prompts instead.

#### Presence system sequence

Open [api/presence.http](api/presence.http). It walks through the presence
system top to bottom: authenticate as an Admin or Teacher, create a room and a
schedule, register a tag, start a manual period, send taps as a reader would
(including a duplicate and a bad key), and read the room's status. Reader
requests prompt for the `RFID_API_KEY`; keep it out of the file. A fresh
database needs `db.create_all()` (or `./scripts/db_init.py` locally), and
`seed_schedules()` loads the starter schedule from `model/presence_schedules.json`.

People can register their own card (steps 22-26): a logged-in user opens a
60-second claim for a room's reader (`POST /api/rfid/claims`), then taps a new
card there, and the scan links it to them. If two people are registering at the
same reader at once, nobody gets the card and both try again, so a card is never
linked to the wrong person. Self-service is one card per person; replacing or
removing a card stays with Teachers and Admins (`/api/rfid/tags`).

Students enroll themselves in a room and one of its periods (steps 27-45): a
logged-in user lists rooms with their periods (`GET /api/presence/enroll/rooms`),
then picks one (`PUT /api/presence/enroll/me` with `{room_id, period_name}`).
A room's periods are the block names of its schedule, so "Period 1" is one
period even though it runs at different times on Wednesdays. During a scheduled
period the room's status lists only the students enrolled in it, plus members
Teachers and Admins added for every period; a manually started period (a demo)
lists every member. Enrolling in the same room again switches the period, and
`DELETE /api/presence/enroll/me/<room_id>` leaves it. A room without a schedule
takes `"period_name": null`, meaning every period.

#### Manual user CRUD sequence

Open [api/user_crud.http](api/user_crud.http) and set `testUid` to a disposable,
valid GitHub username that does not already exist in this app. Select **local**
for development; the same requests also work with **deployed**, but mutate
real data in that environment.

1. Run [api/user_auth.http](api/user_auth.http) separately with an active
   **Admin** account. REST Client remembers response cookies by default and
   reuses them for matching hosts across request files. Authenticate again
   after switching environments.
2. Click **Send Request** for each numbered CRUD request, top to bottom.
   Check the expected response in each heading before proceeding; stop if a
   step fails, especially Create, to avoid modifying an existing account.
3. After Delete, rerun Read all users and confirm `testUid` is absent.

`GET /api/id` returns the logged-in Admin, not the newly created user.
Updating another user and deleting users require Admin access.

New accounts start inactive. The example creates a Mentor, which stays inactive
(403 "Account pending verification" on protected endpoints) until a Teacher or
Admin runs step 1b. A Teacher or Admin can activate any account, a student included;
a student is also activated on their own when Google verifies their school email at
sign-up. Activating a Mentor covers the full 365 days; for other accounts a body of
`{"days": N}` sets how long (30 by default). The example
retains `dob`, but the create endpoint currently ignores it.

These are manual checks, not automated assertions. REST Client does not run
all requests as an automated test sequence; a runner such as pytest or
HTTPYac is needed for that.

### User Operations
| Purpose | Correct Endpoint | What It Does |
|---------|-----------------|--------------|
| **Login** | `/api/authenticate` | Authenticates user & sets cookie |
| **Get User** | `/api/id` | Gets current logged-in user |
| **Signup** | `/api/user` | Creates new user account |
| **Posts** | `/api/post/all` | Gets all social media posts |
| **Create Post** | `/api/post` | Creates a new post |
| **Gemini AI** | `/api/gemini` | Chat with AI assistant |

### MicroBlog Operations
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/microblog` | Create new post |
| GET | `/api/microblog` | Get posts (with filters) |
| PUT | `/api/microblog` | Update post |
| DELETE | `/api/microblog` | Delete post |

**Query Parameters for GET:**
- `?topicId=1` - Posts for specific topic
- `?userId=123` - Posts by specific user  
- `?search=flask` - Search content
- `?limit=20` - Limit results

### MicroBlog Interactions
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/microblog/reply` | Add reply to post |
| POST | `/api/microblog/reaction` | Add reaction (👍, ❤️, etc.) |
| DELETE | `/api/microblog/reaction` | Remove reaction |

### Microblog Page Integration
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/microblog/page/<page_key>` | Get posts for specific page |
| POST | `/api/microblog/topics/auto-create` | Auto-create topic for page |
| GET | `/api/microblog/topics?pagePath=X` | Get topic by page path |

## Idea

### Files and Directories in this Project

The key files and directories in this project are in these online articles.

[Python/Flask](https://pages.opencodingsociety.com/python/flask)

[Legacy - Flask Intro](https://pages.opencodingsociety.com/flask-overview)

### Implementation Summary

#### Oct 2025

> Updates for 2025-2026 school year.  Focus on documentation and API functionality.

- Work to make documentation materials useful.
- Add gemini API's
- Add microblog API's, social medai support

#### July 2024

> Updates for 2024 too 2025 school year.  Primary addition is a fully functional backend for JWT login system.

- Full support for JWT cookies
- The API's for CRUD methods
- The model definition User Class and related tables
- SQLite and RDS support
- Minimal Server side UI in Jinja2

#### July 2023

> Updates for 2023 to 2024 school year.

- Update README with File Descriptions (anatomy)
- Add JWT and add security features using a SQLite user database
- Add migrate.sh to support sqlite schema and data upgrade

#### January 2023

> This project focuses on being a Python backend server.  Intentions are to only have simple UIs an perhaps some Administrative UIs.

#### September 2021

> Basic UI elements were implemented showing server side Flask with Jinja 2 capabilities.

- The Project entry point is main.py, this enables the Flask Web App and provides the capability to render templates (HTML files)
- The main.py is the  Web Server Gateway Interface, essentially it contains an HTTP route and HTML file relationship.  The Python code constructs WSGI relationships for index, kangaroos, walruses, and hawkers.
- The project structure contains many directories and files.  The template directory (containing HTML files) and static directory (containing JS files) are common standards for HTML coding.  Static files can be pictures and videos, in this project they are mostly javascript backgrounds.
- WSGI templates: index.html, kangaroos.html, ... are aligned with routes in main.py.
- Other templates support WSGI templates.  The base.html template contains common Head, Style, Body, and Script definitions.  WSGI templates often "include" or "extend" these templates.  This is a way to reuse code.
- The VANTA javascript statics (backgrounds) are shown and defaulted in base.html (birds) but are block-replaced as needed in other templates (solar, net, ...)
- The Bootstrap Navbar code is in navbar.html. The base.html code includes navbar.html.  The WSGI html files extend base.html files.  This is a process of management and correlation to optimize code management.  For instance, if the menu changes discovery of navbar.html is easy, one change reflects on all WSGI html files.
- Jinja2 variables usage is to isolate data and allow redefinitions of attributes in templates.  Observe "{% set variable = %}" syntax for definition and "{{ variable }}" for reference.
- The base.html uses a combination of Bootstrap grid styling and custom CSS styling.  Grid styling in observation with the "<Col-3>" markers.  A Bootstrap Grid has a width of 12, thus four "Col-3" markers could fit on a Grid row.
- A key purpose of this project is to embed links to other content.  The "href=" definition embeds hyperlinks into the rendered HTML.  The base.html file shows usage of "href={{github}}", the "{{github}}" is a Jinja2 variable.  Jinja2 variables are pre-processed by Python, a variable swap with value, before being sent to the browser.

## Database Management Workflow with Scripts

Production runs on **MySQL (AWS RDS)**; local development runs on SQLite. Data moves
between them over the admin-only `/api/export/*` endpoints, so **both sides must be
running the same code** before you migrate.

Critically: step 5 runs `db_init.py` on production, which does a `drop_all()`. Anything
not covered by the export/import endpoints is destroyed there and never comes back. The
scripts now reconcile every table against `/api/export/counts` and exit non-zero on any
shortfall -- do not ignore that.

**The code deploy comes first, before the pull.** The pull script calls
`/api/export/counts` and the leaderboard / skill-snapshot export endpoints, and none of
them exist on production until you deploy. Deploying the code alone is non-destructive --
the app only creates or drops tables when `db_init.py` is run explicitly -- so this is
safe, and it is the only order in which the pull can verify itself.

0. Be sure `ADMIN_UID` / `ADMIN_PASSWORD` in `.env` are the **production** admin
   credentials. You will need a venv for the python scripts.

1. Deploy the code to production (cockpit, `open/flask`), but **not** the schema:
   ```bash
   git pull
   docker compose up -d --build
   ```
   Do not run `db_init.py` yet. Production keeps its current data and gains the new
   endpoints.

2. Confirm the endpoints are live: authenticate as the production admin and hit
   `/api/export/counts`. It should return a count for every table below. Keep that
   output -- it is your before-picture.

3. Pull production data to local. This fetches every table, then compares local row
   counts against production table by table:
   ```bash
   python scripts/db_migrate-prod2sqlite.py
   ```
   A non-zero exit means the pull was incomplete. Fix it before going further -- pushing
   an incomplete pull back to production deletes the difference.

4. TEST TEST TEST! Make sure your changes work correctly with the local DB. This is the
   last point at which production is still intact.

5. Update the schema on production:
   ```bash
   python scripts/db_init.py
   ```
   This takes a `mysqldump` to `instance/backups/` first and aborts if that fails, then
   runs `drop_all()`, `create_all()` and the seed data. Note the dump path it prints.

6. Push local data back to production:
   ```bash
   python scripts/db_restore-sqlite2prod.py
   ```
   This re-reads production's counts afterwards and reports any table that did not fully
   transfer. Keep your local copy until it reports a clean reconciliation -- until then
   your local database is the only complete copy of that data.

### Which path? Run `db_upgrade.py --check` first

After every deploy, `scripts/db_upgrade.py --check` compares the models with the live
database and says which path the change needs, so nobody has to work it out:

- **Up to date** (exit 0): nothing to do.
- **In place** (exit 2): run `db_upgrade.py` without `--check`. It creates missing tables
  and adds missing columns, and touches nothing else. Most changes are this one.
- **Rebuild needed** (exit 3): it names the column and why, changes nothing, and prints
  the full workflow above (pull, `db_init.py`, restore).

Both paths share their steps in `scripts/db_schema.py`: the same comparison, the same
rollback point, and the same check afterwards that the database matches the models. Only
the change itself differs (`ALTER TABLE` vs. drop and reload). `db_init.py` also prints a
note when the in-place upgrade would have been enough.

```bash
python scripts/db_upgrade.py --check                 # which path; prints the SQL
python scripts/db_upgrade.py                         # backup, ALTER TABLE, create_all(), re-count every table
python scripts/db_upgrade.py --fill users.active=1   # existing rows get 1, new rows the model default
```

A column is added with `ALTER TABLE ... ADD COLUMN` when existing rows can get a value:
it allows NULL, or it has a fixed default (`default=0`, `default="User"`). Existing rows
get NULL or that default. When they need something else, such as keeping current accounts
active while new ones start inactive, pass `--fill table.column=value`. `--fill` also lets
a NOT NULL column with no fixed default (e.g. `default=datetime.utcnow`) be added.

A missing column that is a primary key, unique, a foreign key, or NOT NULL with no fixed
default and no `--fill` needs the rebuild. Tables and columns the models no longer use are listed and left in place.

On production the web container has no `mysqldump`, so take the dump on the host first and
pass it in with `--backup-file`; the exact commands are in the migration runbook. A new
column on an existing table breaks every query of that table until it exists, so run the
upgrade right after `docker compose up -d --build`, with the dump already taken.
`python scripts/verify_db_upgrade.py` checks all of this on a throwaway database, for both
paths, so the rarely used rebuild is tested every time the common one is.

### Tables covered by the migration

`sections`, `users`, `topics`, `microblogs`, `posts`, `classrooms`, `feedback`, `study`,
`personas`, `user_personas`, `leaderboard`, `elementary_leaderboard`, `skill_snapshots`,
plus the `user_sections` and `classroom_students` association rows.

The presence system's tables travel together as one `presence` bundle
(`/api/export/presence`, `/api/export/import/presence`, built in
`model/presence_backup.py`): `presence_schedules`, `presence_schedule_blocks`,
`presence_schedule_overrides`, `presence_rooms` (with `presence_room_members`),
`rfid_tags`, `presence_periods`, `presence_events`, and `camera_checks`. Rows are matched
by uid, room and schedule name, and event id, so a rebuild with new ids keeps every
tag, period, and tap. Each room exports `members` (uid and `period_name`) next to
`member_uids`; an older export with only `member_uids` imports them for every period. `rfid_card_claims` is deliberately not migrated: a claim lives
for 60 seconds (see below), so there is nothing to keep across a rebuild.

**Adding a new model?** It must be added in three places or its data is lost on the next
migration: `MIGRATED_MODELS` plus export/import endpoints in
`api/data_export_import_api.py`, `EXPORT_ENDPOINTS` + a loader in
`scripts/db_migrate-prod2sqlite.py`, and `IMPORT_ENDPOINTS` + a reader in
`scripts/db_restore-sqlite2prod.py`. `/api/export/counts` is what catches the omission.

### The full runbook

The step-by-step production procedure -- with the gates, the rollback paths, and the audit
of what was wrong with the old scripts -- lives at
<https://pages.opencodingsociety.com/documentation/migration-runbook>. Read it before your
first migration; this section is the summary.

### Condensed DB/Schema update simple steps
*(a copy of what's above, just condensed)*

1. On production: `git pull`, then `docker compose up -d --build`. Do **not** run
   `db_init.py` yet.

2. Confirm `/api/export/counts` responds as the production admin.

3. Pull production data to local: `python scripts/db_migrate-prod2sqlite.py` (must exit 0)

4. Test your changes locally

5. On production, update the schema: `python scripts/db_init.py`

6. Push local changes to production: `python scripts/db_restore-sqlite2prod.py` (must exit 0)
