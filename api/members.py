import logging
import uuid
from datetime import date
from flask import Blueprint, request, jsonify
# Import helper from main app (adjust path if needed, depends on how app is run)
# Assuming execute_query and get_db_connection are accessible via app context or direct import
# For simplicity here, we'll assume they are imported or passed somehow.
# A better way involves Flask application factories and context.
# Let's redefine execute_query here for clarity, assuming DATABASE_URL is accessible.
# Ideally, share DB logic via a dedicated module or app context.

import os
import psycopg2
from psycopg2.extras import DictCursor

DATABASE_URL = os.getenv('DATABASE_URL') # Need access to this
logger = logging.getLogger(__name__)

# --- Re-defined DB Helpers (Ideally share from app.py/database.py) ---
def get_db_connection():
    if not DATABASE_URL: return None
    try: return psycopg2.connect(DATABASE_URL)
    except psycopg2.OperationalError as e: logger.error(f"DB conn error: {e}"); return None

def execute_query(query, params=None, fetch_one=False, fetch_all=False, commit=False):
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


# Create Blueprint
members_bp = Blueprint('members_api', __name__, url_prefix='/api/members')

@members_bp.route('', methods=['GET'])
def get_members():
    try:
        members = execute_query("SELECT id, name, phone, date_added, completed_count, skipped_count, missed_count, is_away, away_until FROM members ORDER BY date_added", fetch_all=True)
        return jsonify(members)
    except Exception as e:
        logger.error(f"Error getting members: {e}")
        return jsonify({"error": "Server error retrieving members"}), 500

@members_bp.route('/<uuid:member_id>', methods=['GET'])
def get_member(member_id):
    try:
        member = execute_query("SELECT id, name, phone, date_added, completed_count, skipped_count, missed_count, is_away, away_until FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
        if member:
            return jsonify(member)
        else:
            return jsonify({"error": "Member not found"}), 404
    except Exception as e:
        logger.error(f"Error getting member {member_id}: {e}")
        return jsonify({"error": "Server error retrieving member"}), 500

@members_bp.route('', methods=['POST'])
def add_member():
    data = request.json
    if not data or 'name' not in data or 'phone' not in data: return jsonify({"error": "Name and phone required"}), 400
    phone = data['phone']
    if not phone.startswith('+'): return jsonify({"error": "Phone number must start with +"}), 400
    try:
        existing_member = execute_query("SELECT id FROM members WHERE phone = %s", (phone,), fetch_one=True)
        if existing_member: return jsonify({"error": "Phone number already exists", "member_id": existing_member['id']}), 409
        # Note: Counters, is_away, away_until use DB defaults
        query = "INSERT INTO members (name, phone) VALUES (%s, %s) RETURNING id, name, phone, date_added, completed_count, skipped_count, missed_count, is_away, away_until;"
        new_member = execute_query(query, (data['name'], phone), fetch_one=True, commit=True)
        if new_member: return jsonify({"status": "success", "member": new_member}), 201
        else: logger.error(f"Failed to insert member. Data: {data}"); return jsonify({"error": "Failed to add member"}), 500
    except Exception as e: logger.error(f"Error adding member: {e}"); return jsonify({"error": "Server error adding member"}), 500

@members_bp.route('/<uuid:member_id>', methods=['PUT'])
def update_member(member_id):
    data = request.json
    if not data or ('name' not in data and 'phone' not in data):
        return jsonify({"error": "No update data provided (need name and/or phone)"}), 400
    updates = []; params = []
    if 'name' in data: updates.append("name = %s"); params.append(data['name'])
    if 'phone' in data:
        phone = data['phone']
        if not phone.startswith('+'): return jsonify({"error": "Phone number must start with +"}), 400
        existing_phone = execute_query("SELECT id FROM members WHERE phone = %s AND id != %s", (phone, str(member_id)), fetch_one=True)
        if existing_phone: return jsonify({"error": f"Phone number {phone} is already in use by another member."}), 409
        updates.append("phone = %s"); params.append(phone)
    if not updates: return jsonify({"error": "No valid fields to update"}), 400
    params.append(str(member_id))
    query = f"UPDATE members SET {', '.join(updates)} WHERE id = %s RETURNING id, name, phone, date_added, completed_count, skipped_count, missed_count, is_away, away_until;"
    try:
        updated_member = execute_query(query, tuple(params), fetch_one=True, commit=True)
        if updated_member: return jsonify({"status": "success", "member": updated_member}), 200
        else:
            check_exists = execute_query("SELECT id FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
            if not check_exists: return jsonify({"error": "Member not found"}), 404
            else: logger.error(f"Failed to update member {member_id}."); return jsonify({"error": "Failed to update member"}), 500
    except Exception as e: logger.error(f"Error updating member {member_id}: {e}"); return jsonify({"error": "Server error updating member"}), 500

@members_bp.route('/<uuid:member_id>', methods=['DELETE'])
def delete_member(member_id):
    logger.info(f"Attempting to delete member with ID: {member_id}")
    try:
        member = execute_query("SELECT id, name FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
        if not member: return jsonify({"error": "Member not found"}), 404
        query = "DELETE FROM members WHERE id = %s;"
        execute_query(query, (str(member_id),), commit=True) # CASCADE deletes assignments
        logger.info(f"Successfully deleted member '{member.get('name', 'N/A')}' with ID: {member_id}")
        return jsonify({"status": "success", "message": f"Member '{member.get('name', 'N/A')}' deleted."}), 200
    except Exception as e: logger.error(f"Error deleting member {member_id}: {e}"); return jsonify({"error": "Server error deleting member"}), 500

# --- Availability Endpoints ---
@members_bp.route('/<uuid:member_id>/away', methods=['POST'])
def set_member_away(member_id):
    data = request.json
    if not data or 'away_until' not in data:
        return jsonify({"error": "Missing required field: away_until (YYYY-MM-DD)"}), 400
    try:
        away_until_date = date.fromisoformat(data['away_until'])
        if away_until_date <= date.today():
             return jsonify({"error": "away_until date must be in the future"}), 400
    except ValueError:
        return jsonify({"error": "Invalid away_until date format (must be YYYY-MM-DD)"}), 400

    try:
        query = "UPDATE members SET is_away = TRUE, away_until = %s WHERE id = %s RETURNING id;"
        result = execute_query(query, (away_until_date, str(member_id)), fetch_one=True, commit=True)
        if result:
            logger.info(f"Member {member_id} marked as away until {away_until_date}")
            return jsonify({"status": "success", "message": f"Member marked as away until {away_until_date}."}), 200
        else:
            return jsonify({"error": "Member not found"}), 404
    except Exception as e:
        logger.error(f"Error setting member {member_id} away: {e}")
        return jsonify({"error": "Server error setting away status"}), 500

@members_bp.route('/<uuid:member_id>/back', methods=['POST'])
def set_member_back(member_id):
    try:
        query = "UPDATE members SET is_away = FALSE, away_until = NULL WHERE id = %s RETURNING id;"
        result = execute_query(query, (str(member_id),), fetch_one=True, commit=True)
        if result:
            logger.info(f"Member {member_id} marked as back/available.")
            return jsonify({"status": "success", "message": "Member marked as available."}), 200
        else:
            # Check if member exists but maybe was already back
            check_exists = execute_query("SELECT id FROM members WHERE id = %s", (str(member_id),), fetch_one=True)
            if not check_exists:
                 return jsonify({"error": "Member not found"}), 404
            else: # Member exists, likely already marked as back
                 return jsonify({"status": "success", "message": "Member already marked as available."}), 200
    except Exception as e:
        logger.error(f"Error setting member {member_id} back: {e}")
        return jsonify({"error": "Server error setting available status"}), 500
