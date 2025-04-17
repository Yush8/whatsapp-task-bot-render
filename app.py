import os
import logging
import uuid
from datetime import datetime, timedelta, date
import psycopg2
from psycopg2.extras import DictCursor
from flask import Flask, request, jsonify, render_template, Response
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

# Import Blueprints
from api.members import members_bp
from api.tasks import tasks_bp
from api.assignments import assignments_bp
from api.notifications import notifications_bp

# --- Configuration ---
load_dotenv()

app = Flask(__name__) # Flask app instance

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__) # Use __name__ for logger

# Get credentials and Database URL from environment variables
DATABASE_URL = os.getenv('DATABASE_URL')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')
INIT_DB_SECRET = os.getenv('INIT_DB_SECRET')

# Initialize Twilio client (can be shared via app context if needed in Blueprints)
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio client initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize Twilio client: {e}")
else:
    logger.warning("Twilio credentials not found.")

# --- Database Helper Functions (Can be moved to database.py) ---
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

# --- Database Initialization (Corrected Version - No Default Tasks) ---
def init_db():
    """Creates/updates the database tables without adding default tasks."""
    # NOTE: Ensure uuid-ossp extension is available for uuid_generate_v4()
    # Running this on an existing DB should be safe due to IF NOT EXISTS clauses.
    commands = [
        # Enable UUID generation
        """
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
        """,
        # Create members table with new columns
        """
        CREATE TABLE IF NOT EXISTS members (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            completed_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            missed_count INTEGER NOT NULL DEFAULT 0,
            is_away BOOLEAN NOT NULL DEFAULT FALSE,
            away_until DATE
        );
        """,
        # Add member columns safely if they don't exist
        """ALTER TABLE members ADD COLUMN IF NOT EXISTS completed_count INTEGER NOT NULL DEFAULT 0;""",
        """ALTER TABLE members ADD COLUMN IF NOT EXISTS skipped_count INTEGER NOT NULL DEFAULT 0;""",
        """ALTER TABLE members ADD COLUMN IF NOT EXISTS missed_count INTEGER NOT NULL DEFAULT 0;""",
        """ALTER TABLE members ADD COLUMN IF NOT EXISTS is_away BOOLEAN NOT NULL DEFAULT FALSE;""",
        """ALTER TABLE members ADD COLUMN IF NOT EXISTS away_until DATE;""",
        # Create tasks table with frequency and day column/constraint
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            frequency TEXT CHECK (frequency IN ('weekly', 'daily', 'monthly', 'manual')),
            recur_day_of_week INTEGER CHECK (recur_day_of_week BETWEEN 0 AND 6), -- 0=Monday, 6=Sunday
            date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT freq_day_check CHECK (frequency != 'weekly' OR recur_day_of_week IS NOT NULL)
        );
        """,
        # Add task column safely if it doesn't exist
        """
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS recur_day_of_week INTEGER CHECK (recur_day_of_week BETWEEN 0 AND 6);
        """,
        # Add task constraint safely if it doesn't exist
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'freq_day_check' AND table_name = 'tasks'
            ) THEN
                ALTER TABLE tasks ADD CONSTRAINT freq_day_check
                CHECK (frequency != 'weekly' OR recur_day_of_week IS NOT NULL);
            END IF;
        END $$;
        """,
        # Create assignments table
        """
        CREATE TABLE IF NOT EXISTS assignments (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            member_id UUID NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            assigned_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            due_date DATE NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            completion_date TIMESTAMP WITH TIME ZONE,
            UNIQUE(member_id, task_id, due_date)
        );
        """
        # --- Default Task INSERT statements REMOVED ---
    ]
    conn = get_db_connection()
    if conn is None:
        logger.error("Cannot initialize DB: No connection.")
        return
    try:
        with conn.cursor() as cur:
            for i, command in enumerate(commands):
                # Ensure command is a non-empty string before executing
                if command and isinstance(command, str) and command.strip():
                     logger.debug(f"Executing DB command #{i+1}: {command[:100].strip()}...")
                     cur.execute(command)
                else:
                     logger.warning(f"Skipping empty or invalid command at index {i}")
        conn.commit()
        logger.info("Database tables checked/created/updated successfully (no default tasks added).")
    except Exception as e:
        logger.error(f"Error initializing/updating database: {e}")
        conn.rollback()
        raise # Re-raise after logging
    finally:
        if conn:
            conn.close()

# --- Register Blueprints ---
app.register_blueprint(members_bp)
app.register_blueprint(tasks_bp)
# Register assignments_bp without a prefix, its routes start with /assign or /assignments
app.register_blueprint(assignments_bp, url_prefix='/api')
app.register_blueprint(notifications_bp)


# --- Core Routes ---
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

# --- Twilio Webhook ---
@app.route('/webhook', methods=['POST'])
def webhook():
    sender_number = request.values.get('From', '').replace('whatsapp:', '')
    incoming_msg = request.values.get('Body', '').strip().lower()
    response = MessagingResponse()
    today = date.today()
    logger.info(f"Webhook: Received message from {sender_number}: '{incoming_msg}'")

    if not sender_number: response.message("Could not identify sender."); return Response(str(response), mimetype='application/xml')

    try:
        member = execute_query("SELECT * FROM members WHERE phone = %s", (sender_number,), fetch_one=True)
        if not member: logger.warning(f"Webhook: Number {sender_number} not found."); response.message("Sorry, your number isn't registered."); return Response(str(response), mimetype='application/xml')

        member_id = member['id']
        member_name = member['name']

        # --- Command Processing ---
        if incoming_msg == 'tasks':
            # (Keep the 'tasks' logic exactly as it was in previous version)
            one_week_later = today + timedelta(days=7)
            tasks_query = "SELECT t.name, a.due_date FROM assignments a JOIN tasks t ON a.task_id = t.id WHERE a.member_id = %s AND a.completed = FALSE AND a.due_date >= %s AND a.due_date < %s ORDER BY a.due_date;"
            assignments = execute_query(tasks_query, (member_id, today, one_week_later), fetch_all=True)
            if not assignments: msg = f"Hi {member_name}! You have no tasks due in the next 7 days."
            else:
                days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                tasks_list = [f"- {a['name']} (Due: {days[a['due_date'].weekday()]} {a['due_date'].strftime('%Y-%m-%d')})" for a in assignments]
                tasks_str = "\n".join(tasks_list); msg = f"Hi {member_name}! Your tasks for the next 7 days:\n{tasks_str}"
            response.message(msg)

        elif incoming_msg.startswith('cant do '):
            # (Keep the 'cant do' logic exactly as it was, BUT ADD COUNTER UPDATE)
            task_name_input = incoming_msg[len('cant do '):].strip()
            if not task_name_input: response.message("Please specify which task you can't do.")
            else:
                logger.info(f"Processing 'cant do' for task '{task_name_input}' from member {member_id} ({member_name})")
                assignment_query = "SELECT a.id as assignment_id, a.task_id, t.name as task_name FROM assignments a JOIN tasks t ON a.task_id = t.id WHERE a.member_id = %s AND LOWER(t.name) = LOWER(%s) AND a.completed = FALSE AND a.due_date = %s LIMIT 1;"
                original_assignment = execute_query(assignment_query, (member_id, task_name_input, today), fetch_one=True)
                if not original_assignment: response.message(f"Sorry {member_name}, couldn't find task '{task_name_input}' assigned to you for today.")
                else:
                    # --- Start Pass Logic ---
                    original_assignment_id = original_assignment['assignment_id']; task_id_to_reassign = original_assignment['task_id']; confirmed_task_name = original_assignment['task_name']
                    all_members = execute_query("SELECT id, name, phone FROM members ORDER BY date_added", fetch_all=True)
                    if len(all_members) <= 1: response.message(f"Sorry, you're the only member, task '{confirmed_task_name}' cannot be passed.")
                    else:
                        member_ids = [m['id'] for m in all_members]; current_index = member_ids.index(member_id); next_index = (current_index + 1) % len(all_members)
                        next_member = all_members[next_index]; next_member_id = next_member['id']; next_member_name = next_member['name']; next_member_phone = next_member['phone']
                        logger.info(f"Attempting to pass task '{confirmed_task_name}' from {member_name} to {next_member_name}")
                        check_query = "SELECT id FROM assignments WHERE task_id = %s AND member_id = %s AND due_date = %s AND completed = FALSE LIMIT 1;"
                        existing_reassignment = execute_query(check_query, (task_id_to_reassign, next_member_id, today), fetch_one=True)
                        if existing_reassignment: logger.warning(f"Task '{confirmed_task_name}' already assigned to next member {next_member_name} for today."); response.message(f"Task '{confirmed_task_name}' is already with {next_member_name} for today.")
                        else:
                            conn = get_db_connection()
                            if not conn: response.message("DB error, cannot reassign.")
                            else:
                                try:
                                    with conn.cursor() as cur:
                                        cur.execute("DELETE FROM assignments WHERE id = %s", (original_assignment_id,))
                                        cur.execute("INSERT INTO assignments (task_id, member_id, assigned_date, due_date) VALUES (%s, %s, CURRENT_TIMESTAMP, %s)", (task_id_to_reassign, next_member_id, today))
                                        # --- INCREMENT SKIP COUNTER ---
                                        cur.execute("UPDATE members SET skipped_count = skipped_count + 1 WHERE id = %s", (member_id,))
                                        # --- END INCREMENT ---
                                    conn.commit()
                                    logger.info(f"Passed task '{confirmed_task_name}' to {next_member_name}. Incremented skip count for {member_name}.")
                                    response.message(f"Okay {member_name}, task '{confirmed_task_name}' passed to {next_member_name}.")
                                    if twilio_client and next_member_phone:
                                        try:
                                            notification_body = f"Hi {next_member_name}, task '{confirmed_task_name}' was passed to you by {member_name} and is due today."
                                            twilio_client.messages.create(body=notification_body, from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}", to=f"whatsapp:{next_member_phone}")
                                            logger.info(f"Sent reassignment notification to {next_member_name}")
                                        except Exception as twilio_err: logger.error(f"Failed to send reassignment notification: {twilio_err}")
                                    else: logger.warning("Could not send reassignment notification")
                                except Exception as db_err: logger.error(f"DB error during reassignment: {db_err}"); conn.rollback(); response.message("Error reassigning task.")
                                finally: conn.close()
                    # --- End Pass Logic ---


        elif incoming_msg.startswith('done '):
            # (Keep the 'done' logic, BUT ADD COUNTER UPDATE)
            task_name_input = incoming_msg[5:].strip()
            if not task_name_input: response.message("Please specify which task is done.")
            else:
                assignment_query = "SELECT a.id, t.name as task_name FROM assignments a JOIN tasks t ON a.task_id = t.id WHERE a.member_id = %s AND LOWER(t.name) = LOWER(%s) AND a.completed = FALSE ORDER BY a.due_date LIMIT 1;"
                assignment = execute_query(assignment_query, (member_id, task_name_input), fetch_one=True)
                if assignment:
                    conn = get_db_connection()
                    if not conn: response.message("DB error, cannot mark task done.")
                    else:
                        try:
                            with conn.cursor() as cur:
                                # Mark assignment complete
                                cur.execute("UPDATE assignments SET completed = TRUE, completion_date = CURRENT_TIMESTAMP WHERE id = %s;", (assignment['id'],))
                                # --- INCREMENT COMPLETED COUNTER ---
                                cur.execute("UPDATE members SET completed_count = completed_count + 1 WHERE id = %s", (member_id,))
                                # --- END INCREMENT ---
                            conn.commit()
                            confirmed_task_name = assignment['task_name']
                            logger.info(f"Marked task '{confirmed_task_name}' complete for {member_name}. Incremented complete count.")
                            response.message(f"Great job, {member_name}! Task '{confirmed_task_name}' marked as complete.")
                        except Exception as db_err: logger.error(f"DB error marking done: {db_err}"); conn.rollback(); response.message("Error marking task done.")
                        finally: conn.close()
                else: response.message(f"Sorry {member_name}, couldn't find active task '{task_name_input}'.")

        # --- NEW: Handle "away" command ---
        elif incoming_msg.startswith('away until '):
            date_str = incoming_msg[len('away until '):].strip()
            try:
                away_until_date = date.fromisoformat(date_str)
                if away_until_date <= today:
                    response.message("Please provide a future date in YYYY-MM-DD format for when you'll be back.")
                else:
                    # Update member status in DB
                    query = "UPDATE members SET is_away = TRUE, away_until = %s WHERE id = %s;"
                    execute_query(query, (away_until_date, member_id), commit=True)
                    logger.info(f"Member {member_name} marked away until {away_until_date}")
                    response.message(f"Okay {member_name}, I've marked you as away until {away_until_date.strftime('%Y-%m-%d')}. You won't be assigned recurring tasks until then.")
            except ValueError:
                response.message("Invalid date format. Please use YYYY-MM-DD, e.g., 'away until 2025-05-30'.")
            except Exception as e:
                logger.error(f"Error setting member away via webhook: {e}")
                response.message("Sorry, there was an error setting your away status.")

        # --- NEW: Handle "back" command ---
        elif incoming_msg == 'back':
            try:
                # Update member status in DB
                query = "UPDATE members SET is_away = FALSE, away_until = NULL WHERE id = %s;"
                execute_query(query, (member_id,), commit=True)
                logger.info(f"Member {member_name} marked as back/available via webhook.")
                response.message(f"Welcome back, {member_name}! You will now be included in the task rotation again.")
            except Exception as e:
                logger.error(f"Error setting member back via webhook: {e}")
                response.message("Sorry, there was an error updating your availability.")

        elif incoming_msg == 'help':
             response.message("Commands:\n"
                              "- tasks : List your tasks due in the next 7 days\n"
                              "- done [task name] : Mark task as complete\n"
                              "- cant do [task name] : Pass today's task to next person\n"
                              "- away until YYYY-MM-DD : Mark yourself unavailable\n"
                              "- back : Mark yourself available again\n"
                              "- help : Show this message")

        else:
            response.message("Sorry, I didn't understand. Send 'help' for commands.")

    except Exception as e:
        logger.error(f"Error processing webhook for {sender_number}: {e}", exc_info=True)
        error_response = MessagingResponse()
        error_response.message("Sorry, internal error processing request.")
        return Response(str(error_response), mimetype='application/xml')

    return Response(str(response), mimetype='application/xml')

# --- Manual DB Init Route ---
# (Keep the manual_db_init_route exactly as it was)
@app.route('/_init_db_manual_route', methods=['GET'])
def manual_db_init_route():
    secret = request.args.get('secret')
    if not INIT_DB_SECRET or secret != INIT_DB_SECRET: logger.warning("Unauthorized DB init attempt."); return "Unauthorized", 403
    logger.warning("Manual DB init route accessed!")
    try: init_db(); return "DB init attempted. Check logs.", 200
    except Exception as e: logger.error(f"Manual DB init error: {e}"); return f"Error: {e}", 500

# --- Main Execution ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    debug_mode = os.getenv('FLASK_ENV') == 'development'
    # Note: Use gunicorn for production on Render via Procfile or Start Command
    app.run(host='0.0.0.0', port=port, debug=debug_mode)