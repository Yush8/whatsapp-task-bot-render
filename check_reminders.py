import os
import logging
from datetime import date, timedelta, datetime
import psycopg2
from psycopg2.extras import DictCursor
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger('TaskCyclerReminderScript_V3')

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
# (Keep the get_db_connection and execute_query functions exactly as they were in the previous version)
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

    if result_data is None:
         return None if fetch_one else []
    if fetch_one:
        return dict(result_data)
    if fetch_all:
        return [dict(row) for row in result_data]
    return True

# --- Task Cycling Logic (Day-Specific Weekly) ---

def cycle_recurring_tasks():
    logger.info("Starting day-specific recurring task assignment check...")
    today = date.today()
    # Python's weekday(): Monday is 0 and Sunday is 6
    today_weekday = today.weekday()
    logger.info(f"Today is {today}, weekday {today_weekday} (0=Mon, 6=Sun).")

    try:
        # Get members ordered by date added for cycle rotation
        members = execute_query("SELECT id FROM members ORDER BY date_added", fetch_all=True)
        if not members:
            logger.info("No members found, skipping task cycling.")
            return
        member_ids = [m['id'] for m in members]
        num_members = len(member_ids)
        logger.info(f"Found {num_members} members for cycling.")

        # Get weekly tasks scheduled specifically for *today's* day of the week
        tasks_due_today_query = """
            SELECT id, name
            FROM tasks
            WHERE frequency = 'weekly' AND recur_day_of_week = %s;
        """
        recurring_tasks_for_today = execute_query(tasks_due_today_query, (today_weekday,), fetch_all=True)

        if not recurring_tasks_for_today:
            logger.info(f"No weekly tasks found scheduled for today (weekday {today_weekday}).")
            return

        logger.info(f"Found {len(recurring_tasks_for_today)} weekly tasks scheduled for today.")
        assignments_created = 0

        for task in recurring_tasks_for_today:
            task_id = task['id']
            task_name = task['name']
            logger.info(f"Processing task '{task_name}' ({task_id}) scheduled for today.")

            # Find the most recent assignment for this task to determine the last assignee
            last_assignment_query = """
                SELECT member_id
                FROM assignments
                WHERE task_id = %s
                ORDER BY assigned_date DESC, id DESC
                LIMIT 1;
            """
            last_assignment = execute_query(last_assignment_query, (task_id,), fetch_one=True)

            next_member_id = None

            if not last_assignment:
                # First time assigning this task
                logger.info(f"Task '{task_name}' has no previous assignment. Assigning to first member.")
                next_member_id = member_ids[0]
            else:
                # Determine next member in the cycle
                last_member_id = last_assignment['member_id']
                try:
                    last_index = member_ids.index(last_member_id)
                    next_index = (last_index + 1) % num_members
                    next_member_id = member_ids[next_index]
                    logger.info(f"Last assignee for '{task_name}' was {last_member_id} (index {last_index}). Next is {next_member_id} (index {next_index}).")
                except ValueError:
                    logger.warning(f"Last assignee {last_member_id} for task '{task_name}' not found in current member list. Assigning to first member.")
                    next_member_id = member_ids[0]

            # Create the assignment for *today* if one doesn't already exist
            if next_member_id:
                logger.info(f"Attempting to assign task '{task_name}' to member {next_member_id} due today ({today}).")
                # Check if an incomplete assignment already exists for this task/member/due_date=today
                check_query = """
                    SELECT id FROM assignments
                    WHERE task_id = %s AND member_id = %s AND due_date = %s AND completed = FALSE
                    LIMIT 1;
                """
                existing_incomplete = execute_query(check_query, (task_id, next_member_id, today), fetch_one=True)

                if existing_incomplete:
                    logger.warning(f"Skipping assignment: Task '{task_name}' already assigned to member {next_member_id} for due date {today} and is incomplete.")
                else:
                    # Insert the new assignment with due_date = today
                    insert_query = """
                        INSERT INTO assignments (task_id, member_id, assigned_date, due_date)
                        VALUES (%s, %s, CURRENT_TIMESTAMP, %s);
                    """
                    success = execute_query(insert_query, (task_id, next_member_id, today), commit=True)
                    if success:
                        assignments_created += 1
                        logger.info(f"Successfully assigned task '{task_name}' to member {next_member_id} due {today}.")
                    else:
                        # execute_query now returns True on commit success without fetch
                        # Failure likely means an exception occurred and was logged
                        logger.error(f"Failed to insert assignment for task '{task_name}' to member {next_member_id} (check previous logs for DB error).")

        logger.info(f"Task cycling check finished. Created {assignments_created} new assignments for today.")

    except Exception as e:
        logger.error(f"An error occurred during task cycling: {e}", exc_info=True)

# --- Reminder Sending Logic (Notify ONLY for tasks due TODAY) ---

def send_reminders():
    if not twilio_client:
        logger.error("Cannot send reminders: Twilio client not initialized.")
        return
    if not DATABASE_URL:
        logger.error("Cannot send reminders: DATABASE_URL not set.")
        return

    logger.info("Starting reminder check for tasks due TODAY...")
    today = date.today()

    # Query ONLY for assignments due TODAY that are not completed
    reminder_query = """
        SELECT m.name as member_name, m.phone as member_phone,
               t.name as task_name, t.description as task_description,
               a.due_date
        FROM assignments a
        JOIN members m ON a.member_id = m.id
        JOIN tasks t ON a.task_id = t.id
        WHERE a.completed = FALSE AND a.due_date = %s;
    """

    try:
        assignments_due_today = execute_query(reminder_query, (today,), fetch_all=True)
        logger.info(f"Found {len(assignments_due_today)} assignments due today for reminders.")

        sent_count = 0
        failed_count = 0
        for assignment in assignments_due_today:
            try:
                # Message clearly states it's due today
                message_body = f"Hi {assignment['member_name']}! Task due TODAY: '{assignment['task_name']}'."
                if assignment['task_description']:
                    message_body += f"\nDetails: {assignment['task_description']}"
                message_body += "\nPlease complete it today."

                # Send WhatsApp message via Twilio
                message = twilio_client.messages.create(
                    body=message_body,
                    from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                    to=f"whatsapp:{assignment['member_phone']}"
                )
                logger.info(f"Sent reminder SID {message.sid} to {assignment['member_name']} for task '{assignment['task_name']}' due today.")
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send reminder to {assignment['member_name']} ({assignment['member_phone']}): {e}")
                failed_count += 1

        logger.info(f"Reminder sending finished for tasks due today. Sent: {sent_count}, Failed: {failed_count}.")

    except Exception as e:
        logger.error(f"An error occurred during the reminder sending process: {e}", exc_info=True)


# --- Run the functions when script is executed ---
if __name__ == "__main__":
    logger.info("Cron job script started (V3 - Day Specific).")
    # Run cycling first to create assignments for tasks due today
    cycle_recurring_tasks()
    # Then send reminders ONLY for tasks due today
    send_reminders()
    logger.info("Cron job script finished.")