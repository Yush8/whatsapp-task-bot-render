import os
import logging
import uuid
from datetime import datetime, timedelta
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Get credentials and Database URL from environment variables
# Render provides DATABASE_URL automatically when linked
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
        # Optional: Set SSL mode if required by your DB provider and not in the URL
        # e.g., psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection error: {e}")
        return None

def execute_query(query, params=None, fetch_one=False, fetch_all=False, commit=False):
    """Executes a query and optionally fetches results or commits."""
    conn = get_db_connection()
    if conn is None:
        return None
    result = None
    try:
        # Use DictCursor to get results as dictionary-like objects
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(query, params)
            if fetch_one:
                result = cur.fetchone()
            elif fetch_all:
                result = cur.fetchall()

            if commit:
                conn.commit()
                # If inserting and expecting lastrowid (less common in PG, use RETURNING)
                # For simplicity, we won't return generated IDs here directly
                # but you could modify query with 'RETURNING id' and fetch it
    except Exception as e:
        logger.error(f"Database query failed: {e}")
        conn.rollback() # Rollback on error
    finally:
        if conn:
            conn.close()
    # Convert rows to plain dicts if needed, check if result is None first
    if result is None:
        return None
    if fetch_one:
        return dict(result) if result else None
    if fetch_all:
        return [dict(row) for row in result]
    return result # Should only happen if fetch_one/fetch_all are False


# --- Database Initialization (Run once manually via Render Shell) ---

def init_db():
    """Creates the database tables if they don't exist."""
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
            name TEXT NOT NULL,
            description TEXT,
            frequency TEXT NOT NULL DEFAULT 'weekly',
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
            UNIQUE(member_id, task_id, assigned_date) -- Prevent duplicate assignments on same day
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
    return render_template('index.html') # We'll create this template

@app.route('/health', methods=['GET'])
def health_check():
    # Basic health check
    conn = get_db_connection()
    db_ok = False
    if conn:
        try:
            # Try a simple query
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

# --- API Endpoints (Example: Add Member) ---
@app.route('/api/members', methods=['POST'])
def add_member():
    data = request.json
    if not data or 'name' not in data or 'phone' not in data:
        return jsonify({"error": "Name and phone required"}), 400

    phone = data['phone']
    if not phone.startswith('+'):
         return jsonify({"error": "Phone number must start with +"}), 400

    # Check if phone number already exists
    existing_member = execute_query("SELECT id FROM members WHERE phone = %s", (phone,), fetch_one=True)
    if existing_member:
        return jsonify({"error": "Phone number already exists", "member_id": existing_member['id']}), 409

    # Insert new member
    query = "INSERT INTO members (name, phone) VALUES (%s, %s) RETURNING id;"
    try:
        # Use default UUID generation in PG now
        result = execute_query(query, (data['name'], phone), fetch_one=True, commit=True)
        if result and 'id' in result:
             return jsonify({"status": "success", "member_id": result['id']}), 201
        else:
             logger.error(f"Failed to insert member or retrieve ID. Data: {data}")
             return jsonify({"error": "Failed to add member"}), 500
    except Exception as e:
         # Catch potential unique constraint violation if check failed somehow, or other errors
         logger.error(f"Error adding member: {e}")
         return jsonify({"error": "Server error adding member"}), 500

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
                # Use the task name confirmed from the database query
                confirmed_task_name = assignment['task_name']
                response.message(f"Great job, {member_name}! Task '{confirmed_task_name}' marked as complete.")
                logger.info(f"Marked task '{confirmed_task_name}' complete for member {member_id}")
            else:
                response.message(f"Sorry {member_name}, couldn't find an active task matching '{task_name_input}'. Try the exact name from 'tasks' command.")

    elif incoming_msg == 'help':
         response.message("Commands:\n- tasks : List your current tasks\n- done [task name] : Mark a task as complete\n- help : Show this message")

    else:
        response.message("Sorry, I didn't understand that. Send 'help' for commands.")

    return Response(str(response), mimetype='application/xml')

# --- Manual DB Init Route (Remove or secure before production) ---
@app.route('/_init_db_manual_route', methods=['GET']) # Added route for clarity
def manual_db_init_route():
    # In a real app, secure this (e.g., check for a secret header/param)
    # or better yet, use Render's shell for one-time setup.
    logger.warning("Manual DB init route accessed!")
    try:
        init_db()
        return "Database initialization attempted. Check logs.", 200
    except Exception as e:
        logger.error(f"Error during manual DB init: {e}")
        return f"Error during manual DB init: {e}", 500


if __name__ == '__main__':
    # Gunicorn runs the app using the Flask 'app' object.
    # This block is mainly for potential local testing if needed.
    port = int(os.environ.get('PORT', 5001)) # Use a different default port locally if desired
    # Run debug=False because Gunicorn is for production
    app.run(host='0.0.0.0', port=port, debug=False)