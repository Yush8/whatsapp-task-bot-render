import logging
from flask import Blueprint, request, jsonify
from datetime import date
import uuid
# Import helpers (same assumption/issue as in members.py - needs shared DB logic)
import os
import psycopg2
from psycopg2.extras import DictCursor

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

# Separate Blueprint for assignments
assignments_bp = Blueprint('assignments_api', __name__) # No prefix needed if mounting later

# Mounted at /api/assign for consistency with previous version
@assignments_bp.route('/assign', methods=['POST'])
def assign_task():
    # (Keep the assign_task logic exactly as it was)
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

# Routes mounted at /api/assignments
@assignments_bp.route('/assignments', methods=['GET'])
def get_assignments():
    # (Keep the get_assignments logic exactly as it was)
    query = """
        SELECT a.id, a.assigned_date, a.due_date, a.completed, a.completion_date,
               m.id as member_id, m.name as member_name,
               t.id as task_id, t.name as task_name
        FROM assignments a JOIN members m ON a.member_id = m.id JOIN tasks t ON a.task_id = t.id
        ORDER BY a.due_date DESC, m.name, t.name;"""
    try:
        assignments = execute_query(query, fetch_all=True)
        return jsonify(assignments)
    except Exception as e: logger.error(f"Error getting assignments: {e}"); return jsonify({"error": "Server error retrieving assignments"}), 500

@assignments_bp.route('/assignments/<uuid:assignment_id>', methods=['GET'])
def get_assignment(assignment_id):
    # (Keep the get_assignment logic exactly as it was)
    query = """
        SELECT a.id, a.assigned_date, a.due_date, a.completed, a.completion_date,
               m.id as member_id, m.name as member_name,
               t.id as task_id, t.name as task_name, t.description as task_description
        FROM assignments a JOIN members m ON a.member_id = m.id JOIN tasks t ON a.task_id = t.id
        WHERE a.id = %s;"""
    try:
        assignment = execute_query(query, (str(assignment_id),), fetch_one=True)
        if assignment: return jsonify(assignment)
        else: return jsonify({"error": "Assignment not found"}), 404
    except Exception as e: logger.error(f"Error getting assignment {assignment_id}: {e}"); return jsonify({"error": "Server error retrieving assignment"}), 500

@assignments_bp.route('/assignments/<uuid:assignment_id>', methods=['PUT'])
def update_assignment(assignment_id):
    # (Keep the update_assignment logic exactly as it was)
    data = request.json
    if not data or ('due_date' not in data and 'completed' not in data): return jsonify({"error": "No update data provided (need due_date and/or completed)"}), 400
    updates = []; params = []
    if 'due_date' in data:
        try: due_date = date.fromisoformat(data['due_date']); updates.append("due_date = %s"); params.append(due_date)
        except ValueError: return jsonify({"error": "Invalid due_date format (YYYY-MM-DD)"}), 400
    if 'completed' in data:
        completed_status = data['completed']
        if not isinstance(completed_status, bool): return jsonify({"error": "Invalid completed status (must be true or false)"}), 400
        updates.append("completed = %s"); params.append(completed_status)
        if completed_status: updates.append("completion_date = CURRENT_TIMESTAMP")
        else: updates.append("completion_date = NULL")
    if not updates: return jsonify({"message": "No valid fields to update"}), 200
    params.append(str(assignment_id))
    query = f"UPDATE assignments SET {', '.join(updates)} WHERE id = %s RETURNING id;"
    try:
        result = execute_query(query, tuple(params), fetch_one=True, commit=True)
        if result:
            # Use internal call logic - needs Flask app context or refactor get_assignment
            # For simplicity, just return success here, client can re-fetch if needed
            return jsonify({"status": "success", "assignment_id": result['id']}), 200
            # full_assignment = get_assignment(assignment_id).get_json() # This call needs context
            # return jsonify({"status": "success", "assignment": full_assignment}), 200
        else:
            check_exists = execute_query("SELECT id FROM assignments WHERE id = %s", (str(assignment_id),), fetch_one=True)
            if not check_exists: return jsonify({"error": "Assignment not found"}), 404
            else: logger.error(f"Failed to update assignment {assignment_id}."); return jsonify({"error": "Failed to update assignment"}), 500
    except Exception as e: logger.error(f"Error updating assignment {assignment_id}: {e}"); return jsonify({"error": "Server error updating assignment"}), 500


@assignments_bp.route('/assignments/<uuid:assignment_id>/complete', methods=['POST'])
def complete_assignment_api(assignment_id):
    # (Keep the complete_assignment_api logic exactly as it was)
    logger.info(f"API request to complete assignment {assignment_id}")
    try:
        check_query = "SELECT id FROM assignments WHERE id = %s AND completed = FALSE;"
        assignment = execute_query(check_query, (str(assignment_id),), fetch_one=True)
        if not assignment:
            already_done = execute_query("SELECT id FROM assignments WHERE id = %s AND completed = TRUE;", (str(assignment_id),), fetch_one=True)
            if already_done: return jsonify({"message": "Assignment already marked as complete"}), 200
            else: return jsonify({"error": "Assignment not found or already complete"}), 404
        update_query = "UPDATE assignments SET completed = TRUE, completion_date = CURRENT_TIMESTAMP WHERE id = %s RETURNING id;"
        result = execute_query(update_query, (str(assignment_id),), fetch_one=True, commit=True)
        if result:
            logger.info(f"Marked assignment {assignment_id} complete via API.")
            # Simplification: Return success, client can re-fetch if needed
            return jsonify({"status": "success", "assignment_id": result['id']}), 200
            # full_assignment = get_assignment(assignment_id).get_json()
            # return jsonify({"status": "success", "assignment": full_assignment}), 200
        else: logger.error(f"Failed to mark assignment {assignment_id} complete."); return jsonify({"error": "Failed to mark assignment complete"}), 500
    except Exception as e: logger.error(f"Error completing assignment {assignment_id} via API: {e}"); return jsonify({"error": "Server error completing assignment"}), 500

@assignments_bp.route('/assignments/<uuid:assignment_id>', methods=['DELETE'])
def delete_assignment(assignment_id):
    # (Keep the delete_assignment logic exactly as it was)
    logger.info(f"Attempting to delete assignment with ID: {assignment_id}")
    try:
        assignment = execute_query("SELECT id FROM assignments WHERE id = %s", (str(assignment_id),), fetch_one=True)
        if not assignment: return jsonify({"error": "Assignment not found"}), 404
        query = "DELETE FROM assignments WHERE id = %s;"
        execute_query(query, (str(assignment_id),), commit=True)
        logger.info(f"Successfully deleted assignment with ID: {assignment_id}")
        return jsonify({"status": "success", "message": "Assignment deleted successfully."}), 200
    except Exception as e: logger.error(f"Error deleting assignment {assignment_id}: {e}"); return jsonify({"error": "Server error deleting assignment"}), 500