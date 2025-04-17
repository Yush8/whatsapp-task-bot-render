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
load_dotenv() # Load environment variables from .env file for local dev

app = Flask(__name__)

# Configure logging
# Ensure logs are captured by Render
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()] # Log to stdout/stderr for Render
)
logger = logging.getLogger(__name__)

# Get credentials and Database URL from environment variables
DATABASE_URL = os.getenv('DATABASE_URL')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')

# Initialize Twilio client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio client initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize Twilio client: {e}")
else:
    logger.warning("Twilio credentials not found in environment variables.")

# --- Database Helper Functions (PostgreSQL) ---

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
        return None if fetch_one else [] # Return None or empty list on connection failure
    result_data = None
    last_row_id = None # Usually not needed with RETURNING in PG
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(query, params)
            if fetch_one:
                result_data = cur.fetchone()
                if result_data and 'id' in result_data: # Example if using RETURNING id
                    last_row_id = result_data['id']
            elif fetch_all:
                result_data = cur.fetchall()

            if commit:
                conn.commit()
    except Exception as e:
        logger.error(f"Database query failed: {e}")
        conn.rollback() # Rollback on error
        # Re-raise or return specific error indicator if needed
        raise # Re-raise the exception to be handled by the caller route
    finally:
        if conn:
            conn.close()

    # Convert rows to plain dicts if needed
    if result_data is None:
         return None if fetch_one else []
    if fetch_one:
        return dict(result_data)
    if fetch_all:
        return [dict(row) for row in result_data]
    return True # Indicate success for commit=True without fetch

# --- Database Initialization (Run once manually) ---

def init_db():
    """Creates the database tables if they don't exist."""
    # Ensure uuid-ossp extension is available for uuid_generate_v4()
    commands = [
        """
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
        """,
        """
        CREATE TABLE IF NOT EXISTS members (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            name TEXT NOT NULL UNIQUE, -- Ensure task names are unique
            description TEXT,
            frequency TEXT CHECK (frequency IN ('weekly', 'daily', 'monthly', 'manual')), -- Added 'manual', restricted values
            date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS assignments (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            assigned_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            due_date DATE NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            completion_date TIMESTAMP WITH TIME ZONE,
            UNIQUE(member_id, task_id, due_date) -- Prevent assigning same task to same person on same due date
        );
        """,
         # Add default tasks if table is empty
        """
        INSERT INTO tasks (name, description, frequency)
        SELECT 'Kitchen cleaning', 'Clean kitchen surfaces and floor', 'weekly'
        WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE name = 'Kitchen cleaning');
        """,
        """
        INSERT INTO tasks (name, description, frequency)
        SELECT 'Bathroom cleaning', 'Clean bathroom, including shower, toilet and sink', 'weekly'
        WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE name = 'Bathroom cleaning');
        """
    ]
    conn = get_db_connection()
    if conn is None:
        logger.error("Cannot initialize DB: No connection.")
        return
    try:
        with conn.cursor() as cur:
            for command in commands:
                cur.execute(command)
        conn.commit()
        logger.info("Database tables checked/created successfully.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        conn.rollback()
    finally:
        if conn:
            conn.close()

# --- Flask Routes ---

@app.route('/')
def home_page():
    # Simple HTML page for documentation or status
    return render_template('index.html')

@app.route('/health', methods=['GET'])
def health_check():
    # Basic health check
    conn = get_db_connection()
    db_ok = False
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
            db_ok = True
        except Exception as e:
             logger.error(f"Health check DB query failed: {e}")
        finally:
             conn.close()
    db_status = "connected" if db_ok else "disconnected_or_error"

    return jsonify({
        "status": "healthy",
        "database": db_status,
        "twilio_client": "initialized" if twilio_client else "not_initialized"
    }), 200

# --- API Endpoints ---

# -- Members --
@app.route('/api/members', methods=['GET'])
def get_members():
    try:
        members = execute_query("SELECT id, name, phone, date_added FROM members ORDER BY date_added", fetch_all=True)
        return jsonify(members)
    except Exception as e:
        logger.error(f"Error getting members: {e}")
        return jsonify({"error": "Server error retrieving members"}), 500

@app.route('/api/members', methods=['POST'])
def add_member():
    data = request.json
    if not data or 'name' not in data or 'phone' not in data:
        return jsonify({"error": "Name and phone required"}), 400

    phone = data['phone']
    if not phone.startswith('+'):
         return jsonify({"error": "Phone number must start with +"}), 400

    # Check if phone number already exists
    try:
        existing_member = execute_query("SELECT id FROM members WHERE phone = %s", (phone,), fetch_one=True)
        if existing_member:
            return jsonify({"error": "Phone number already exists", "member_id": existing_member['id']}), 409

        # Insert new member
        query = "INSERT INTO members (name, phone) VALUES (%s, %s) RETURNING id, name, phone, date_added;"
        new_member = execute_query(query, (data['name'], phone), fetch_one=True, commit=True)
        if new_member:
             return jsonify({"status": "success", "member": new_member}), 201
        else:
             # This case should ideally not happen if unique constraint works
             logger.error(f"Failed to insert member or retrieve details. Data: {data}")
             return jsonify({"error": "Failed to add member"}), 500
    except Exception as e:
         logger.error(f"Error adding member: {e}")
         return jsonify({"error": "Server error adding member"}), 500

# Add DELETE /api/members/<id> if needed

# -- Tasks --
@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    try:
        tasks = execute_query("SELECT id, name, description, frequency, date_added FROM tasks ORDER BY name", fetch_all=True)
        return jsonify(tasks)
    except Exception as e:
        logger.error(f"Error getting tasks: {e}")
        return jsonify({"error": "Server error retrieving tasks"}), 500

@app.route('/api/tasks', methods=['POST'])
def add_task():
    data = request.json
    if not data or 'name' not in data:
        return jsonify({"error": "Task name required"}), 400

    name = data['name']
    description = data.get('description') # Optional
    # Default to manual unless specified, validate frequency
    frequency = data.get('frequency', 'manual')
    valid_frequencies = ['daily', 'weekly', 'monthly', 'manual']
    if frequency not in valid_frequencies:
        return jsonify({"error": f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"}), 400

    # Check if task name already exists
    try:
        existing_task = execute_query("SELECT id FROM tasks WHERE name = %s", (name,), fetch_one=True)
        if existing_task:
            return jsonify({"error": "Task name already exists", "task_id": existing_task['id']}), 409

        # Insert new task
        query = "INSERT INTO tasks (name, description, frequency) VALUES (%s, %s, %s) RETURNING id, name, description, frequency, date_added;"
        new_task = execute_query(query, (name, description, frequency), fetch_one=True, commit=True)
        if new_task:
            return jsonify({"status": "success", "task": new_task}), 201
        else:
            logger.error(f"Failed to insert task or retrieve details. Data: {data}")
            return jsonify({"error": "Failed to add task"}), 500
    except Exception as e:
        logger.error(f"Error adding task: {e}")
        return jsonify({"error": "Server error adding task"}), 500

# Add DELETE /api/tasks/<id> if needed

# -- Assignments --
@app.route('/api/assign', methods=['POST'])
def assign_task():
    data = request.json
    required_fields = ['member_id', 'task_id', 'due_date']
    if not data or not all(field in data for field in required_fields):
        return jsonify({"error": f"Missing required fields: {', '.join(required_fields)}"}), 400

    member_id = data['member_id']
    task_id = data['task_id']
    due_date_str = data['due_date']

    # Validate UUIDs (basic check)
    try:
        uuid.UUID(member_id)
        uuid.UUID(task_id)
    except ValueError:
        return jsonify({"error": "Invalid member_id or task_id format (must be UUID)"}), 400

    # Validate date
    try:
        due_date = date.fromisoformat(due_date_str)
    except ValueError:
        return jsonify({"error": "Invalid due_date format (must be YYYY-MM-DD)"}), 400

    # Check if member and task exist (optional, DB constraints handle it but good practice)

    # Prevent duplicate assignment for same member/task/due_date
    try:
        check_query = """
        SELECT id FROM assignments
        WHERE member_id = %s AND task_id = %s AND due_date = %s AND completed = FALSE
        """
        existing = execute_query(check_query, (member_id, task_id, due_date), fetch_one=True)
        if existing:
            return jsonify({"error": "This task is already assigned to this member for this due date and is not completed."}), 409

        # Insert new assignment
        query = """
        INSERT INTO assignments (member_id, task_id, due_date)
        VALUES (%s, %s, %s)
        RETURNING id, member_id, task_id, assigned_date, due_date, completed;
        """
        new_assignment = execute_query(query, (member_id, task_id, due_date), fetch_one=True, commit=True)
        if new_assignment:
            return jsonify({"status": "success", "assignment": new_assignment}), 201
        else:
             logger.error(f"Failed to insert assignment or retrieve details. Data: {data}")
             return jsonify({"error": "Failed to assign task"}), 500
    except Exception as e:
        logger.error(f"Error assigning task: {e}")
        # Handle potential foreign key violations if member/task don't exist
        if "violates foreign key constraint" in str(e):
             return jsonify({"error": "Invalid member_id or task_id provided."}), 404
        return jsonify({"error": "Server error assigning task"}), 500

# Add GET /api/assignments and POST /api/assignments/<id>/complete if needed

# -- Notifications --
@app.route('/api/notify/all_chores', methods=['POST'])
def notify_all_chores():
    if not twilio_client:
        return jsonify({"error": "Twilio client not configured"}), 500

    try:
        members = execute_query("SELECT id, name, phone FROM members", fetch_all=True)
        if not members:
            return jsonify({"status": "No members found to notify"}), 200

        logger.info(f"Attempting to send generic chore notification to {len(members)} members.")
        success_count = 0
        error_count = 0
        for member in members:
            message_body = f"Hi {member['name']}, friendly reminder to check and complete your assigned chores!"
            # Optional: Could query specific tasks for each member here if desired
            try:
                message = twilio_client.messages.create(
                    body=message_body,
                    from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                    to=f"whatsapp:{member['phone']}"
                )
                logger.info(f"Sent notification SID {message.sid} to {member['name']}")
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to send notification to {member['name']} ({member['phone']}): {e}")
                error_count += 1

        return jsonify({
            "status": "Notification process completed.",
            "successful_notifications": success_count,
            "failed_notifications": error_count
        })

    except Exception as e:
        logger.error(f"Error during notify all chores: {e}")
        return jsonify({"error": "Server error sending notifications"}), 500


# --- Twilio Webhook ---
@app.route('/webhook', methods=['POST'])
def webhook():
    sender_number = request.values.get('From', '').replace('whatsapp:', '')
    incoming_msg = request.values.get('Body', '').strip().lower()
    response = MessagingResponse()
    logger.info(f"Received message from {sender_number}: '{incoming_msg}'")

    if not sender_number:
        response.message("Could not identify sender.")
        return Response(str(response), mimetype='application/xml')

    # Find member by phone
    try:
        member = execute_query("SELECT * FROM members WHERE phone = %s", (sender_number,), fetch_one=True)

        if not member:
            logger.warning(f"Number {sender_number} not found in members.")
            response.message("Sorry, your number isn't registered. Please contact the admin.")
            return Response(str(response), mimetype='application/xml')

        member_id = member['id']
        member_name = member['name']

        # --- Command Processing ---
        if incoming_msg == 'tasks':
            tasks_query = """
                SELECT t.name, a.due_date
                FROM assignments a
                JOIN tasks t ON a.task_id = t.id
                WHERE a.member_id = %s AND a.completed = FALSE
                ORDER BY a.due_date;
            """
            assignments = execute_query(tasks_query, (member_id,), fetch_all=True)
            if not assignments:
                msg = f"Hi {member_name}! You have no outstanding tasks."
            else:
                tasks_list = "\n".join([f"- {a['name']} (Due: {a['due_date'].strftime('%Y-%m-%d')})" for a in assignments])
                msg = f"Hi {member_name}! Your current tasks:\n{tasks_list}"
            response.message(msg)

        elif incoming_msg.startswith('done '):
            task_name_input = incoming_msg[5:].strip()
            if not task_name_input:
                 response.message("Please specify which task is done, e.g., 'done Kitchen cleaning'")
            else:
                # Find the task and mark it complete (simplistic matching by name)
                assignment_query = """
                    SELECT a.id, t.name as task_name
                    FROM assignments a
                    JOIN tasks t ON a.task_id = t.id
                    WHERE a.member_id = %s AND a.completed = FALSE AND LOWER(t.name) = LOWER(%s)
                    ORDER BY a.due_date
                    LIMIT 1;
                """
                assignment = execute_query(assignment_query, (member_id, task_name_input), fetch_one=True)

                if assignment:
                    update_query = "UPDATE assignments SET completed = TRUE, completion_date = CURRENT_TIMESTAMP WHERE id = %s;"
                    execute_query(update_query, (assignment['id'],), commit=True)
                    confirmed_task_name = assignment['task_name']
                    response.message(f"Great job, {member_name}! Task '{confirmed_task_name}' marked as complete.")
                    logger.info(f"Marked task '{confirmed_task_name}' complete for member {member_id}")
                else:
                    response.message(f"Sorry {member_name}, couldn't find an active task matching '{task_name_input}'. Try the exact name from 'tasks' command.")

        elif incoming_msg == 'help':
             response.message("Commands:\n- tasks : List your current tasks\n- done [task name] : Mark a task as complete\n- help : Show this message")

        else:
            response.message("Sorry, I didn't understand that. Send 'help' for commands.")

    except Exception as e:
        logger.error(f"Error processing webhook for {sender_number}: {e}")
        response.message("Sorry, an internal error occurred. Please try again later.")


    return Response(str(response), mimetype='application/xml')

# --- Manual DB Init Route (Remove or secure before production) ---
@app.route('/_init_db_manual_route', methods=['GET'])
def manual_db_init_route():
    # WARNING: This endpoint should be secured or removed in a real production environment.
    # It allows anyone to potentially re-run the DB initialization.
    # For setup, ensure it's only called once, then consider removing it.
    # Check for a secret query parameter for basic security:
    secret = request.args.get('secret')
    expected_secret = os.getenv('INIT_DB_SECRET') # Set this in Render Env Vars if used

    if not expected_secret or secret != expected_secret:
        logger.warning("Unauthorized attempt to access manual DB init route.")
        return "Unauthorized", 403

    logger.warning("Manual DB init route accessed successfully!")
    try:
        init_db()
        return "Database initialization attempted. Check logs.", 200
    except Exception as e:
        logger.error(f"Error during manual DB init: {e}")
        return f"Error during manual DB init: {e}", 500


if __name__ == '__main__':
    # Gunicorn runs the app using the Flask 'app' object in production (on Render)
    # This block allows direct execution for local testing (if needed)
    # It's better to use `flask run` or `gunicorn app:app` locally too
    port = int(os.environ.get('PORT', 5001))
    # Debug mode should be False in production
    # Use FLASK_ENV=development environment variable locally to enable debug mode if needed
    debug_mode = os.getenv('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)