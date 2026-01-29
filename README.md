# README

> This is a project to support AP Computer Science Principles (APCSP) as well as a UC articulated Data Structures course. It was crafted iteratively starting in 2020 to the present time.  The primary purposes are ...

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

  - Make a local `.env` file in root of project to contain your secret passwords

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
  USER_PASSWORD='123Hop!'
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
  # KASM Configuration (server is defaulted)
  KASM_SERVER=https://kasm.opencodingsociety.com
  KASM_API_KEY_SECRET=xxxx
  KASM_API_KEY=xxx
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

If you are working with the database, follow the below procedure to safely interact with the remote DB while applying changes locally. Certain scripts require flask to be running while others don't, so follow the instructions that the scripts provide.

Note, steps 1,2,3,5 are on your development (LOCAL) server. You need to update your .env on development server and be sure all PRs are completed, pulled, and tested before you start pushing to production.

0. Be sure ADMIN_PASSWORD is set in .env.  You will need a venv for the python scripts.

1. Initialize your local DB with clean data. For example, this would be good to see that a schema update works correctly.
   ```bash
   python scripts/db_init.py
   ```

2. Pull the database content from the remote DB onto your local machine. This allows you to work with real data and test that real data works with your local changes.
   ```bash
   python scripts/db_migrate-prod2sqlite.py
   ```

3. TEST TEST TEST! Make sure your changes work correctly with the local DB.

4. Now go onto the remote DB and back up the db using `cp sqlite.db backups/sqlite_year-month-day.db` in the volumes directory of the flask directory on cockpit. Then, run `git pull` to ensure that flask has been updated with the latest code. Then, run `python scripts/db_init.py` again to ensure that the remote DB schema is up to date with the latest code.

5. Once you are satisfied with your changes, push the local DB content to the remote DB. This requires authentication, so you need to replace the ADMIN_PASSWORD in the .env file of "flask" with the production admin password.
   ```bash
   python scripts/db_restore-sqlite2prod.py
   ```

### Condensed DB/Schema update simple steps
*(a copy of what's above, just condensed)*

1. Initialize local DB: `python scripts/db_init.py`

2. Pull production data to local: `python scripts/db_migrate-prod2sqlite.py`

3. Test your changes locally

4. On production server (in cockpit):
   - Backup DB in volumes directory: `cp sqlite.db backups/sqlite_year-month-day.db`
   - Update code: `git pull`
   - Update schema: `python scripts/db_init.py`

5. Push local changes to production: `python scripts/db_restore-sqlite2prod.py` (Requires admin password from production in .env)
