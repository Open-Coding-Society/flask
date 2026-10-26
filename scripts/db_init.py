#!/usr/bin/env python3

""" db_migrate.py
Generates the database schema for all db models
- Initializes Users, Sections, and UserSections tables.
- Imports data from the old database to the new database.

Usage: Run from the terminal as such:

Goto the scripts directory:
> cd scripts; ./db_migrate.py

Or run from the root of the project:
> scripts/db_migrate.py

General Process outline:
0. Warning to the user.
1. Old data extraction.  An API has been created in the old project ...
  - Extract Data: retrieves data from the specified tables in the old database.
  - Transform Data: the API to JSON format understood by the new project.
2. New schema.  The schema is created in "this" new database.
3. Load Data: The bulk load API in "this" project inserts the data using required business logic.

"""
import shutil
import subprocess
import sys
import os
from datetime import datetime

# Add the directory containing main.py to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# Import application object
from main import app, db, generate_data

BACKUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'instance', 'backups'))


def backup_mysql_database():
    """mysqldump the production database before it is dropped.

    Returns the dump path, or None if the dump could not be taken. This step
    used to print "Backup not supported for production database" and continue
    straight into drop_all(), leaving no rollback point at all.
    """
    if shutil.which('mysqldump') is None:
        print("ERROR: mysqldump not found on PATH; cannot back up MySQL.")
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    db_name = app.config['SQLALCHEMY_DATABASE_NAME']
    dump_path = os.path.join(BACKUP_DIR, f"{db_name}_{timestamp}.sql")

    cmd = [
        'mysqldump',
        f"--host={app.config['DB_ENDPOINT']}",
        '--port=3306',
        f"--user={app.config['DB_USERNAME']}",
        '--single-transaction', '--routines', '--triggers',
        # RDS's admin user has no RELOAD privilege; without this, mysqldump 8 on a GTID
        # server runs FLUSH TABLES WITH READ LOCK, is refused, and no dump is written.
        '--set-gtid-purged=OFF',
        db_name,
    ]
    env = dict(os.environ, MYSQL_PWD=app.config['DB_PASSWORD'])

    print(f"Backing up MySQL database to {dump_path} ...")
    with open(dump_path, 'w') as out:
        result = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, env=env)

    if result.returncode != 0:
        print(f"ERROR: mysqldump failed: {result.stderr.decode(errors='replace').strip()[:400]}")
        if os.path.exists(dump_path):
            os.remove(dump_path)
        return None

    size = os.path.getsize(dump_path)
    print(f"MySQL database backed up to {dump_path} ({size} bytes)")
    print(f"Roll back with: mysql -h {app.config['DB_ENDPOINT']} "
          f"-u {app.config['DB_USERNAME']} -p {db_name} < {dump_path}")
    return dump_path


# Backup the old database
def backup_database(db_uri, backup_uri):
    """Backup the current database. Returns True when a rollback point exists."""
    if backup_uri:
        db_path = db_uri.replace('sqlite:///', 'instance/')
        backup_path = backup_uri.replace('sqlite:///', 'instance/')
        shutil.copyfile(db_path, backup_path)
        print(f"Database backed up to {backup_path}")
        return True

    # No backup_uri means production MySQL.
    return backup_mysql_database() is not None

# Main extraction and loading process
def main():
    
    # Step 0: Warning to the user and backup table
    with app.app_context():
        try:
            # Step 3: Build New schema
            # Check if the database has any tables
            inspector = db.inspect(db.engine)
            tables = inspector.get_table_names()
            
            if tables:
                print("Warning, you are about to lose all data in the database!")
                if os.getenv('FORCE_YES') == 'true':
                    response = 'y'
                else:
                    print("Do you want to continue? (y/n)")
                    response = input()
                if response.lower() != 'y':
                    print("Exiting without making changes.")
                    sys.exit(0)
                    
            # Backup the old database
            backed_up = backup_database(
                app.config['SQLALCHEMY_DATABASE_URI'],
                app.config['SQLALCHEMY_BACKUP_URI'],
            )
            if not backed_up and os.getenv('ALLOW_NO_BACKUP') != 'true':
                print("\nRefusing to drop the database without a rollback point.")
                print("Fix the backup above, or set ALLOW_NO_BACKUP=true to override.")
                sys.exit(1)

        except Exception as e:
            print(f"An error occurred: {e}")
            sys.exit(1)
        
    # Step 1: Build New schema and create test data 
    try:
        with app.app_context():
            # Disable foreign key checks for MySQL (no effect on SQLite)
            if db.engine.url.drivername in ['mysql', 'mysql+pymysql']:
                db.session.execute(db.text('SET FOREIGN_KEY_CHECKS=0;'))
                db.session.commit()
            
            # Drop all the tables defined in the project
            db.drop_all()
            print("All tables dropped.")
            
            # Re-enable foreign key checks for MySQL
            if db.engine.url.drivername in ['mysql', 'mysql+pymysql']:
                db.session.execute(db.text('SET FOREIGN_KEY_CHECKS=1;'))
                db.session.commit()
            
            # Create all tables
            db.create_all()
            print("All tables created.")
            
            # Add default test data 
            generate_data() # test data
            
    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)
    
    # Log success 
    print("Database initialized!")
 
if __name__ == "__main__":
    main()