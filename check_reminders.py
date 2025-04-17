import os
import logging
from datetime import date, timedelta, datetime
import psycopg2
from psycopg2.extras import DictCursor
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv() # Load .env for local testing if needed

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()] # Log to stdout/stderr for Render Cron Job
)
logger = logging.getLogger('TaskCyclerReminderScript')

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
        logger.info("Twilio client initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize Twilio client: {e}")
else:
    logger.error("Twilio credentials missing.")

# --- Database Helper Functions ---

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
    """Executes a query, optionally fetches results or commits."""
    conn = get_db_connection()
    if conn is None:
        return None if fetch_one else []
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
        # Optionally re-raise or return error indicator
    finally:
        if conn:
            conn.close()

    # Convert rows to plain dicts
    if result_data is None:
         return None if fetch_one else []
    if fetch_one:
        return dict(result_data)
    if fetch_all:
        return [dict(row) for row in result_data]
    return True # Indicate success for commit=True without fetch

# --- Task Cycling Logic ---

def cycle_recurring_tasks():
    logger.info("Starting recurring task cycling check...")
    today = date.today()

    try:
        # Get members ordered by date added for cycle rotation
        members = execute_query("SELECT id FROM members ORDER BY date_added", fetch_all=True)
        if not members:
            logger.info("No members found, skipping task cycling.")
            return
        member_ids = [m['id'] for m in members]
        num_members = len(member_ids)

        # Get tasks marked for recurring frequency (e.g., 'weekly')
        # Add other frequencies like 'daily', 'monthly' here if needed
        recurring_tasks = execute_query(
            "SELECT id, name, frequency FROM tasks WHERE frequency = 'weekly'", # Add other frequencies later
            fetch_all=True
        )
        if not recurring_tasks:
            logger.info("No recurring tasks found to cycle.")
            return

        logger.info(f"Found {len(recurring_tasks)} recurring tasks to check.")
        assignments_created = 0

        for task in recurring_tasks:
            task_id = task['id']
            task_name = task['name']
            frequency = task['frequency'] # Currently only 'weekly'

            # Find the most recent assignment for this task to determine the last assignee and date
            last_assignment_query = """
                SELECT member_id, assigned_date, due_date
                FROM assignments
                WHERE task_id = %s
                ORDER BY assigned_date DESC, id DESC -- Use id as tiebreaker if assigned same day
                LIMIT 1;
            """
            last_assignment = execute_query(last_assignment_query, (task_id,), fetch_one=True)

            next_member_id = None
            next_due_date = None
            should_assign = False

            if not last_assignment:
                # First time assigning this task
                logger.info(f"Task '{task_name}' ({task_id}) has no previous assignment. Assigning to first member.")
                next_member_id = member_ids[0]
                # Assign immediately, set due date based on frequency
                if frequency == 'weekly':
                    next_due_date = today + timedelta(days=7)
                # Add logic for other frequencies (daily, monthly) here
                should_assign = True
            else:
                # Task has been assigned before, check if it's time to cycle
                last_member_id = last_assignment['member_id']
                # Use assigned_date as the basis for the next cycle calculation
                last_assigned_date = last_assignment['assigned_date'].date() # Get just the date part

                next_assignment_trigger_date = None
                days_to_add_due = 0

                if frequency == 'weekly':
                    next_assignment_trigger_date = last_assigned_date + timedelta(days=7)
                    days_to_add_due = 7
                # Add logic for 'daily', 'monthly' here

                if next_assignment_trigger_date and today >= next_assignment_trigger_date:
                    logger.info(f"Task '{task_name}' ({task_id}) is due for reassignment (last assigned {last_assigned_date}, trigger {next_assignment_trigger_date}).")
                    should_assign = True
                    # Find the next member in the cycle
                    try:
                        last_index = member_ids.index(last_member_id)
                        next_index = (last_index + 1) % num_members
                        next_member_id = member_ids[next_index]
                        next_due_date = today + timedelta(days=days_to_add_due)
                    except ValueError:
                        # Last assigned member might have been deleted, assign to first member
                        logger.warning(f"Last assignee {last_member_id} for task '{task_name}' not found in current member list. Assigning to first member.")
                        next_member_id = member_ids[0]
                        next_due_date = today + timedelta(days=days_to_add_due)
                else:
                     logger.debug(f"Task '{task_name}' ({task_id}) not yet due for reassignment (last assigned {last_assigned_date}, trigger {next_assignment_trigger_date}).")


            # If we determined an assignment should happen, create it (if not already existing and incomplete)
            if should_assign and next_member_id and next_due_date:
                logger.info(f"Attempting to assign task '{task_name}' to member {next_member_id} with due date {next_due_date}.")
                # Check if an incomplete assignment already exists for this task/member/due_date
                # (Helps prevent duplicates if cron runs unexpectedly close together)
                check_query = """
                    SELECT id FROM assignments
                    WHERE task_id = %s AND member_id = %s AND due_date = %s AND completed = FALSE
                    LIMIT 1;
                """
                existing_incomplete = execute_query(check_query, (task_id, next_member_id, next_due_date), fetch_one=True)

                if existing_incomplete:
                    logger.warning(f"Skipping assignment: Task '{task_name}' already assigned to member {next_member_id} for due date {next_due_date} and is incomplete.")
                else:
                    # Insert the new assignment
                    insert_query = """
                        INSERT INTO assignments (task_id, member_id, assigned_date, due_date)
                        VALUES (%s, %s, CURRENT_TIMESTAMP, %s);
                    """
                    success = execute_query(insert_query, (task_id, next_member_id, next_due_date), commit=True)
                    if success:
                        assignments_created += 1
                        logger.info(f"Successfully assigned task '{task_name}' to member {next_member_id} due {next_due_date}.")
                    else:
                        logger.error(f"Failed to insert assignment for task '{task_name}' to member {next_member_id}.")

        logger.info(f"Task cycling check finished. Created {assignments_created} new assignments.")

    except Exception as e:
        logger.error(f"An error occurred during task cycling: {e}", exc_info=True) # Log traceback

# --- Reminder Sending Logic ---

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
        logger.info(f"Found {len(assignments_due)} assignments due soon for reminders.")

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

        logger.info(f"Reminder sending finished. Sent: {sent_count}, Failed: {failed_count}.")

    except Exception as e:
        # Log error for the overall process
        logger.error(f"An error occurred during the reminder sending process: {e}", exc_info=True)


# --- Run the functions when script is executed ---
if __name__ == "__main__":
    logger.info("Cron job script started.")
    # Run cycling first to create new assignments if needed
    cycle_recurring_tasks()
    # Then send reminders for anything due soon (including newly created ones)
    send_reminders()
    logger.info("Cron job script finished.")