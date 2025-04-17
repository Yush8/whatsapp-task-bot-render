import os
import logging
from datetime import date, timedelta
import psycopg2
from psycopg2.extras import DictCursor
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv() # Load .env for local testing if needed

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('ReminderScript')

# Get credentials and Database URL from environment variables
DATABASE_URL = os.getenv('DATABASE_URL')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')

# Initialize Twilio client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio client initialized for reminders.")
    except Exception as e:
        logger.error(f"Failed to initialize Twilio client: {e}")
else:
    logger.error("Twilio credentials missing for reminder script.")

# --- Database Helper Functions (Copied/Adapted from app.py for standalone use) ---

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

def execute_query(query, params=None, fetch_all=False):
     """Executes a query and fetches all results."""
     conn = get_db_connection()
     if conn is None:
         return [] # Return empty list on connection failure
     results = []
     try:
         with conn.cursor(cursor_factory=DictCursor) as cur:
             cur.execute(query, params)
             if fetch_all:
                 results = cur.fetchall()
     except Exception as e:
         logger.error(f"Database query failed: {e}")
         conn.rollback() # Rollback if error occurs during fetch (unlikely)
     finally:
         if conn:
             conn.close()
     # Convert Row objects to simple dicts to avoid issues after closing connection
     return [dict(row) for row in results]

# --- Main Reminder Logic ---
def send_reminders():
    if not twilio_client:
        logger.error("Cannot send reminders: Twilio client not initialized.")
        return
    if not DATABASE_URL:
        logger.error("Cannot send reminders: DATABASE_URL not set.")
        return

    logger.info("Starting reminder check...")
    today = date.today()
    tomorrow = today + timedelta(days=1)

    # Query for assignments due today or tomorrow that are not completed
    reminder_query = """
        SELECT m.name as member_name, m.phone as member_phone,
               t.name as task_name, t.description as task_description,
               a.due_date
        FROM assignments a
        JOIN members m ON a.member_id = m.id
        JOIN tasks t ON a.task_id = t.id
        WHERE a.completed = FALSE AND (a.due_date = %s OR a.due_date = %s);
    """

    try:
        assignments_due = execute_query(reminder_query, (today, tomorrow), fetch_all=True)
        logger.info(f"Found {len(assignments_due)} assignments due soon.")

        sent_count = 0
        failed_count = 0
        for assignment in assignments_due:
            try:
                due_date_str = assignment['due_date'].strftime('%Y-%m-%d')
                if assignment['due_date'] == today:
                    urgency = "due today"
                else:
                    urgency = "due tomorrow"

                message_body = f"Hi {assignment['member_name']}! Reminder: Your task '{assignment['task_name']}' is {urgency} ({due_date_str})."
                if assignment['task_description']:
                    message_body += f"\nDetails: {assignment['task_description']}"

                # Send WhatsApp message via Twilio
                message = twilio_client.messages.create(
                    body=message_body,
                    from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                    to=f"whatsapp:{assignment['member_phone']}"
                )
                logger.info(f"Sent reminder SID {message.sid} to {assignment['member_name']} for task '{assignment['task_name']}'")
                sent_count += 1
            except Exception as e:
                # Log failure for specific user but continue with others
                logger.error(f"Failed to send reminder to {assignment['member_name']} ({assignment['member_phone']}): {e}")
                failed_count += 1

        logger.info(f"Reminder check finished. Sent: {sent_count}, Failed: {failed_count}.")

    except Exception as e:
        # Log error for the overall process
        logger.error(f"An error occurred during the reminder process: {e}")

# --- Run the reminder function when script is executed ---
if __name__ == "__main__":
    logger.info("Reminder script started by direct execution.")
    send_reminders()
    logger.info("Reminder script finished.")