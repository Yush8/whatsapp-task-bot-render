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
logger = logging.getLogger('TaskSchedulerScript_V4')

DATABASE_URL = os.getenv('DATABASE_URL')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')

# Initialize Twilio client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER:
    try: twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN); logger.info("Twilio client initialized.")
    except Exception as e: logger.error(f"Failed to init Twilio client: {e}")
else: logger.error("Twilio credentials missing.")

# --- Database Helper Functions ---
# (Keep the get_db_connection and execute_query functions exactly as they were)
def get_db_connection():
    if not DATABASE_URL: logger.error("DATABASE_URL not set."); return None
    try: return psycopg2.connect(DATABASE_URL)
    except psycopg2.OperationalError as e: logger.error(f"DB conn error: {e}"); return None

def execute_query(query, params=None, fetch_one=False, fetch_all=False, commit=False):
    conn = get_db_connection()
    if conn is None:
        if fetch_one: return None
        if fetch_all: return []
        if commit: raise ConnectionError("DB connection failed")
        return None
    result_data = None; rowcount = 0
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(query, params)
            rowcount = cur.rowcount # Get affected rows for UPDATE/DELETE if needed
            if fetch_one: result_data = cur.fetchone()
            elif fetch_all: result_data = cur.fetchall()
            if commit: conn.commit()
    except Exception as e: logger.error(f"DB query failed: {query} | PARAMS: {params} | ERROR: {e}"); conn.rollback(); raise
    finally: conn.close()
    if fetch_one: return dict(result_data) if result_data else None
    if fetch_all: return [dict(row) for row in result_data] if result_data else []
    if commit: return rowcount # Return affected rows for commit
    return None # Should not happen unless commit=False and no fetch

# --- Availability Check ---
def reset_availability():
    logger.info("Checking for members whose 'away' status should expire...")
    today = date.today()
    query = "UPDATE members SET is_away = FALSE, away_until = NULL WHERE is_away = TRUE AND away_until < %s;"
    try:
        affected_rows = execute_query(query, (today,), commit=True)
        if affected_rows is not None and affected_rows > 0:
             logger.info(f"Marked {affected_rows} member(s) as available.")
        else:
             logger.info("No members needed availability reset.")
    except Exception as e:
        logger.error(f"Error resetting member availability: {e}")


# --- Missed Task Check ---
def check_missed_tasks():
    logger.info("Checking for missed tasks from yesterday...")
    yesterday = date.today() - timedelta(days=1)
    missed_query = """
        SELECT id, member_id
        FROM assignments
        WHERE completed = FALSE AND due_date = %s;
    """
    # Need to update members table, requires careful handling if run multiple times
    # Let's just log for now, incrementing needs idempotency check or flag
    # Alternative: Count assignments directly on member profile API?

    # Correct Approach: Find assignments due yesterday, not complete, AND member is NOT away today
    # Then increment counter for those members. Use a transaction.
    conn = get_db_connection()
    if not conn: logger.error("Cannot check missed tasks: DB connection failed."); return
    updated_members = set()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Find incomplete assignments from yesterday for currently available members
            cur.execute("""
                SELECT a.member_id
                FROM assignments a JOIN members m ON a.member_id = m.id
                WHERE a.completed = FALSE
                  AND a.due_date = %s
                  AND (m.is_away = FALSE OR m.away_until < %s);
            """, (yesterday, date.today()))
            missed_assignments = cur.fetchall()

            if not missed_assignments:
                logger.info("No incomplete tasks found from yesterday for available members.")
                return

            logger.info(f"Found {len(missed_assignments)} potentially missed tasks from yesterday.")
            for assign in missed_assignments:
                member_id = assign['member_id']
                # Increment count for each unique member with a missed task yesterday
                if member_id not in updated_members:
                     cur.execute("UPDATE members SET missed_count = missed_count + 1 WHERE id = %s", (member_id,))
                     logger.info(f"Incremented missed_count for member {member_id}")
                     updated_members.add(member_id)

        conn.commit()
        logger.info(f"Finished checking missed tasks. Updated {len(updated_members)} members.")

    except Exception as e:
        logger.error(f"Error checking/updating missed tasks: {e}")
        conn.rollback()
    finally:
        conn.close()


# --- Task Cycling Logic ---
def cycle_recurring_tasks():
    logger.info("Starting recurring task assignment check (daily & weekly)...")
    today = date.today()
    today_weekday = today.weekday() # 0=Mon, 6=Sun

    try:
        # Get ONLY available members for the cycle
        members_query = """
            SELECT id FROM members
            WHERE (is_away = FALSE OR away_until < %s)
            ORDER BY date_added
        """
        available_members = execute_query(members_query, (today,), fetch_all=True)

        if not available_members:
            logger.info("No available members found, skipping task cycling.")
            return
        member_ids = [m['id'] for m in available_members]
        num_members = len(member_ids)
        logger.info(f"Found {num_members} available members for cycling.")

        # Get tasks recurring today (daily OR weekly on the correct day)
        recurring_tasks_query = """
            SELECT id, name, frequency
            FROM tasks
            WHERE (frequency = 'daily')
               OR (frequency = 'weekly' AND recur_day_of_week = %s);
        """
        recurring_tasks_for_today = execute_query(recurring_tasks_query, (today_weekday,), fetch_all=True)

        if not recurring_tasks_for_today:
            logger.info(f"No daily tasks or weekly tasks scheduled for today (weekday {today_weekday}).")
            return

        logger.info(f"Found {len(recurring_tasks_for_today)} recurring tasks to process today.")
        assignments_created = 0

        for task in recurring_tasks_for_today:
            task_id = task['id']; task_name = task['name']; frequency = task['frequency']
            logger.info(f"Processing task '{task_name}' ({task_id}), frequency '{frequency}'.")

            last_assignment_query = "SELECT member_id FROM assignments WHERE task_id = %s ORDER BY assigned_date DESC, id DESC LIMIT 1;"
            last_assignment = execute_query(last_assignment_query, (task_id,), fetch_one=True)

            next_member_id = None
            if not last_assignment:
                logger.info(f"Task '{task_name}' has no previous assignment. Assigning to first available member.")
                next_member_id = member_ids[0]
            else:
                last_member_id = last_assignment['member_id']
                # Find index in the *available* members list
                try:
                    # Check if last assignee is currently available
                    if last_member_id in member_ids:
                        last_index = member_ids.index(last_member_id)
                        next_index = (last_index + 1) % num_members
                        next_member_id = member_ids[next_index]
                        logger.info(f"Last available assignee for '{task_name}' was {last_member_id} (index {last_index}). Next available is {next_member_id} (index {next_index}).")
                    else:
                        # Last assignee is currently away, find who they *would* have passed to
                        # Find last assignee in the *full* member list to determine their position
                        all_members_ordered = execute_query("SELECT id FROM members ORDER BY date_added", fetch_all=True)
                        all_member_ids = [m['id'] for m in all_members_ordered]
                        try:
                            last_absolute_index = all_member_ids.index(last_member_id)
                            # Find the next person in absolute order, then check if *they* are available
                            # Keep checking subsequent people until an available one is found
                            found_next_available = False
                            for i in range(1, len(all_member_ids) + 1):
                                 check_index = (last_absolute_index + i) % len(all_member_ids)
                                 potential_next_id = all_member_ids[check_index]
                                 if potential_next_id in member_ids: # Check if this person is in the available list
                                     next_member_id = potential_next_id
                                     logger.info(f"Last assignee {last_member_id} is away. Found next available member {next_member_id}.")
                                     found_next_available = True
                                     break
                            if not found_next_available:
                                 logger.warning(f"Could not find any available member to assign '{task_name}' to after unavailable member {last_member_id}. Skipping.")
                                 continue # Skip assignment for this task
                        except ValueError:
                            logger.warning(f"Last assignee {last_member_id} for task '{task_name}' not found in full member list? Assigning to first available member.")
                            next_member_id = member_ids[0]
                except ValueError: # Should not happen if last_assignment has valid member_id
                     logger.warning(f"Error finding index for last assignee {last_member_id}. Assigning to first available member.")
                     next_member_id = member_ids[0]


            # Create the assignment for *today* if we found a next member
            if next_member_id:
                logger.info(f"Attempting assignment: Task '{task_name}' to member {next_member_id} due today ({today}).")
                check_query = "SELECT id FROM assignments WHERE task_id = %s AND member_id = %s AND due_date = %s AND completed = FALSE LIMIT 1;"
                existing_incomplete = execute_query(check_query, (task_id, next_member_id, today), fetch_one=True)

                if existing_incomplete:
                    logger.warning(f"Skipping assignment: Task '{task_name}' already assigned to member {next_member_id} for due date {today} and is incomplete.")
                else:
                    insert_query = "INSERT INTO assignments (task_id, member_id, assigned_date, due_date) VALUES (%s, %s, CURRENT_TIMESTAMP, %s);"
                    try:
                        success = execute_query(insert_query, (task_id, next_member_id, today), commit=True)
                        if success: assignments_created += 1; logger.info(f"Successfully assigned task '{task_name}' to member {next_member_id} due {today}.")
                        # else: logger.error(f"Failed to insert assignment for task '{task_name}'. DB issue likely logged.") # execute_query logs errors
                    except Exception as insert_err:
                         logger.error(f"Failed to insert assignment for task '{task_name}' due to exception: {insert_err}")


        logger.info(f"Task cycling check finished. Created {assignments_created} new assignments for today.")

    except Exception as e:
        logger.error(f"An error occurred during task cycling: {e}", exc_info=True)

# --- Reminder Sending Logic ---
def send_reminders():
    if not twilio_client: logger.error("Cannot send reminders: Twilio client not init."); return
    if not DATABASE_URL: logger.error("Cannot send reminders: DATABASE_URL not set."); return

    logger.info("Starting reminder check for tasks due TODAY...")
    today = date.today()

    # Query ONLY for assignments due TODAY that are not completed AND assigned to an AVAILABLE member
    reminder_query = """
        SELECT m.name as member_name, m.phone as member_phone,
               t.name as task_name, t.description as task_description,
               a.due_date
        FROM assignments a
        JOIN members m ON a.member_id = m.id
        JOIN tasks t ON a.task_id = t.id
        WHERE a.completed = FALSE
          AND a.due_date = %s
          AND (m.is_away = FALSE OR m.away_until < %s); -- Only notify available members
    """
    try:
        assignments_due_today = execute_query(reminder_query, (today, today), fetch_all=True)
        logger.info(f"Found {len(assignments_due_today)} assignments due today for reminders to available members.")
        sent_count = 0; failed_count = 0
        for assignment in assignments_due_today:
            try:
                message_body = f"Hi {assignment['member_name']}! Task due TODAY: '{assignment['task_name']}'."
                if assignment['task_description']: message_body += f"\nDetails: {assignment['task_description']}"
                message_body += "\nPlease complete it today."
                message = twilio_client.messages.create(body=message_body, from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}", to=f"whatsapp:{assignment['member_phone']}")
                logger.info(f"Sent reminder SID {message.sid} to {assignment['member_name']} for task '{assignment['task_name']}' due today."); sent_count += 1
            except Exception as e: logger.error(f"Failed to send reminder to {assignment['member_name']} ({assignment['phone']}): {e}"); failed_count += 1
        logger.info(f"Reminder sending finished. Sent: {sent_count}, Failed: {failed_count}.")
    except Exception as e: logger.error(f"An error occurred during reminder sending: {e}", exc_info=True)

# --- Run the functions when script is executed ---
if __name__ == "__main__":
    logger.info("Cron job script started (V4 - Stats/Away/Daily).")
    # Reset availability first
    reset_availability()
    # Check for tasks missed yesterday
    check_missed_tasks()
    # Assign tasks due today
    cycle_recurring_tasks()
    # Send reminders for tasks due today to available members
    send_reminders()
    logger.info("Cron job script finished.")