"""
Tenant Admin blueprint — administrative oversight scoped to a single tenant.

Mirrors the superadmin blueprint but all data is filtered to the caller's
tenant_id.  A superadmin can also use these endpoints by passing
?tenant_id=<uuid> (or a JSON body field) to inspect any tenant.

Routes:
  GET    /api/tenant-admin/overview
  GET    /api/tenant-admin/analytics
  GET    /api/tenant-admin/users
  PATCH  /api/tenant-admin/users/<user_id>/status
  PATCH  /api/tenant-admin/users/<user_id>/role
  POST   /api/tenant-admin/users/<user_id>/force-reset
  GET    /api/tenant-admin/users/<user_id>/export
  DELETE /api/tenant-admin/users/<user_id>
  POST   /api/tenant-admin/users/<user_id>/impersonate
  GET    /api/tenant-admin/pending-approvals
  PATCH  /api/tenant-admin/users/<user_id>/approve
  PATCH  /api/tenant-admin/users/<user_id>/deny
  GET    /api/tenant-admin/classes
  PATCH  /api/tenant-admin/classes/<class_id>/transfer
  DELETE /api/tenant-admin/classes/<class_id>
  GET    /api/tenant-admin/audit
"""

import logging
from flask import Blueprint, jsonify, request, g

from core.auth import require_auth, require_tenant_admin, create_jwt_token
from core.audit import audit, audit_commit
from core.database import get_db_connection
from core.extensions import csrf_exempt, rate_limit


logger = logging.getLogger(__name__)
bp = Blueprint("tenant_admin", __name__)


# ---------------------------------------------------------------------------
# Tenant scoping helper
# ---------------------------------------------------------------------------


def _scoped_tenant_id():
    """
    Return the effective tenant_id for the current request.

    - tenant_admin: always g.tenant_id (their own tenant, cannot be overridden)
    - superadmin:   reads ?tenant_id= from query string or JSON body
    """
    if g.user_role == "tenant_admin":
        return g.tenant_id
    # superadmin browsing as a specific tenant
    tid = request.args.get("tenant_id")
    if not tid:
        json_body = request.get_json(silent=True) or {}
        tid = json_body.get("tenant_id")
    return tid


def _assert_user_in_tenant(conn, user_id, tenant_id):
    """
    Return (user_id, name, email, role) if the user belongs to tenant_id,
    or None if not found / wrong tenant.  Used before any mutation.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, name, email, COALESCE(role, 'student') FROM users WHERE user_id = %s AND tenant_id = %s",
        (user_id, tenant_id),
    )
    row = cur.fetchone()
    cur.close()
    return row


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@bp.route("/api/tenant-admin/overview", methods=["GET"])
@require_auth
@require_tenant_admin
def get_overview():
    """High-level stats for the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                COUNT(*) AS total_users,
                COUNT(*) FILTER (WHERE account_status = 'active') AS active_users,
                COUNT(*) FILTER (WHERE account_status = 'pending_approval') AS pending_approvals
            FROM users
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        user_row = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(DISTINCT c.class_id)
            FROM classes c
            JOIN users u ON u.user_id = c.professor_id
            WHERE u.tenant_id = %s
            """,
            (tenant_id,),
        )
        class_count = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM tenant_sso_providers WHERE tenant_id = %s AND is_enabled = true",
            (tenant_id,),
        )
        sso_count = cur.fetchone()[0]

        # Active users in last 30 days (via audit_log)
        cur.execute(
            """
            SELECT COUNT(DISTINCT al.user_id)
            FROM audit_log al
            JOIN users u ON u.user_id::text = al.user_id
            WHERE u.tenant_id = %s
              AND al.created_at >= NOW() - INTERVAL '30 days'
            """,
            (tenant_id,),
        )
        active_30d = cur.fetchone()[0]

        cur.close()
        return (
            jsonify(
                {
                    "total_users": user_row[0] if user_row else 0,
                    "active_users": user_row[1] if user_row else 0,
                    "pending_approvals": user_row[2] if user_row else 0,
                    "active_30d": active_30d,
                    "total_classes": class_count,
                    "sso_providers": sso_count,
                }
            ),
            200,
        )
    except Exception as e:
        logger.error("tenant_admin overview failed: %s", e)
        return jsonify({"error": "Failed to fetch overview"}), 500


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@bp.route("/api/tenant-admin/analytics", methods=["GET"])
@require_auth
@require_tenant_admin
def get_analytics():
    """Tenant-scoped usage metrics."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Users by role within tenant
        cur.execute(
            "SELECT COALESCE(role, 'student'), COUNT(*) FROM users WHERE tenant_id = %s GROUP BY role",
            (tenant_id,),
        )
        roles = {r[0]: r[1] for r in cur.fetchall()}

        # New users (last 7 / 30 days) within tenant
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS new_7d,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS new_30d
            FROM users
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        new_users = cur.fetchone()

        # Active users from audit_log within tenant
        cur.execute(
            """
            SELECT
                COUNT(DISTINCT al.user_id) FILTER (WHERE al.created_at >= NOW() - INTERVAL '7 days')  AS active_7d,
                COUNT(DISTINCT al.user_id) FILTER (WHERE al.created_at >= NOW() - INTERVAL '30 days') AS active_30d
            FROM audit_log al
            JOIN users u ON u.user_id::text = al.user_id
            WHERE u.tenant_id = %s
            """,
            (tenant_id,),
        )
        active = cur.fetchone()

        # Content totals within tenant
        cur.execute(
            """
            SELECT
                COUNT(DISTINCT c.class_id)                                                  AS classes,
                COUNT(DISTINCT cc.chapter_id)                                               AS chapters,
                COUNT(DISTINCT ce.exercise_id)                                              AS exercises,
                COUNT(DISTINCT pp.id) FILTER (WHERE pp.completed = TRUE)                   AS completions
            FROM classes c
            JOIN users u ON u.user_id = c.professor_id AND u.tenant_id = %s
            LEFT JOIN class_chapters cc ON cc.class_id = c.class_id
            LEFT JOIN class_exercises ce ON ce.chapter_id = cc.chapter_id
            LEFT JOIN practice_progress pp ON pp.exercise_id = ce.exercise_id
            """,
            (tenant_id,),
        )
        content = cur.fetchone()

        # Daily registrations — last 14 days within tenant
        cur.execute(
            """
            SELECT DATE(created_at)::text, COUNT(*)
            FROM users
            WHERE tenant_id = %s AND created_at >= NOW() - INTERVAL '14 days'
            GROUP BY DATE(created_at)
            ORDER BY 1
            """,
            (tenant_id,),
        )
        daily_regs = [{"date": r[0], "count": r[1]} for r in cur.fetchall()]

        # Top 5 most recent audit actions for this tenant's users
        cur.execute(
            """
            SELECT al.action, COUNT(*) AS cnt
            FROM audit_log al
            JOIN users u ON u.user_id::text = al.user_id
            WHERE u.tenant_id = %s AND al.created_at >= NOW() - INTERVAL '7 days'
            GROUP BY al.action
            ORDER BY cnt DESC
            LIMIT 5
            """,
            (tenant_id,),
        )
        top_actions = [{"action": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.close()
        return (
            jsonify(
                {
                    "users": {
                        "total": sum(roles.values()),
                        "by_role": roles,
                        "new_7d": new_users[0] if new_users else 0,
                        "new_30d": new_users[1] if new_users else 0,
                        "active_7d": active[0] if active else 0,
                        "active_30d": active[1] if active else 0,
                    },
                    "content": {
                        "classes": content[0] if content else 0,
                        "chapters": content[1] if content else 0,
                        "exercises": content[2] if content else 0,
                        "completions": content[3] if content else 0,
                    },
                    "daily_registrations": daily_regs,
                    "top_actions": top_actions,
                }
            ),
            200,
        )
    except Exception as e:
        logger.error("tenant_admin analytics failed: %s", e)
        return jsonify({"error": "Failed to fetch analytics"}), 500


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@bp.route("/api/tenant-admin/users", methods=["GET"])
@require_auth
@require_tenant_admin
def search_users():
    """Paginated, filterable user list scoped to the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(100, max(1, int(request.args.get("limit", 25))))
        offset = (page - 1) * limit
        q = request.args.get("q", "").strip()
        role_filter = request.args.get("role", "").strip()
        status_filter = request.args.get("status", "").strip()

        where_clauses = ["u.tenant_id = %s"]
        params = [tenant_id]

        if q:
            where_clauses.append("(u.name ILIKE %s OR u.email ILIKE %s)")
            params.extend([f"%{q}%", f"%{q}%"])
        if role_filter:
            where_clauses.append("COALESCE(u.role, 'student') = %s")
            params.append(role_filter)
        if status_filter:
            where_clauses.append("u.account_status = %s")
            params.append(status_filter)

        where_sql = " AND ".join(where_clauses)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                u.user_id, u.name, u.email, COALESCE(u.role, 'student'),
                u.account_status, u.created_at,
                COUNT(*) OVER() AS total_count
            FROM users u
            WHERE {where_sql}
            ORDER BY u.created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()
        total = rows[0][6] if rows else 0

        users = [
            {
                "user_id": r[0],
                "name": r[1],
                "email": r[2],
                "role": r[3],
                "account_status": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]
        cur.close()
        return (
            jsonify(
                {
                    "users": users,
                    "total": total,
                    "pages": max(1, -(-total // limit)),
                    "current_page": page,
                }
            ),
            200,
        )
    except Exception as e:
        logger.error("tenant_admin search_users failed: %s", e)
        return jsonify({"error": "Failed to fetch users"}), 500


@bp.route("/api/tenant-admin/users/<user_id>/status", methods=["PATCH"])
@require_auth
@require_tenant_admin
@csrf_exempt
def update_user_status(user_id):
    """Enable or disable a user within the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    data = request.get_json(silent=True) or {}
    new_status = data.get("status")
    if new_status not in ("active", "disabled"):
        return jsonify({"error": "status must be 'active' or 'disabled'"}), 400

    try:
        conn = get_db_connection()
        if not _assert_user_in_tenant(conn, user_id, tenant_id):
            return jsonify({"error": "User not found in your tenant"}), 404

        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET account_status = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
            (new_status, user_id),
        )
        cur.close()
        action = "DISABLE_USER" if new_status == "disabled" else "ENABLE_USER"
        audit(conn, action, resource_type="user", resource_id=user_id)
        conn.commit()
        return jsonify({"message": f"User {new_status}"}), 200
    except Exception as e:
        logger.error("tenant_admin update_user_status failed: %s", e)
        return jsonify({"error": "Failed to update user status"}), 500


@bp.route("/api/tenant-admin/users/<user_id>/role", methods=["PATCH"])
@require_auth
@require_tenant_admin
@csrf_exempt
def change_user_role(user_id):
    """Change a user's role (cannot escalate to superadmin)."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    data = request.get_json(silent=True) or {}
    new_role = data.get("role")
    # tenant_admin cannot grant superadmin
    valid_roles = {"student", "professor", "tenant_admin"}
    if new_role not in valid_roles:
        return jsonify({"error": f"role must be one of: {', '.join(sorted(valid_roles))}"}), 400

    try:
        conn = get_db_connection()
        target = _assert_user_in_tenant(conn, user_id, tenant_id)
        if not target:
            return jsonify({"error": "User not found in your tenant"}), 404
        _, _, _, target_role = target
        if target_role == "superadmin":
            return jsonify({"error": "Cannot modify a superadmin account"}), 403

        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET role = %s,
                is_super_admin = FALSE,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            """,
            (new_role, user_id),
        )
        cur.close()
        audit(
            conn,
            "UPDATE_USER_ROLE",
            resource_type="user",
            resource_id=user_id,
            details={"new_role": new_role},
        )
        conn.commit()
        return jsonify({"message": "Role updated"}), 200
    except Exception as e:
        logger.error("tenant_admin change_user_role failed: %s", e)
        return jsonify({"error": "Failed to update role"}), 500


@bp.route("/api/tenant-admin/users/<user_id>/force-reset", methods=["POST"])
@require_auth
@require_tenant_admin
@csrf_exempt
def force_password_reset(user_id):
    """Flag a user's account so they must change their password next login."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        if not _assert_user_in_tenant(conn, user_id, tenant_id):
            return jsonify({"error": "User not found in your tenant"}), 404

        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET requires_password_reset = TRUE, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
            (user_id,),
        )
        cur.close()
        audit(conn, "FORCE_PASSWORD_RESET", resource_type="user", resource_id=user_id)
        conn.commit()
        return jsonify({"message": "Password reset required on next login"}), 200
    except Exception as e:
        logger.error("tenant_admin force_password_reset failed: %s", e)
        return jsonify({"error": "Failed to set password reset flag"}), 500


@bp.route("/api/tenant-admin/users/<user_id>/export", methods=["GET"])
@require_auth
@require_tenant_admin
def export_user_data(user_id):
    """GDPR data export for a user in the tenant."""
    import json
    from flask import Response

    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        target = _assert_user_in_tenant(conn, user_id, tenant_id)
        if not target:
            return jsonify({"error": "User not found in your tenant"}), 404

        cur = conn.cursor()

        # Core user
        cur.execute(
            "SELECT user_id, name, email, role, account_status, created_at, updated_at FROM users WHERE user_id = %s",
            (user_id,),
        )
        urow = cur.fetchone()
        user_data = {
            "user_id": urow[0],
            "name": urow[1],
            "email": urow[2],
            "role": urow[3],
            "account_status": urow[4],
            "created_at": urow[5].isoformat() if urow[5] else None,
            "updated_at": urow[6].isoformat() if urow[6] else None,
        }

        # Owned classes
        cur.execute(
            "SELECT class_id, name, description, created_at FROM classes WHERE professor_id = %s",
            (user_id,),
        )
        owned_classes = [
            {
                "class_id": r[0],
                "name": r[1],
                "description": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
            }
            for r in cur.fetchall()
        ]

        # Enrolled classes
        cur.execute(
            """
            SELECT c.class_id, c.name, ce.enrolled_at
            FROM class_enrollments ce
            JOIN classes c ON c.class_id = ce.class_id
            WHERE ce.user_id = %s
            """,
            (user_id,),
        )
        enrolled = [
            {"class_id": r[0], "name": r[1], "enrolled_at": r[2].isoformat() if r[2] else None}
            for r in cur.fetchall()
        ]

        # Last 200 exercise attempts
        cur.execute(
            """
            SELECT exercise_id, completed, score, attempts, updated_at
            FROM practice_progress
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT 200
            """,
            (user_id,),
        )
        attempts = [
            {
                "exercise_id": r[0],
                "completed": r[1],
                "score": r[2],
                "attempts": r[3],
                "updated_at": r[4].isoformat() if r[4] else None,
            }
            for r in cur.fetchall()
        ]

        cur.close()

        # FERPA: a tenant admin exporting another user's education records is a
        # disclosure — record who accessed whose data.
        audit_commit(conn, "EXPORT_USER_DATA", resource_type="user", resource_id=user_id)

        export = {
            "user": user_data,
            "owned_classes": owned_classes,
            "enrolled_classes": enrolled,
            "exercise_attempts": attempts,
        }
        filename = f"user_export_{user_data['email'].replace('@', '_at_')}.json"
        return Response(
            json.dumps(export, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error("tenant_admin export_user_data failed: %s", e)
        return jsonify({"error": "Failed to export user data"}), 500


@bp.route("/api/tenant-admin/users/<user_id>", methods=["DELETE"])
@require_auth
@require_tenant_admin
@csrf_exempt
def delete_user(user_id):
    """Cascade-delete a user within the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        target = _assert_user_in_tenant(conn, user_id, tenant_id)
        if not target:
            return jsonify({"error": "User not found in your tenant"}), 404

        _, name, email, role = target
        if role == "superadmin":
            return jsonify({"error": "Cannot delete a superadmin account"}), 403
        if user_id == g.user_id:
            return jsonify({"error": "Cannot delete your own account"}), 400

        cur = conn.cursor()

        # 1. Remove student enrollments in other classes
        cur.execute("DELETE FROM class_enrollments WHERE student_id = %s", (user_id,))

        # 2. Cascade-delete classes the user owns as a professor
        cur.execute("SELECT class_id FROM classes WHERE professor_id = %s", (user_id,))
        professor_class_ids = [r[0] for r in cur.fetchall()]
        for cid in professor_class_ids:
            cur.execute("DELETE FROM class_enrollments WHERE class_id = %s", (cid,))
            cur.execute("SELECT chapter_id FROM class_chapters WHERE class_id = %s", (cid,))
            for (chid,) in cur.fetchall():
                cur.execute("DELETE FROM class_exercises WHERE chapter_id = %s", (chid,))
            cur.execute("DELETE FROM class_chapters WHERE class_id = %s", (cid,))
        cur.execute("DELETE FROM classes WHERE professor_id = %s", (user_id,))

        # 3. Delete the user record
        cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        cur.close()

        audit(
            conn,
            "DELETE_USER",
            resource_type="user",
            resource_id=user_id,
            details={"name": name, "email": email},
        )
        conn.commit()
        return jsonify({"message": "User deleted"}), 200
    except Exception as e:
        logger.error("tenant_admin delete_user failed: %s", e)
        return jsonify({"error": "Failed to delete user"}), 500


@bp.route("/api/tenant-admin/users/<user_id>/impersonate", methods=["POST"])
@require_auth
@require_tenant_admin
@csrf_exempt
@rate_limit("10/minute")
def impersonate_user(user_id):
    """Issue a 1-hour impersonation token for a user within the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        target = _assert_user_in_tenant(conn, user_id, tenant_id)
        if not target:
            return jsonify({"error": "User not found in your tenant"}), 404

        _, name, email, role = target
        # Cannot impersonate superadmin or another tenant_admin
        if role in ("superadmin", "tenant_admin"):
            return jsonify({"error": "Cannot impersonate admin accounts"}), 403

        token = create_jwt_token(
            user_id,
            role=role,
            name=name,
            email=email,
            impersonator_id=g.user_id,
            expires_hours=1,
            tenant_id=tenant_id,
        )
        audit(
            conn,
            "IMPERSONATE_USER",
            resource_type="user",
            resource_id=user_id,
            details={"impersonator_id": g.user_id},
        )
        conn.commit()
        return jsonify({"token": token, "user": {"name": name, "email": email, "role": role}}), 200
    except Exception as e:
        logger.error("tenant_admin impersonate_user failed: %s", e)
        return jsonify({"error": "Failed to create impersonation token"}), 500


# ---------------------------------------------------------------------------
# Pending Approvals
# ---------------------------------------------------------------------------


@bp.route("/api/tenant-admin/pending-approvals", methods=["GET"])
@require_auth
@require_tenant_admin
def get_pending_approvals():
    """List users awaiting approval within the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT user_id, name, email, COALESCE(role, 'student'), created_at
            FROM users
            WHERE tenant_id = %s AND account_status = 'pending_approval'
            ORDER BY created_at DESC
            """,
            (tenant_id,),
        )
        rows = cur.fetchall()
        cur.close()
        pending = [
            {
                "user_id": r[0],
                "name": r[1],
                "email": r[2],
                "role": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]
        return jsonify({"pending": pending, "count": len(pending)}), 200
    except Exception as e:
        logger.error("tenant_admin get_pending_approvals failed: %s", e)
        return jsonify({"error": "Failed to fetch pending approvals"}), 500


@bp.route("/api/tenant-admin/users/<user_id>/approve", methods=["PATCH"])
@require_auth
@require_tenant_admin
@csrf_exempt
def approve_user(user_id):
    """Approve a pending user within the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        if not _assert_user_in_tenant(conn, user_id, tenant_id):
            return jsonify({"error": "User not found in your tenant"}), 404

        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET account_status = 'active', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND account_status = 'pending_approval'
            """,
            (user_id,),
        )
        if cur.rowcount == 0:
            cur.close()
            return jsonify({"error": "User is not pending approval"}), 400
        cur.close()
        audit(
            conn,
            "ENABLE_USER",
            resource_type="user",
            resource_id=user_id,
            details={"action": "approved_admin"},
        )
        conn.commit()
        return jsonify({"message": "User approved"}), 200
    except Exception as e:
        logger.error("tenant_admin approve_user failed: %s", e)
        return jsonify({"error": "Failed to approve user"}), 500


@bp.route("/api/tenant-admin/users/<user_id>/deny", methods=["PATCH"])
@require_auth
@require_tenant_admin
@csrf_exempt
def deny_user(user_id):
    """Deny a pending user within the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        if not _assert_user_in_tenant(conn, user_id, tenant_id):
            return jsonify({"error": "User not found in your tenant"}), 404

        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET account_status = 'denied', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND account_status = 'pending_approval'
            """,
            (user_id,),
        )
        if cur.rowcount == 0:
            cur.close()
            return jsonify({"error": "User is not pending approval"}), 400
        cur.close()
        audit(
            conn,
            "DISABLE_USER",
            resource_type="user",
            resource_id=user_id,
            details={"action": "denied_admin"},
        )
        conn.commit()
        return jsonify({"message": "User denied"}), 200
    except Exception as e:
        logger.error("tenant_admin deny_user failed: %s", e)
        return jsonify({"error": "Failed to deny user"}), 500


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------


@bp.route("/api/tenant-admin/classes", methods=["GET"])
@require_auth
@require_tenant_admin
def list_classes():
    """Paginated class list for professors in the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(100, max(1, int(request.args.get("limit", 25))))
        offset = (page - 1) * limit
        q = request.args.get("q", "").strip()

        params = [tenant_id]
        extra_where = ""
        if q:
            extra_where = "AND (c.name ILIKE %s OR u.name ILIKE %s OR u.email ILIKE %s)"
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

        conn = get_db_connection()
        cur = conn.cursor()

        # Separate count query to avoid window-function + GROUP BY complexity
        cur.execute(
            f"SELECT COUNT(*) FROM classes c JOIN users u ON u.user_id = c.professor_id AND u.tenant_id = %s WHERE 1=1 {extra_where}",
            params,
        )
        total = cur.fetchone()[0]

        # Page data — subqueries for per-class counts
        cur.execute(
            f"""
            SELECT
                c.class_id, c.name, c.description,
                u.user_id AS professor_id, u.name AS professor_name, u.email AS professor_email,
                (SELECT COUNT(*) FROM class_enrollments WHERE class_id = c.class_id) AS student_count,
                (SELECT COUNT(*) FROM class_chapters    WHERE class_id = c.class_id) AS chapter_count,
                c.created_at
            FROM classes c
            JOIN users u ON u.user_id = c.professor_id AND u.tenant_id = %s
            WHERE 1=1 {extra_where}
            ORDER BY c.created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()

        classes = [
            {
                "class_id": r[0],
                "name": r[1],
                "description": r[2],
                "professor_id": r[3],
                "professor_name": r[4],
                "professor_email": r[5],
                "student_count": r[6],
                "chapter_count": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
            }
            for r in rows
        ]
        cur.close()
        return (
            jsonify(
                {
                    "classes": classes,
                    "total": total,
                    "pages": max(1, -(-total // limit)),
                    "current_page": page,
                }
            ),
            200,
        )
    except Exception as e:
        logger.error("tenant_admin list_classes failed: %s", e)
        return jsonify({"error": "Failed to fetch classes"}), 500


@bp.route("/api/tenant-admin/classes/<class_id>/transfer", methods=["PATCH"])
@require_auth
@require_tenant_admin
@csrf_exempt
def transfer_class(class_id):
    """Transfer a class to another professor within the same tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    data = request.get_json(silent=True) or {}
    new_professor_email = (data.get("professor_email") or "").strip()
    if not new_professor_email:
        return jsonify({"error": "professor_email is required"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Verify class belongs to tenant
        cur.execute(
            """
            SELECT c.class_id FROM classes c
            JOIN users u ON u.user_id = c.professor_id AND u.tenant_id = %s
            WHERE c.class_id = %s
            """,
            (tenant_id, class_id),
        )
        if not cur.fetchone():
            cur.close()
            return jsonify({"error": "Class not found in your tenant"}), 404

        # Verify new professor is in the same tenant
        cur.execute(
            "SELECT user_id, role FROM users WHERE email = %s AND tenant_id = %s",
            (new_professor_email, tenant_id),
        )
        prof = cur.fetchone()
        if not prof or prof[1] not in ("professor", "tenant_admin", "superadmin"):
            cur.close()
            return jsonify({"error": "Target professor not found in your tenant"}), 404

        new_professor_id = prof[0]
        cur.execute(
            "UPDATE classes SET professor_id = %s, updated_at = CURRENT_TIMESTAMP WHERE class_id = %s",
            (new_professor_id, class_id),
        )
        cur.close()
        audit(
            conn,
            "TRANSFER_CLASS",
            resource_type="class",
            resource_id=class_id,
            details={
                "new_professor_id": new_professor_id,
                "new_professor_email": new_professor_email,
            },
        )
        conn.commit()
        return jsonify({"message": "Class transferred"}), 200
    except Exception as e:
        logger.error("tenant_admin transfer_class failed: %s", e)
        return jsonify({"error": "Failed to transfer class"}), 500


@bp.route("/api/tenant-admin/classes/<class_id>", methods=["DELETE"])
@require_auth
@require_tenant_admin
@csrf_exempt
def delete_class(class_id):
    """Cascade-delete a class that belongs to the tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Verify class belongs to tenant
        cur.execute(
            """
            SELECT c.class_id, c.name FROM classes c
            JOIN users u ON u.user_id = c.professor_id AND u.tenant_id = %s
            WHERE c.class_id = %s
            """,
            (tenant_id, class_id),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return jsonify({"error": "Class not found in your tenant"}), 404

        class_name = row[1]
        # Cascade delete
        cur.execute(
            "DELETE FROM class_exercises WHERE chapter_id IN (SELECT chapter_id FROM class_chapters WHERE class_id = %s)",
            (class_id,),
        )
        cur.execute("DELETE FROM class_chapters WHERE class_id = %s", (class_id,))
        cur.execute("DELETE FROM class_enrollments WHERE class_id = %s", (class_id,))
        cur.execute("DELETE FROM classes WHERE class_id = %s", (class_id,))
        cur.close()
        audit(
            conn,
            "DELETE_CLASS",
            resource_type="class",
            resource_id=class_id,
            details={"name": class_name},
        )
        conn.commit()
        return jsonify({"message": "Class deleted"}), 200
    except Exception as e:
        logger.error("tenant_admin delete_class failed: %s", e)
        return jsonify({"error": "Failed to delete class"}), 500


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------


@bp.route("/api/tenant-admin/audit", methods=["GET"])
@require_auth
@require_tenant_admin
def get_audit_log():
    """Paginated audit log for events caused by users in this tenant."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(100, max(1, int(request.args.get("limit", 50))))
        offset = (page - 1) * limit
        action_filter = request.args.get("action", "").strip()
        from_date = request.args.get("from", "").strip()
        to_date = request.args.get("to", "").strip()

        # Build dynamic WHERE
        extra_clauses = []
        params: list = [tenant_id]

        if action_filter:
            extra_clauses.append("al.action = %s")
            params.append(action_filter)
        if from_date:
            extra_clauses.append("al.created_at >= %s::timestamptz")
            params.append(from_date)
        if to_date:
            extra_clauses.append("al.created_at <= %s::timestamptz")
            params.append(to_date)

        extra_sql = ""
        if extra_clauses:
            extra_sql = "AND " + " AND ".join(extra_clauses)

        conn = get_db_connection()
        cur = conn.cursor()

        count_params = list(params)
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM audit_log al
            JOIN users u ON u.user_id::text = al.user_id
            WHERE u.tenant_id = %s {extra_sql}
            """,
            count_params,
        )
        total = cur.fetchone()[0]

        cur.execute(
            f"""
            SELECT al.id, al.user_id, u.name, u.email, al.action,
                   al.resource_type, al.resource_id, al.details, al.created_at
            FROM audit_log al
            JOIN users u ON u.user_id::text = al.user_id
            WHERE u.tenant_id = %s {extra_sql}
            ORDER BY al.created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()
        cur.close()

        entries = [
            {
                "id": r[0],
                "user_id": r[1],
                "user_name": r[2],
                "user_email": r[3],
                "action": r[4],
                "resource_type": r[5],
                "resource_id": r[6],
                "details": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
            }
            for r in rows
        ]
        return (
            jsonify(
                {
                    "entries": entries,
                    "total": total,
                    "page": page,
                    "pages": max(1, -(-total // limit)),
                }
            ),
            200,
        )
    except Exception as e:
        logger.error("tenant_admin get_audit_log failed: %s", e)
        return jsonify({"error": "Failed to fetch audit log"}), 500


# ---------------------------------------------------------------------------
# Announcements (tenant-scoped)
# ---------------------------------------------------------------------------

_announcements_migrated = False


def _ensure_announcements_tenant_column():
    """Idempotent migration: adds tenant_id column to announcements if missing."""
    global _announcements_migrated
    if _announcements_migrated:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "ALTER TABLE announcements "
            "ADD COLUMN IF NOT EXISTS tenant_id UUID "
            "REFERENCES tenants(tenant_id) ON DELETE CASCADE"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_announcements_tenant_id " "ON announcements(tenant_id)"
        )
        conn.commit()
        cur.close()
        _announcements_migrated = True
    except Exception as e:
        logger.warning("Could not migrate announcements.tenant_id column: %s", e)


@bp.route("/api/tenant-admin/announcements", methods=["GET"])
@require_auth
@require_tenant_admin
def list_tenant_announcements():
    """List announcements scoped to this tenant."""
    _ensure_announcements_tenant_column()
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.announcement_id, a.title, a.message, a.type, a.target_roles,
                   a.is_active, a.expires_at, a.created_at, u.name AS created_by_name
            FROM announcements a
            LEFT JOIN users u ON u.user_id = a.created_by
            WHERE a.tenant_id = %s
            ORDER BY a.created_at DESC
            """,
            (tenant_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return (
            jsonify(
                {
                    "announcements": [
                        {
                            "id": r[0],
                            "title": r[1],
                            "message": r[2],
                            "type": r[3],
                            "target_roles": r[4] or [],
                            "is_active": r[5],
                            "expires_at": r[6].isoformat() if r[6] else None,
                            "created_at": r[7].isoformat() if r[7] else None,
                            "created_by_name": r[8],
                        }
                        for r in rows
                    ]
                }
            ),
            200,
        )
    except Exception as e:
        logger.error("list_tenant_announcements failed: %s", e)
        return jsonify({"error": "Failed to list announcements"}), 500


@bp.route("/api/tenant-admin/announcements", methods=["POST"])
@csrf_exempt
@require_auth
@require_tenant_admin
def create_tenant_announcement():
    """Create an announcement scoped to this tenant."""
    _ensure_announcements_tenant_column()
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    data = request.json or {}
    title = (data.get("title") or "").strip()
    message = (data.get("message") or "").strip()
    atype = data.get("type", "info")
    target = data.get("target_roles", [])
    expires = data.get("expires_at") or None

    if not title or not message:
        return jsonify({"error": "title and message are required"}), 400
    if atype not in ("info", "warning", "danger", "success"):
        return jsonify({"error": "type must be info, warning, danger, or success"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO announcements
                (title, message, type, target_roles, expires_at, created_by, tenant_id, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING announcement_id
            """,
            (title, message, atype, target or [], expires, g.user_id, tenant_id),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        audit(conn, "CREATE_ANNOUNCEMENT", resource_type="announcement", resource_id=str(new_id))
        return jsonify({"success": True, "id": new_id}), 201
    except Exception as e:
        logger.error("create_tenant_announcement failed: %s", e)
        return jsonify({"error": "Failed to create announcement"}), 500


@bp.route("/api/tenant-admin/announcements/<announcement_id>", methods=["PATCH"])
@csrf_exempt
@require_auth
@require_tenant_admin
def update_tenant_announcement(announcement_id):
    """Update an announcement — tenant-scoped (cannot modify other tenants' announcements)."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400

    data = request.json or {}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        updates, params = [], []
        for field in ("title", "message", "type", "is_active", "expires_at"):
            if field in data:
                updates.append(f"{field} = %s")
                params.append(data[field])
        if not updates:
            return jsonify({"error": "No fields to update"}), 400
        params += [announcement_id, tenant_id]
        cur.execute(
            f"UPDATE announcements SET {', '.join(updates)}, updated_at = NOW() "
            f"WHERE announcement_id = %s AND tenant_id = %s RETURNING announcement_id",
            params,
        )
        if not cur.fetchone():
            return jsonify({"error": "Announcement not found"}), 404
        conn.commit()
        cur.close()
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error("update_tenant_announcement failed: %s", e)
        return jsonify({"error": "Failed to update announcement"}), 500


@bp.route("/api/tenant-admin/announcements/<announcement_id>", methods=["DELETE"])
@csrf_exempt
@require_auth
@require_tenant_admin
def delete_tenant_announcement(announcement_id):
    """Delete an announcement — tenant-scoped."""
    tenant_id = _scoped_tenant_id()
    if not tenant_id:
        return jsonify({"error": "tenant_id is required"}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM announcements WHERE announcement_id = %s AND tenant_id = %s "
            "RETURNING announcement_id",
            (announcement_id, tenant_id),
        )
        if not cur.fetchone():
            return jsonify({"error": "Announcement not found"}), 404
        conn.commit()
        cur.close()
        audit(
            conn, "DELETE_ANNOUNCEMENT", resource_type="announcement", resource_id=announcement_id
        )
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error("delete_tenant_announcement failed: %s", e)
        return jsonify({"error": "Failed to delete announcement"}), 500
