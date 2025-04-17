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
        logger.error(f"Database query failed: {e}")
        conn.rollback()
        raise
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
# (Keep the init_db function exactly as it was in the previous version - render_app_py_v3)
# NOTE: Ensure you have run this or equivalent ALTER commands to add the recur_day_of_week column
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
# (Keep /, /health, /api/members GET/POST, /api/tasks GET/POST, /api/assign POST, /api/notify/all_chores POST exactly as they were)
@app.route('/')
def home_page():
    return render_template('index.html')

@app.route('/health', methods=['GET'])
def health_check():
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

@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    try:
        tasks = execute_query("SELECT id, name, description, frequency, recur_day_of_week, date_added FROM tasks ORDER BY name", fetch_all=True)
        for task in tasks: task['recur_day_of_week'] = task.get('recur_day_of_week') # Ensure None if NULL
        return jsonify(tasks)
    except Exception as e: logger.error(f"Error getting tasks: {e}"); return jsonify({"error": "Server error retrieving tasks"}), 500

@app.route('/api/tasks', methods=['POST'])
def add_task():
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
        try:
            day_num = int(recur_day_of_week); assert 0 <= day_num <= 6; recur_day_of_week = day_num
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

@app.route('/api/assign', methods=['POST'])
def assign_task():
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

@app.route('/api/notify/all_chores', methods=['POST'])
def notify_all_chores():
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

# --- Twilio Webhook ---
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

        # --- NEW: Handle "cant do" command ---
        elif incoming_msg.startswith('cant do '):
            task_name_input = incoming_msg[len('cant do '):].strip()
            if not task_name_input:
                response.message("Please specify which task you can't do, e.g., 'cant do Kitchen cleaning'")
            else:
                logger.info(f"Processing 'cant do' for task '{task_name_input}' from member {member_id} ({member_name})")
                # Find the assignment for this user, matching task name, due *today*, and not completed
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
                    # Found the task, now find the next person
                    original_assignment_id = original_assignment['assignment_id']
                    task_id_to_reassign = original_assignment['task_id']
                    confirmed_task_name = original_assignment['task_name'] # Use confirmed name

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

                        # Check if the next person already has this task assigned and incomplete for today
                        check_query = "SELECT id FROM assignments WHERE task_id = %s AND member_id = %s AND due_date = %s AND completed = FALSE LIMIT 1;"
                        existing_reassignment = execute_query(check_query, (task_id_to_reassign, next_member_id, today), fetch_one=True)

                        if existing_reassignment:
                             logger.warning(f"Task '{confirmed_task_name}' already assigned to next member {next_member_name} for today.")
                             response.message(f"Task '{confirmed_task_name}' is already assigned to {next_member_name} for today. It cannot be passed again right now.")
                        else:
                            # Proceed with reassignment: Delete original, Insert new
                            conn = get_db_connection()
                            if not conn:
                                response.message("Database connection error, cannot reassign task.")
                            else:
                                try:
                                    with conn.cursor() as cur:
                                        # Delete original
                                        cur.execute("DELETE FROM assignments WHERE id = %s", (original_assignment_id,))
                                        logger.info(f"Deleted original assignment {original_assignment_id} for task '{confirmed_task_name}' for member {member_id}")
                                        # Insert new one for next person, due today
                                        cur.execute(
                                            "INSERT INTO assignments (task_id, member_id, assigned_date, due_date) VALUES (%s, %s, CURRENT_TIMESTAMP, %s)",
                                            (task_id_to_reassign, next_member_id, today)
                                        )
                                        logger.info(f"Inserted new assignment for task '{confirmed_task_name}' for next member {next_member_id} due today")
                                    conn.commit()

                                    # Send confirmations
                                    response.message(f"Okay {member_name}, task '{confirmed_task_name}' has been passed to {next_member_name}.")
                                    # Send notification to the next person
                                    if twilio_client and next_member_phone:
                                        try:
                                            notification_body = f"Hi {next_member_name}, task '{confirmed_task_name}' was passed to you by {member_name} and is due today."
                                            twilio_client.messages.create(
                                                body=notification_body,
                                                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                                                to=f"whatsapp:{next_member_phone}"
                                            )
                                            logger.info(f"Sent reassignment notification to {next_member_name} for task '{confirmed_task_name}'")
                                        except Exception as twilio_err:
                                            logger.error(f"Failed to send reassignment notification to {next_member_name}: {twilio_err}")
                                    else:
                                         logger.warning("Could not send reassignment notification (Twilio client or phone missing).")

                                except Exception as db_err:
                                    logger.error(f"Database error during reassignment: {db_err}")
                                    conn.rollback()
                                    response.message("An error occurred trying to reassign the task. Please contact admin.")
                                finally:
                                    conn.close()

        # --- Handle "done" command ---
        elif incoming_msg.startswith('done '):
            # This logic remains the same as before
            task_name_input = incoming_msg[5:].strip()
            if not task_name_input:
                 response.message("Please specify which task is done, e.g., 'done Kitchen cleaning'")
            else:
                # Find the assignment for this user, matching task name, not completed
                # Prioritize task due today if multiple match name
                assignment_query = """
                    SELECT a.id, t.name as task_name
                    FROM assignments a JOIN tasks t ON a.task_id = t.id
                    WHERE a.member_id = %s AND LOWER(t.name) = LOWER(%s) AND a.completed = FALSE
                    ORDER BY a.due_date -- Complete earliest due first
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
             response.message("Commands:\n"
                              "- tasks : List your tasks due in the next 7 days\n"
                              "- done [task name] : Mark a task as complete\n"
                              "- cant do [task name] : Pass today's task to the next person\n"
                              "- help : Show this message")

        else:
            response.message("Sorry, I didn't understand that. Send 'help' for commands.")

    except Exception as e:
        logger.error(f"Error processing webhook for {sender_number}: {e}", exc_info=True)
        # Use a fresh response object for error to avoid conflicts
        error_response = MessagingResponse()
        error_response.message("Sorry, an internal error occurred processing your request. Please try again later.")
        return Response(str(error_response), mimetype='application/xml')

    # Return the TwiML response
    return Response(str(response), mimetype='application/xml')

# --- Manual DB Init Route ---
# (Keep the manual_db_init_route exactly as it was)
@app.route('/_init_db_manual_route', methods=['GET'])
def manual_db_init_route():
    secret = request.args.get('secret')
    expected_secret = os.getenv('INIT_DB_SECRET')
    if not expected_secret or secret != expected_secret: logger.warning("Unauthorized attempt to access manual DB init route."); return "Unauthorized", 403
    logger.warning("Manual DB init route accessed successfully!")
    try: init_db(); return "Database initialization attempted. Check logs.", 200
    except Exception as e: logger.error(f"Error during manual DB init: {e}"); return f"Error during manual DB init: {e}", 500

if __name__ == '__main__':
    # (Keep the __main__ block exactly as it was)
    port = int(os.environ.get('PORT', 5001))
    debug_mode = os.getenv('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)