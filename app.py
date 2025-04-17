import os
import logging
import uuid
from datetime import datetime, timedelta, date
import psycopg2 # PostgreSQL adapter
from psycopg2.extras import DictCursor # To get rows as dictionaries
from flask import Flask, request, jsonify, render_template, Response
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv # For local development

# --- Configuration ---
load_dotenv()

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv('DATABASE_URL')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')
INIT_DB_SECRET = os.getenv('INIT_DB_SECRET') # For securing init endpoint

# Initialize Twilio client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio client initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize Twilio client: {e}")
else:
    logger.warning("Twilio credentials not found.")

# --- Database Helper Functions (PostgreSQL) ---
# (Keep the get_db_connection and execute_query functions exactly as they were)
def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    if not DATABASE_URL:
        logger.error("DATABASE_URL environment variable is not set.")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection error: {e}")
        return None

def execute_query(query, params=None, fetch_one=False, fetch_all=False, commit=False):
    """Executes a query and optionally fetches results or commits."""
    conn = get_db_connection()
    if conn is None:
        if fetch_one: return None
        if fetch_all: return []
        if commit: raise ConnectionError("Database connection failed")
        return None
    result_data = None
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(query, params)
            if fetch_one:
                result_data = cur.fetchone()
            elif fetch_all:
                result_data = cur.fetchall()
            if commit:
                conn.commit()
    except Exception as e:
        logger.error(f"Database query failed: {query} | PARAMS: {params} | ERROR: {e}")
        conn.rollback()
        raise # Re-raise the exception
    finally:
        if conn:
            conn.close()
    if result_data is None:
         return None if fetch_one else []
    if fetch_one:
        return dict(result_data)
    if fetch_all:
        return [dict(row) for row in result_data]
    return True

# --- Database Initialization ---
# (Keep the init_db function exactly as it was in the previous version - render_app_py_v4)
# NOTE: Ensure you have run this or equivalent ALTER commands previously
def init_db():
    """Creates/updates the database tables."""
    commands = [
        """CREATE EXTENSION IF NOT EXISTS "uuid-ossp";""",
        """CREATE TABLE IF NOT EXISTS members (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            frequency TEXT CHECK (frequency IN ('weekly', 'daily', 'monthly', 'manual')),
            recur_day_of_week INTEGER CHECK (recur_day_of_week BETWEEN 0 AND 6), -- 0=Monday, 6=Sunday
            date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT freq_day_check CHECK (frequency != 'weekly' OR recur_day_of_week IS NOT NULL) -- Weekly tasks must have a day
        );""",
        """ALTER TABLE tasks ADD COLUMN IF NOT EXISTS recur_day_of_week INTEGER CHECK (recur_day_of_week BETWEEN 0 AND 6);""",
        """DO $$ BEGIN
               IF NOT EXISTS (
                   SELECT 1 FROM information_schema.table_constraints
                   WHERE constraint_name = 'freq_day_check' AND table_name = 'tasks'
               ) THEN
                   ALTER TABLE tasks ADD CONSTRAINT freq_day_check
                   CHECK (frequency != 'weekly' OR recur_day_of_week IS NOT NULL);
               END IF;
           END $$;""",
        """CREATE TABLE IF NOT EXISTS assignments (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            assigned_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            due_date DATE NOT NULL, -- Task is due ON this specific date
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            completion_date TIMESTAMP WITH TIME ZONE,
            UNIQUE(member_id, task_id, due_date)
        );""",
        """INSERT INTO tasks (name, description, frequency, recur_day_of_week)
           SELECT 'Kitchen cleaning', 'Clean kitchen surfaces and floor', 'weekly', 1 -- Example: Tuesday (0=Mon)
           WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE name = 'Kitchen cleaning')
           ON CONFLICT (name) DO UPDATE SET
               description = EXCLUDED.description,
               frequency = EXCLUDED.frequency,
               recur_day_of_week = EXCLUDED.recur_day_of_week;""",
        """INSERT INTO tasks (name, description, frequency, recur_day_of_week)
           SELECT 'Bathroom cleaning', 'Clean bathroom, shower, toilet, sink', 'weekly', 3 -- Example: Thursday
           WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE name = 'Bathroom cleaning')
           ON CONFLICT (name) DO UPDATE SET
               description = EXCLUDED.description,
               frequency = EXCLUDED.frequency,
               recur_day_of_week = EXCLUDED.recur_day_of_week;"""
    ]
    conn = get_db_connection()
    if conn is None: logger.error("Cannot initialize DB: No connection."); return
    try:
        with conn.cursor() as cur:
            for command in commands:
                logger.debug(f"Executing DB command: {command[:100]}...")
                cur.execute(command)
        conn.commit()
        logger.info("Database tables checked/created/updated successfully.")
    except Exception as e:
        logger.error(f"Error initializing/updating database: {e}")
        conn.rollback()
    finally:
        if conn:
            conn.close()

# --- Flask Routes ---

@app.route('/')
def home_page():
    return render_template('index.html')

@app.route('/health', methods=['GET'])
def health_check():
    # (Keep the health_check function exactly as it was)
    conn = get_db_connection()
    db_ok = False
    if conn:
        try:
            with conn.cursor() as cur: cur.execute("SELECT 1;")
            db_ok = True
        except Exception as e: logger.error(f"Health check DB query failed: {e}")
        finally: conn.close()
    db_status = "connected" if db_ok else "disconnected_or_error"
    return jsonify({
        "status": "healthy", "database": db_status,
        "twilio_client": "initialized" if twilio_client else "not_initialized"
    }), 200

# --- API Endpoints ---
# Note: For a larger application, consider using Flask Blueprints
# to organize these routes into separate files.

# == MEMBERS ==
@app.route('/api/members', methods=['GET'])
def get_members():
    try:
        members = execute_query("SELECT id, name, phone, date_added FROM members ORDER BY date_added", fetch_all=True)
        return jsonify(members)
    except Exception as e:
        logger.error(f"Error getting members: {e}")
        return jsonify({"error": "Server error retrieving members"}), 500

@app.route('/api/members/<uuid:member_id>', methods=['GET'])
def get_member(member_id):
    try:
        member = execute_query("SELECT id, name, phone, date_added FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
        if member:
            return jsonify(member)
        else:
            return jsonify({"error": "Member not found"}), 404
    except Exception as e:
        logger.error(f"Error getting member {member_id}: {e}")
        return jsonify({"error": "Server error retrieving member"}), 500

@app.route('/api/members', methods=['POST'])
def add_member():
    # (Keep the add_member function exactly as it was)
    data = request.json
    if not data or 'name' not in data or 'phone' not in data: return jsonify({"error": "Name and phone required"}), 400
    phone = data['phone']
    if not phone.startswith('+'): return jsonify({"error": "Phone number must start with +"}), 400
    try:
        existing_member = execute_query("SELECT id FROM members WHERE phone = %s", (phone,), fetch_one=True)
        if existing_member: return jsonify({"error": "Phone number already exists", "member_id": existing_member['id']}), 409
        query = "INSERT INTO members (name, phone) VALUES (%s, %s) RETURNING id, name, phone, date_added;"
        new_member = execute_query(query, (data['name'], phone), fetch_one=True, commit=True)
        if new_member: return jsonify({"status": "success", "member": new_member}), 201
        else: logger.error(f"Failed to insert member. Data: {data}"); return jsonify({"error": "Failed to add member"}), 500
    except Exception as e: logger.error(f"Error adding member: {e}"); return jsonify({"error": "Server error adding member"}), 500

@app.route('/api/members/<uuid:member_id>', methods=['PUT'])
def update_member(member_id):
    data = request.json
    if not data or ('name' not in data and 'phone' not in data):
        return jsonify({"error": "No update data provided (need name and/or phone)"}), 400

    updates = []
    params = []

    if 'name' in data:
        updates.append("name = %s")
        params.append(data['name'])
    if 'phone' in data:
        phone = data['phone']
        if not phone.startswith('+'):
            return jsonify({"error": "Phone number must start with +"}), 400
        # Check if the NEW phone number is already taken by ANOTHER user
        existing_phone = execute_query("SELECT id FROM members WHERE phone = %s AND id != %s", (phone, str(member_id)), fetch_one=True)
        if existing_phone:
            return jsonify({"error": f"Phone number {phone} is already in use by another member."}), 409
        updates.append("phone = %s")
        params.append(phone)

    if not updates: # Should be caught by initial check, but belts and braces
         return jsonify({"error": "No valid fields to update"}), 400

    params.append(str(member_id)) # Add the member_id for the WHERE clause

    query = f"UPDATE members SET {', '.join(updates)} WHERE id = %s RETURNING id, name, phone, date_added;"

    try:
        updated_member = execute_query(query, tuple(params), fetch_one=True, commit=True)
        if updated_member:
            return jsonify({"status": "success", "member": updated_member}), 200
        else:
            # Check if the member ID itself was invalid
            check_exists = execute_query("SELECT id FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
            if not check_exists:
                return jsonify({"error": "Member not found"}), 404
            else:
                logger.error(f"Failed to update member {member_id} or retrieve updated data.")
                return jsonify({"error": "Failed to update member"}), 500
    except Exception as e:
        logger.error(f"Error updating member {member_id}: {e}")
        return jsonify({"error": "Server error updating member"}), 500

@app.route('/api/members/<uuid:member_id>', methods=['DELETE'])
def delete_member(member_id):
    logger.info(f"Attempting to delete member with ID: {member_id}")
    try:
        # Check if member exists first
        member = execute_query("SELECT id, name FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
        if not member:
            return jsonify({"error": "Member not found"}), 404

        # Delete the member. ON DELETE CASCADE in assignments table handles related assignments.
        query = "DELETE FROM members WHERE id = %s;"
        execute_query(query, (str(member_id),), commit=True)

        logger.info(f"Successfully deleted member '{member.get('name', 'N/A')}' with ID: {member_id}")
        return jsonify({"status": "success", "message": f"Member '{member.get('name', 'N/A')}' deleted successfully."}), 200
        # return '', 204 # Alternative response
    except Exception as e:
        logger.error(f"Error deleting member {member_id}: {e}")
        return jsonify({"error": "Server error deleting member"}), 500

# == TASKS ==
@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    # (Keep the get_tasks function exactly as it was)
    try:
        tasks = execute_query("SELECT id, name, description, frequency, recur_day_of_week, date_added FROM tasks ORDER BY name", fetch_all=True)
        for task in tasks: task['recur_day_of_week'] = task.get('recur_day_of_week')
        return jsonify(tasks)
    except Exception as e: logger.error(f"Error getting tasks: {e}"); return jsonify({"error": "Server error retrieving tasks"}), 500

@app.route('/api/tasks/<uuid:task_id>', methods=['GET'])
def get_task(task_id):
    try:
        task = execute_query("SELECT id, name, description, frequency, recur_day_of_week, date_added FROM tasks WHERE id = %s", (str(task_id),), fetch_one=True)
        if task:
            task['recur_day_of_week'] = task.get('recur_day_of_week') # Ensure None if NULL
            return jsonify(task)
        else:
            return jsonify({"error": "Task not found"}), 404
    except Exception as e:
        logger.error(f"Error getting task {task_id}: {e}")
        return jsonify({"error": "Server error retrieving task"}), 500

@app.route('/api/tasks', methods=['POST'])
def add_task():
    # (Keep the add_task function exactly as it was)
    data = request.json
    if not data or 'name' not in data: return jsonify({"error": "Task name required"}), 400
    name = data['name']
    description = data.get('description')
    frequency = data.get('frequency', 'manual')
    recur_day_of_week = data.get('recur_day_of_week')
    valid_frequencies = ['daily', 'weekly', 'monthly', 'manual']
    if frequency not in valid_frequencies: return jsonify({"error": f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"}), 400
    if frequency == 'weekly':
        if recur_day_of_week is None: return jsonify({"error": "recur_day_of_week (0-6, Mon-Sun) is required for weekly frequency"}), 400
        try: day_num = int(recur_day_of_week); assert 0 <= day_num <= 6; recur_day_of_week = day_num
        except (ValueError, TypeError, AssertionError): return jsonify({"error": "Invalid recur_day_of_week. Must be integer 0-6."}), 400
    else: recur_day_of_week = None
    try:
        existing_task = execute_query("SELECT id FROM tasks WHERE name = %s", (name,), fetch_one=True)
        if existing_task: return jsonify({"error": "Task name already exists", "task_id": existing_task['id']}), 409
        query = "INSERT INTO tasks (name, description, frequency, recur_day_of_week) VALUES (%s, %s, %s, %s) RETURNING id, name, description, frequency, recur_day_of_week, date_added;"
        new_task = execute_query(query, (name, description, frequency, recur_day_of_week), fetch_one=True, commit=True)
        if new_task: new_task['recur_day_of_week'] = new_task.get('recur_day_of_week'); return jsonify({"status": "success", "task": new_task}), 201
        else: logger.error(f"Failed to insert task. Data: {data}"); return jsonify({"error": "Failed to add task"}), 500
    except Exception as e: logger.error(f"Error adding task: {e}"); return jsonify({"error": "Server error adding task"}), 500

@app.route('/api/tasks/<uuid:task_id>', methods=['PUT'])
def update_task(task_id):
    data = request.json
    if not data:
        return jsonify({"error": "No update data provided"}), 400

    # Fetch existing task to check existence and potentially current values
    try:
        existing_task = execute_query("SELECT * FROM tasks WHERE id = %s", (str(task_id),), fetch_one=True)
        if not existing_task:
            return jsonify({"error": "Task not found"}), 404
    except Exception as e:
         logger.error(f"Error checking task {task_id} before update: {e}")
         return jsonify({"error": "Server error checking task"}), 500

    updates = []
    params = []

    # Handle name update (check for uniqueness if changed)
    if 'name' in data and data['name'] != existing_task['name']:
        name = data['name']
        check_name = execute_query("SELECT id FROM tasks WHERE name = %s AND id != %s", (name, str(task_id)), fetch_one=True)
        if check_name:
            return jsonify({"error": f"Task name '{name}' already exists."}), 409
        updates.append("name = %s")
        params.append(name)

    if 'description' in data:
        updates.append("description = %s")
        params.append(data['description']) # Allow setting to null/empty

    # Handle frequency and recur_day_of_week together
    frequency = data.get('frequency', existing_task['frequency']) # Use existing if not provided
    recur_day_of_week = data.get('recur_day_of_week', existing_task['recur_day_of_week'])

    valid_frequencies = ['daily', 'weekly', 'monthly', 'manual']
    if frequency not in valid_frequencies:
        return jsonify({"error": f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"}), 400

    if frequency == 'weekly':
        if recur_day_of_week is None:
             # If user explicitly set frequency to weekly but not day, use existing day if possible, else error
             if existing_task['recur_day_of_week'] is not None and 'recur_day_of_week' not in data:
                 recur_day_of_week = existing_task['recur_day_of_week'] # Keep existing day
                 logger.info(f"Updating task {task_id} frequency to weekly, keeping existing day {recur_day_of_week}")
             else:
                 return jsonify({"error": "recur_day_of_week (0-6) is required when setting frequency to weekly"}), 400
        try: # Validate even if keeping existing
            day_num = int(recur_day_of_week); assert 0 <= day_num <= 6; recur_day_of_week = day_num
        except (ValueError, TypeError, AssertionError):
            return jsonify({"error": "Invalid recur_day_of_week. Must be integer 0-6."}), 400
    else:
        recur_day_of_week = None # Force null if not weekly

    # Add frequency and day to update if they changed
    if frequency != existing_task['frequency']:
        updates.append("frequency = %s")
        params.append(frequency)
    # Add day if it changed OR if frequency changed (to ensure null is set correctly)
    if recur_day_of_week != existing_task['recur_day_of_week'] or frequency != existing_task['frequency']:
         updates.append("recur_day_of_week = %s")
         params.append(recur_day_of_week)


    if not updates:
         return jsonify({"message": "No changes detected to update"}), 200 # Or 304 Not Modified? 200 is simpler.

    params.append(str(task_id)) # Add the task_id for the WHERE clause
    query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = %s RETURNING id, name, description, frequency, recur_day_of_week, date_added;"

    try:
        updated_task = execute_query(query, tuple(params), fetch_one=True, commit=True)
        if updated_task:
            updated_task['recur_day_of_week'] = updated_task.get('recur_day_of_week') # Ensure None if NULL
            return jsonify({"status": "success", "task": updated_task}), 200
        else:
            # Should be caught by initial check, but maybe race condition?
            logger.error(f"Failed to update task {task_id} or retrieve updated data.")
            return jsonify({"error": "Failed to update task"}), 500
    except Exception as e:
        logger.error(f"Error updating task {task_id}: {e}")
        return jsonify({"error": "Server error updating task"}), 500

@app.route('/api/tasks/<uuid:task_id>', methods=['DELETE'])
def delete_task(task_id):
    # (Keep the delete_task function exactly as it was)
    logger.info(f"Attempting to delete task with ID: {task_id}")
    try:
        task = execute_query("SELECT id, name FROM tasks WHERE id = %s", (str(task_id),), fetch_one=True)
        if not task: return jsonify({"error": "Task not found"}), 404
        query = "DELETE FROM tasks WHERE id = %s;"
        execute_query(query, (str(task_id),), commit=True)
        logger.info(f"Successfully deleted task '{task.get('name', 'N/A')}' with ID: {task_id}")
        return jsonify({"status": "success", "message": f"Task '{task.get('name', 'N/A')}' deleted successfully."}), 200
    except Exception as e: logger.error(f"Error deleting task {task_id}: {e}"); return jsonify({"error": "Server error deleting task"}), 500

# == ASSIGNMENTS ==
@app.route('/api/assignments', methods=['GET'])
def get_assignments():
    # Add joins to get member and task names
    query = """
        SELECT
            a.id, a.assigned_date, a.due_date, a.completed, a.completion_date,
            m.id as member_id, m.name as member_name,
            t.id as task_id, t.name as task_name
        FROM assignments a
        JOIN members m ON a.member_id = m.id
        JOIN tasks t ON a.task_id = t.id
        ORDER BY a.due_date DESC, m.name, t.name;
    """
    try:
        assignments = execute_query(query, fetch_all=True)
        return jsonify(assignments)
    except Exception as e:
        logger.error(f"Error getting assignments: {e}")
        return jsonify({"error": "Server error retrieving assignments"}), 500

@app.route('/api/assignments/<uuid:assignment_id>', methods=['GET'])
def get_assignment(assignment_id):
    query = """
        SELECT
            a.id, a.assigned_date, a.due_date, a.completed, a.completion_date,
            m.id as member_id, m.name as member_name,
            t.id as task_id, t.name as task_name, t.description as task_description
        FROM assignments a
        JOIN members m ON a.member_id = m.id
        JOIN tasks t ON a.task_id = t.id
        WHERE a.id = %s;
    """
    try:
        assignment = execute_query(query, (str(assignment_id),), fetch_one=True)
        if assignment:
            return jsonify(assignment)
        else:
            return jsonify({"error": "Assignment not found"}), 404
    except Exception as e:
        logger.error(f"Error getting assignment {assignment_id}: {e}")
        return jsonify({"error": "Server error retrieving assignment"}), 500

@app.route('/api/assign', methods=['POST'])
def assign_task():
    # (Keep the assign_task function exactly as it was)
    data = request.json
    required_fields = ['member_id', 'task_id', 'due_date']
    if not data or not all(field in data for field in required_fields): return jsonify({"error": f"Missing required fields: {', '.join(required_fields)}"}), 400
    member_id = data['member_id']; task_id = data['task_id']; due_date_str = data['due_date']
    try: uuid.UUID(member_id); uuid.UUID(task_id); due_date = date.fromisoformat(due_date_str)
    except ValueError: return jsonify({"error": "Invalid member_id, task_id (UUID) or due_date (YYYY-MM-DD) format."}), 400
    try:
        check_query = "SELECT id FROM assignments WHERE member_id = %s AND task_id = %s AND due_date = %s AND completed = FALSE"
        existing = execute_query(check_query, (member_id, task_id, due_date), fetch_one=True)
        if existing: return jsonify({"error": "This task is already assigned to this member for this due date and is not completed."}), 409
        query = "INSERT INTO assignments (member_id, task_id, due_date) VALUES (%s, %s, %s) RETURNING id, member_id, task_id, assigned_date, due_date, completed;"
        new_assignment = execute_query(query, (member_id, task_id, due_date), fetch_one=True, commit=True)
        if new_assignment: return jsonify({"status": "success", "assignment": new_assignment}), 201
        else: logger.error(f"Failed to insert assignment. Data: {data}"); return jsonify({"error": "Failed to assign task"}), 500
    except Exception as e:
        logger.error(f"Error assigning task: {e}")
        if "violates foreign key constraint" in str(e): return jsonify({"error": "Invalid member_id or task_id provided."}), 404
        return jsonify({"error": "Server error assigning task"}), 500

@app.route('/api/assignments/<uuid:assignment_id>', methods=['PUT'])
def update_assignment(assignment_id):
    # Allows updating due_date or completed status
    data = request.json
    if not data or ('due_date' not in data and 'completed' not in data):
        return jsonify({"error": "No update data provided (need due_date and/or completed)"}), 400

    updates = []
    params = []

    if 'due_date' in data:
        try:
            due_date = date.fromisoformat(data['due_date'])
            updates.append("due_date = %s")
            params.append(due_date)
        except ValueError:
            return jsonify({"error": "Invalid due_date format (must be YYYY-MM-DD)"}), 400

    if 'completed' in data:
        completed_status = data['completed']
        if not isinstance(completed_status, bool):
            return jsonify({"error": "Invalid completed status (must be true or false)"}), 400
        updates.append("completed = %s")
        params.append(completed_status)
        # Also update completion_date if marking as complete
        if completed_status:
            updates.append("completion_date = CURRENT_TIMESTAMP")
        else:
            updates.append("completion_date = NULL") # Clear completion date if marking incomplete

    if not updates:
        return jsonify({"message": "No valid fields to update"}), 200

    params.append(str(assignment_id)) # For WHERE clause
    query = f"UPDATE assignments SET {', '.join(updates)} WHERE id = %s RETURNING id;" # Just return ID to confirm update

    try:
        result = execute_query(query, tuple(params), fetch_one=True, commit=True)
        if result:
            # Fetch the full updated assignment to return it
            full_assignment = get_assignment(assignment_id).get_json() # Call the GET endpoint logic
            return jsonify({"status": "success", "assignment": full_assignment}), 200
        else:
            # Check if assignment ID was invalid
            check_exists = execute_query("SELECT id FROM assignments WHERE id = %s", (str(assignment_id),), fetch_one=True)
            if not check_exists:
                return jsonify({"error": "Assignment not found"}), 404
            else:
                 logger.error(f"Failed to update assignment {assignment_id} or retrieve updated data.")
                 return jsonify({"error": "Failed to update assignment"}), 500
    except Exception as e:
        logger.error(f"Error updating assignment {assignment_id}: {e}")
        return jsonify({"error": "Server error updating assignment"}), 500

@app.route('/api/assignments/<uuid:assignment_id>/complete', methods=['POST'])
def complete_assignment_api(assignment_id):
    """Marks a specific assignment as complete via API."""
    logger.info(f"API request to complete assignment {assignment_id}")
    try:
        # Check if assignment exists and is not already complete
        check_query = "SELECT id FROM assignments WHERE id = %s AND completed = FALSE;"
        assignment = execute_query(check_query, (str(assignment_id),), fetch_one=True)
        if not assignment:
            # Check if it exists but is already complete
            already_done = execute_query("SELECT id FROM assignments WHERE id = %s AND completed = TRUE;", (str(assignment_id),), fetch_one=True)
            if already_done:
                 return jsonify({"message": "Assignment already marked as complete"}), 200
            else:
                 return jsonify({"error": "Assignment not found or already complete"}), 404

        # Update the assignment
        update_query = "UPDATE assignments SET completed = TRUE, completion_date = CURRENT_TIMESTAMP WHERE id = %s RETURNING id;"
        result = execute_query(update_query, (str(assignment_id),), fetch_one=True, commit=True)

        if result:
            logger.info(f"Marked assignment {assignment_id} complete via API.")
            # Fetch the full updated assignment to return it
            full_assignment = get_assignment(assignment_id).get_json()
            return jsonify({"status": "success", "assignment": full_assignment}), 200
        else:
             logger.error(f"Failed to mark assignment {assignment_id} complete.")
             return jsonify({"error": "Failed to mark assignment complete"}), 500
    except Exception as e:
        logger.error(f"Error completing assignment {assignment_id} via API: {e}")
        return jsonify({"error": "Server error completing assignment"}), 500

@app.route('/api/assignments/<uuid:assignment_id>', methods=['DELETE'])
def delete_assignment(assignment_id):
    """Deletes a specific assignment instance."""
    logger.info(f"Attempting to delete assignment with ID: {assignment_id}")
    try:
        # Check if assignment exists first
        assignment = execute_query("SELECT id FROM assignments WHERE id = %s", (str(assignment_id),), fetch_one=True)
        if not assignment:
            return jsonify({"error": "Assignment not found"}), 404

        # Delete the assignment
        query = "DELETE FROM assignments WHERE id = %s;"
        execute_query(query, (str(assignment_id),), commit=True)

        logger.info(f"Successfully deleted assignment with ID: {assignment_id}")
        return jsonify({"status": "success", "message": "Assignment deleted successfully."}), 200
        # return '', 204 # Alternative response
    except Exception as e:
        logger.error(f"Error deleting assignment {assignment_id}: {e}")
        return jsonify({"error": "Server error deleting assignment"}), 500


# == NOTIFICATIONS ==
@app.route('/api/notify/all_chores', methods=['POST'])
def notify_all_chores():
    # (Keep the notify_all_chores function exactly as it was)
    if not twilio_client: return jsonify({"error": "Twilio client not configured"}), 500
    try:
        members = execute_query("SELECT id, name, phone FROM members", fetch_all=True)
        if not members: return jsonify({"status": "No members found to notify"}), 200
        logger.info(f"Attempting to send generic chore notification to {len(members)} members.")
        success_count = 0; error_count = 0
        for member in members:
            message_body = f"Hi {member['name']}, friendly reminder to check and complete your assigned chores!"
            try:
                message = twilio_client.messages.create(body=message_body, from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}", to=f"whatsapp:{member['phone']}")
                logger.info(f"Sent notification SID {message.sid} to {member['name']}"); success_count += 1
            except Exception as e: logger.error(f"Failed to send notification to {member['name']} ({member['phone']}): {e}"); error_count += 1
        return jsonify({"status": "Notification process completed.", "successful_notifications": success_count, "failed_notifications": error_count})
    except Exception as e: logger.error(f"Error during notify all chores: {e}"); return jsonify({"error": "Server error sending notifications"}), 500

@app.route('/api/notify/member/<uuid:member_id>', methods=['POST'])
def notify_member_tasks(member_id):
    """Sends a specific member their currently outstanding tasks."""
    if not twilio_client:
        return jsonify({"error": "Twilio client not configured"}), 500

    try:
        member = execute_query("SELECT id, name, phone FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
        if not member:
            return jsonify({"error": "Member not found"}), 404

        today = date.today()
        tasks_query = """
            SELECT t.name, a.due_date
            FROM assignments a JOIN tasks t ON a.task_id = t.id
            WHERE a.member_id = %s AND a.completed = FALSE AND a.due_date >= %s
            ORDER BY a.due_date;
        """
        assignments = execute_query(tasks_query, (member_id, today), fetch_all=True)

        if not assignments:
            message_body = f"Hi {member['name']}! You currently have no outstanding tasks assigned."
        else:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            tasks_list = [f"- {a['name']} (Due: {days[a['due_date'].weekday()]} {a['due_date'].strftime('%Y-%m-%d')})" for a in assignments]
            tasks_str = "\n".join(tasks_list)
            message_body = f"Hi {member['name']}! Here are your outstanding tasks:\n{tasks_str}"

        try:
            message = twilio_client.messages.create(
                body=message_body,
                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                to=f"whatsapp:{member['phone']}"
            )
            logger.info(f"Sent task list notification SID {message.sid} to {member['name']}")
            return jsonify({"status": "success", "message": f"Notification sent to {member['name']}."}), 200
        except Exception as e:
            logger.error(f"Failed to send task list notification to {member['name']} ({member['phone']}): {e}")
            return jsonify({"error": "Failed to send Twilio message"}), 500

    except Exception as e:
        logger.error(f"Error during notify member {member_id}: {e}")
        return jsonify({"error": "Server error sending notification"}), 500


# --- Twilio Webhook ---
# (Keep the webhook function exactly as it was in the previous version - render_app_py_v4)
@app.route('/webhook', methods=['POST'])
def webhook():
    sender_number = request.values.get('From', '').replace('whatsapp:', '')
    incoming_msg = request.values.get('Body', '').strip().lower()
    response = MessagingResponse()
    today = date.today() # Get today's date for logic
    logger.info(f"Webhook: Received message from {sender_number}: '{incoming_msg}'")

    if not sender_number:
        response.message("Could not identify sender.")
        return Response(str(response), mimetype='application/xml')

    try:
        # Find member details
        member = execute_query("SELECT * FROM members WHERE phone = %s", (sender_number,), fetch_one=True)
        if not member:
            logger.warning(f"Webhook: Number {sender_number} not found.")
            response.message("Sorry, your number isn't registered. Please contact the admin.")
            return Response(str(response), mimetype='application/xml')

        member_id = member['id']
        member_name = member['name']

        # --- Command Processing ---
        if incoming_msg == 'tasks':
            # Shows tasks due in the next 7 days (including today)
            one_week_later = today + timedelta(days=7)
            tasks_query = """
                SELECT t.name, a.due_date
                FROM assignments a JOIN tasks t ON a.task_id = t.id
                WHERE a.member_id = %s AND a.completed = FALSE AND a.due_date >= %s AND a.due_date < %s
                ORDER BY a.due_date;
            """
            assignments = execute_query(tasks_query, (member_id, today, one_week_later), fetch_all=True)
            if not assignments:
                msg = f"Hi {member_name}! You have no tasks due in the next 7 days."
            else:
                days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                tasks_list = [f"- {a['name']} (Due: {days[a['due_date'].weekday()]} {a['due_date'].strftime('%Y-%m-%d')})" for a in assignments]
                tasks_str = "\n".join(tasks_list)
                msg = f"Hi {member_name}! Your tasks for the next 7 days:\n{tasks_str}"
            response.message(msg)

        elif incoming_msg.startswith('cant do '):
            task_name_input = incoming_msg[len('cant do '):].strip()
            if not task_name_input:
                response.message("Please specify which task you can't do, e.g., 'cant do Kitchen cleaning'")
            else:
                logger.info(f"Processing 'cant do' for task '{task_name_input}' from member {member_id} ({member_name})")
                assignment_query = """
                    SELECT a.id as assignment_id, a.task_id, t.name as task_name
                    FROM assignments a JOIN tasks t ON a.task_id = t.id
                    WHERE a.member_id = %s AND LOWER(t.name) = LOWER(%s) AND a.completed = FALSE AND a.due_date = %s
                    LIMIT 1;
                """
                original_assignment = execute_query(assignment_query, (member_id, task_name_input, today), fetch_one=True)
                if not original_assignment:
                    response.message(f"Sorry {member_name}, couldn't find an active task named '{task_name_input}' assigned to you for today.")
                else:
                    original_assignment_id = original_assignment['assignment_id']
                    task_id_to_reassign = original_assignment['task_id']
                    confirmed_task_name = original_assignment['task_name']
                    all_members = execute_query("SELECT id, name, phone FROM members ORDER BY date_added", fetch_all=True)
                    if len(all_members) <= 1:
                        response.message(f"Sorry {member_name}, you're the only member, so the task '{confirmed_task_name}' cannot be passed on.")
                    else:
                        member_ids = [m['id'] for m in all_members]
                        current_index = member_ids.index(member_id)
                        next_index = (current_index + 1) % len(all_members)
                        next_member = all_members[next_index]
                        next_member_id = next_member['id']
                        next_member_name = next_member['name']
                        next_member_phone = next_member['phone']
                        logger.info(f"Attempting to pass task '{confirmed_task_name}' from {member_name} to {next_member_name}")
                        check_query = "SELECT id FROM assignments WHERE task_id = %s AND member_id = %s AND due_date = %s AND completed = FALSE LIMIT 1;"
                        existing_reassignment = execute_query(check_query, (task_id_to_reassign, next_member_id, today), fetch_one=True)
                        if existing_reassignment:
                             logger.warning(f"Task '{confirmed_task_name}' already assigned to next member {next_member_name} for today.")
                             response.message(f"Task '{confirmed_task_name}' is already assigned to {next_member_name} for today. It cannot be passed again right now.")
                        else:
                            conn = get_db_connection()
                            if not conn: response.message("Database connection error, cannot reassign task.")
                            else:
                                try:
                                    with conn.cursor() as cur:
                                        cur.execute("DELETE FROM assignments WHERE id = %s", (original_assignment_id,))
                                        logger.info(f"Deleted original assignment {original_assignment_id} for task '{confirmed_task_name}' for member {member_id}")
                                        cur.execute("INSERT INTO assignments (task_id, member_id, assigned_date, due_date) VALUES (%s, %s, CURRENT_TIMESTAMP, %s)", (task_id_to_reassign, next_member_id, today))
                                        logger.info(f"Inserted new assignment for task '{confirmed_task_name}' for next member {next_member_id} due today")
                                    conn.commit()
                                    response.message(f"Okay {member_name}, task '{confirmed_task_name}' has been passed to {next_member_name}.")
                                    if twilio_client and next_member_phone:
                                        try:
                                            notification_body = f"Hi {next_member_name}, task '{confirmed_task_name}' was passed to you by {member_name} and is due today."
                                            twilio_client.messages.create(body=notification_body, from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}", to=f"whatsapp:{next_member_phone}")
                                            logger.info(f"Sent reassignment notification to {next_member_name} for task '{confirmed_task_name}'")
                                        except Exception as twilio_err: logger.error(f"Failed to send reassignment notification to {next_member_name}: {twilio_err}")
                                    else: logger.warning("Could not send reassignment notification (Twilio client or phone missing).")
                                except Exception as db_err:
                                    logger.error(f"Database error during reassignment: {db_err}"); conn.rollback()
                                    response.message("An error occurred trying to reassign the task. Please contact admin.")
                                finally: conn.close()

        elif incoming_msg.startswith('done '):
            # (Keep the 'done' logic exactly as it was)
            task_name_input = incoming_msg[5:].strip()
            if not task_name_input: response.message("Please specify which task is done, e.g., 'done Kitchen cleaning'")
            else:
                assignment_query = "SELECT a.id, t.name as task_name FROM assignments a JOIN tasks t ON a.task_id = t.id WHERE a.member_id = %s AND LOWER(t.name) = LOWER(%s) AND a.completed = FALSE ORDER BY a.due_date LIMIT 1;"
                assignment = execute_query(assignment_query, (member_id, task_name_input), fetch_one=True)
                if assignment:
                    update_query = "UPDATE assignments SET completed = TRUE, completion_date = CURRENT_TIMESTAMP WHERE id = %s;"
                    execute_query(update_query, (assignment['id'],), commit=True)
                    confirmed_task_name = assignment['task_name']
                    response.message(f"Great job, {member_name}! Task '{confirmed_task_name}' marked as complete.")
                    logger.info(f"Marked task '{confirmed_task_name}' complete for member {member_id}")
                else: response.message(f"Sorry {member_name}, couldn't find an active task matching '{task_name_input}'. Try the exact name from 'tasks' command.")

        elif incoming_msg == 'help':
             response.message("Commands:\n- tasks : List your tasks due in the next 7 days\n- done [task name] : Mark a task as complete\n- cant do [task name] : Pass today's task to the next person\n- help : Show this message")

        else:
            response.message("Sorry, I didn't understand that. Send 'help' for commands.")

    except Exception as e:
        logger.error(f"Error processing webhook for {sender_number}: {e}", exc_info=True)
        error_response = MessagingResponse()
        error_response.message("Sorry, an internal error occurred processing your request. Please try again later.")
        return Response(str(error_response), mimetype='application/xml')

    return Response(str(response), mimetype='application/xml')


# --- Manual DB Init Route ---
# (Keep the manual_db_init_route exactly as it was)
@app.route('/_init_db_manual_route', methods=['GET'])
def manual_db_init_route():
    secret = request.args.get('secret')
    if not INIT_DB_SECRET or secret != INIT_DB_SECRET: logger.warning("Unauthorized attempt to access manual DB init route."); return "Unauthorized", 403
    logger.warning("Manual DB init route accessed successfully!")
    try: init_db(); return "Database initialization attempted. Check logs.", 200
    except Exception as e: logger.error(f"Error during manual DB init: {e}"); return f"Error during manual DB init: {e}", 500

if __name__ == '__main__':
    # (Keep the __main__ block exactly as it was)
    port = int(os.environ.get('PORT', 5001))
    debug_mode = os.getenv('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)