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
import sys
import os

# Add the directory containing main.py to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
# Import application object
from main import app, db, generate_data
# The compare, backup and checks shared with db_upgrade.py
import db_schema as schema

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
                # Same decision db_upgrade.py makes, so a rebuild isn't run when it isn't needed.
                to_create, missing = schema.compare()[:2]
                path = schema.decide(to_create, schema.plan_columns(missing, {}))
                if path == schema.CURRENT:
                    print("Note: the database already matches the models; no rebuild is needed.")
                elif path == schema.IN_PLACE:
                    print("Note: scripts/db_upgrade.py can apply this schema change without dropping data.")
                print("Warning, you are about to lose all data in the database!")
                if os.getenv('FORCE_YES') == 'true':
                    response = 'y'
                else:
                    print("Do you want to continue? (y/n)")
                    response = input()
                if response.lower() != 'y':
                    print("Exiting without making changes.")
                    sys.exit(0)
                    
            # Backup the old database (BACKUP_FILE: a dump taken on the host, as for db_upgrade.py)
            backed_up = schema.take_backup("pre_init", os.getenv('BACKUP_FILE')) if tables else True
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

            # The same check db_upgrade.py runs after an in-place change.
            problems = schema.verify_schema()
            if problems:
                print("FAILED: the rebuilt database does not match the models:"
                      + "".join(f"\n  {p}" for p in problems))
                sys.exit(1)
            
    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)
    
    # Log success 
    print("Database initialized!")
 
if __name__ == "__main__":
    main()