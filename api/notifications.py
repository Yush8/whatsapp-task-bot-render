import logging
from flask import Blueprint, jsonify
from datetime import date
import uuid
# Import helpers and Twilio client (needs proper sharing via app context ideally)
import os
import psycopg2
from psycopg2.extras import DictCursor
from twilio.rest import Client as TwilioClient

DATABASE_URL = os.getenv('DATABASE_URL')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')
logger = logging.getLogger(__name__)

# Re-init Twilio client (better: pass from app factory)
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try: twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception as e: logger.error(f"Failed to initialize Twilio client in notifications: {e}")

# --- Re-defined DB Helpers ---
def get_db_connection():
    if not DATABASE_URL: return None
    try: return psycopg2.connect(DATABASE_URL)
    except psycopg2.OperationalError as e: logger.error(f"DB conn error: {e}"); return None

def execute_query(query, params=None, fetch_one=False, fetch_all=False, commit=False):
    # (Same implementation as in members.py)
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
            if fetch_one: result_data = cur.fetchone()
            elif fetch_all: result_data = cur.fetchall()
            if commit: conn.commit()
    except Exception as e: logger.error(f"DB query failed: {query} | PARAMS: {params} | ERROR: {e}"); conn.rollback(); raise
    finally: conn.close()
    if result_data is None: return None if fetch_one else []
    if fetch_one: return dict(result_data)
    if fetch_all: return [dict(row) for row in result_data]
    return True
# --- End Re-defined DB Helpers ---

notifications_bp = Blueprint('notifications_api', __name__, url_prefix='/api/notify')

@notifications_bp.route('/all_chores', methods=['POST'])
def notify_all_chores():
    # (Keep the notify_all_chores logic exactly as it was)
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

@notifications_bp.route('/member/<uuid:member_id>', methods=['POST'])
def notify_member_tasks(member_id):
     # (Keep the notify_member_tasks logic exactly as it was)
    if not twilio_client: return jsonify({"error": "Twilio client not configured"}), 500
    try:
        member = execute_query("SELECT id, name, phone FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
        if not member: return jsonify({"error": "Member not found"}), 404
        today = date.today()
        tasks_query = """SELECT t.name, a.due_date FROM assignments a JOIN tasks t ON a.task_id = t.id
                         WHERE a.member_id = %s AND a.completed = FALSE AND a.due_date >= %s ORDER BY a.due_date;"""
        assignments = execute_query(tasks_query, (member_id, today), fetch_all=True)
        if not assignments: message_body = f"Hi {member['name']}! You currently have no outstanding tasks assigned."
        else:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            tasks_list = [f"- {a['name']} (Due: {days[a['due_date'].weekday()]} {a['due_date'].strftime('%Y-%m-%d')})" for a in assignments]
            tasks_str = "\n".join(tasks_list); message_body = f"Hi {member['name']}! Here are your outstanding tasks:\n{tasks_str}"
        try:
            message = twilio_client.messages.create(body=message_body, from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}", to=f"whatsapp:{member['phone']}")
            logger.info(f"Sent task list notification SID {message.sid} to {member['name']}")
            return jsonify({"status": "success", "message": f"Notification sent to {member['name']}."}), 200
        except Exception as e: logger.error(f"Failed to send task list to {member['name']} ({member['phone']}): {e}"); return jsonify({"error": "Failed to send Twilio message"}), 500
    except Exception as e: logger.error(f"Error during notify member {member_id}: {e}"); return jsonify({"error": "Server error sending notification"}), 500