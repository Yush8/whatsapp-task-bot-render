import logging
from flask import Blueprint, request, jsonify
# Import helpers (same assumption/issue as in members.py - needs shared DB logic)
import os
import psycopg2
from psycopg2.extras import DictCursor
import uuid

DATABASE_URL = os.getenv('DATABASE_URL')
logger = logging.getLogger(__name__)

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

tasks_bp = Blueprint('tasks_api', __name__, url_prefix='/api/tasks')

@tasks_bp.route('', methods=['GET'])
def get_tasks():
    try:
        tasks = execute_query("SELECT id, name, description, frequency, recur_day_of_week, date_added FROM tasks ORDER BY name", fetch_all=True)
        for task in tasks: task['recur_day_of_week'] = task.get('recur_day_of_week')
        return jsonify(tasks)
    except Exception as e: logger.error(f"Error getting tasks: {e}"); return jsonify({"error": "Server error retrieving tasks"}), 500

@tasks_bp.route('/<uuid:task_id>', methods=['GET'])
def get_task(task_id):
    try:
        task = execute_query("SELECT id, name, description, frequency, recur_day_of_week, date_added FROM tasks WHERE id = %s", (str(task_id),), fetch_one=True)
        if task:
            task['recur_day_of_week'] = task.get('recur_day_of_week')
            return jsonify(task)
        else:
            return jsonify({"error": "Task not found"}), 404
    except Exception as e: logger.error(f"Error getting task {task_id}: {e}"); return jsonify({"error": "Server error retrieving task"}), 500

@tasks_bp.route('', methods=['POST'])
def add_task():
    # (Keep the add_task validation logic exactly as it was in previous app.py)
    data = request.json
    if not data or 'name' not in data: return jsonify({"error": "Task name required"}), 400
    name = data['name']; description = data.get('description'); frequency = data.get('frequency', 'manual'); recur_day_of_week = data.get('recur_day_of_week')
    valid_frequencies = ['daily', 'weekly', 'monthly', 'manual'] # Added daily
    if frequency not in valid_frequencies: return jsonify({"error": f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"}), 400
    if frequency == 'weekly':
        if recur_day_of_week is None: return jsonify({"error": "recur_day_of_week (0-6) required for weekly frequency"}), 400
        try: day_num = int(recur_day_of_week); assert 0 <= day_num <= 6; recur_day_of_week = day_num
        except (ValueError, TypeError, AssertionError): return jsonify({"error": "Invalid recur_day_of_week. Must be integer 0-6."}), 400
    # Add validation for monthly/daily if needed (e.g., monthly requires day of month?) For now, only weekly requires day.
    elif frequency in ['daily', 'monthly']:
         recur_day_of_week = None # Can't set day_of_week for daily/monthly yet
    else: recur_day_of_week = None
    try:
        existing_task = execute_query("SELECT id FROM tasks WHERE name = %s", (name,), fetch_one=True)
        if existing_task: return jsonify({"error": "Task name already exists", "task_id": existing_task['id']}), 409
        query = "INSERT INTO tasks (name, description, frequency, recur_day_of_week) VALUES (%s, %s, %s, %s) RETURNING id, name, description, frequency, recur_day_of_week, date_added;"
        new_task = execute_query(query, (name, description, frequency, recur_day_of_week), fetch_one=True, commit=True)
        if new_task: new_task['recur_day_of_week'] = new_task.get('recur_day_of_week'); return jsonify({"status": "success", "task": new_task}), 201
        else: logger.error(f"Failed to insert task. Data: {data}"); return jsonify({"error": "Failed to add task"}), 500
    except Exception as e: logger.error(f"Error adding task: {e}"); return jsonify({"error": "Server error adding task"}), 500


@tasks_bp.route('/<uuid:task_id>', methods=['PUT'])
def update_task(task_id):
    # (Keep the update_task validation logic exactly as it was in previous app.py)
    data = request.json
    if not data: return jsonify({"error": "No update data provided"}), 400
    try:
        existing_task = execute_query("SELECT * FROM tasks WHERE id = %s", (str(task_id),), fetch_one=True)
        if not existing_task: return jsonify({"error": "Task not found"}), 404
    except Exception as e: logger.error(f"Error checking task {task_id}: {e}"); return jsonify({"error": "Server error checking task"}), 500
    updates = []; params = []
    if 'name' in data and data['name'] != existing_task['name']:
        name = data['name']
        check_name = execute_query("SELECT id FROM tasks WHERE name = %s AND id != %s", (name, str(task_id)), fetch_one=True)
        if check_name: return jsonify({"error": f"Task name '{name}' already exists."}), 409
        updates.append("name = %s"); params.append(name)
    if 'description' in data: updates.append("description = %s"); params.append(data.get('description'))
    frequency = data.get('frequency', existing_task['frequency'])
    recur_day_of_week = data.get('recur_day_of_week', existing_task['recur_day_of_week'])
    valid_frequencies = ['daily', 'weekly', 'monthly', 'manual']; # Added daily
    if frequency not in valid_frequencies: return jsonify({"error": f"Invalid frequency. Must be one of: {', '.join(valid_frequencies)}"}), 400
    if frequency == 'weekly':
        if recur_day_of_week is None:
             if existing_task['recur_day_of_week'] is not None and 'recur_day_of_week' not in data: recur_day_of_week = existing_task['recur_day_of_week']
             else: return jsonify({"error": "recur_day_of_week (0-6) required when setting frequency to weekly"}), 400
        try: day_num = int(recur_day_of_week); assert 0 <= day_num <= 6; recur_day_of_week = day_num
        except (ValueError, TypeError, AssertionError): return jsonify({"error": "Invalid recur_day_of_week. Must be integer 0-6."}), 400
    elif frequency in ['daily', 'monthly']: recur_day_of_week = None # Enforce null for now
    else: recur_day_of_week = None
    if frequency != existing_task['frequency']: updates.append("frequency = %s"); params.append(frequency)
    if recur_day_of_week != existing_task['recur_day_of_week'] or frequency != existing_task['frequency']: updates.append("recur_day_of_week = %s"); params.append(recur_day_of_week)
    if not updates: return jsonify({"message": "No changes detected to update"}), 200
    params.append(str(task_id))
    query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = %s RETURNING id, name, description, frequency, recur_day_of_week, date_added;"
    try:
        updated_task = execute_query(query, tuple(params), fetch_one=True, commit=True)
        if updated_task: updated_task['recur_day_of_week'] = updated_task.get('recur_day_of_week'); return jsonify({"status": "success", "task": updated_task}), 200
        else: logger.error(f"Failed to update task {task_id}."); return jsonify({"error": "Failed to update task"}), 500
    except Exception as e: logger.error(f"Error updating task {task_id}: {e}"); return jsonify({"error": "Server error updating task"}), 500

@tasks_bp.route('/<uuid:task_id>', methods=['DELETE'])
def delete_task(task_id):
    # (Keep the delete_task logic exactly as it was)
    logger.info(f"Attempting to delete task with ID: {task_id}")
    try:
        task = execute_query("SELECT id, name FROM tasks WHERE id = %s", (str(task_id),), fetch_one=True)
        if not task: return jsonify({"error": "Task not found"}), 404
        query = "DELETE FROM tasks WHERE id = %s;"
        execute_query(query, (str(task_id),), commit=True) # CASCADE deletes assignments
        logger.info(f"Successfully deleted task '{task.get('name', 'N/A')}' with ID: {task_id}")
        return jsonify({"status": "success", "message": f"Task '{task.get('name', 'N/A')}' deleted."}), 200
    except Exception as e: logger.error(f"Error deleting task {task_id}: {e}"); return jsonify({"error": "Server error deleting task"}), 500