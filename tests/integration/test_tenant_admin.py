"""
Integration tests — tenant-admin HTTP endpoints against a real Postgres database.

These exist because the tenant-admin user-delete cascade is the one place in
this codebase that writes its SQL inline instead of going through
``repositories/``, and it had drifted from the schema: it deleted from
``class_enrollments WHERE user_id`` (the column is ``student_id``) and from a
table called ``exercises`` (it is ``class_exercises``). Both raise from
PostgreSQL, the handler catches every exception into a 500, and so the user was
silently never deleted. Nothing caught it because no test drove the endpoint
against a real database — a mocked cursor accepts any SQL at all.

Like ``test_superadmin.py`` these drive the Flask test client, so they cannot
use the ``tx`` savepoint fixture: the application opens its own connection from
the pool and would not see uncommitted rows. Fixture rows are therefore
committed and swept up on teardown, and every id is prefixed ``inttest_`` so the
session-level cleanup in conftest can finish the job after an interrupted run.
"""

import pytest

from app import app as flask_app
from core.auth import create_jwt_token
from tests.integration.conftest import (
    make_chapter_id,
    make_class_id,
    make_exercise_id,
    make_user_id,
)

TENANT_ID = "inttest_tenant_admin_tenant"
TENANT_ADMIN_ID = "inttest_tenant_admin"


# ---------------------------------------------------------------------------
# Committed fixtures
# ---------------------------------------------------------------------------


def _insert_user(conn, user_id, role, tenant_id=TENANT_ID):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, name, email, password_hash, role, tenant_id,
                               token_version)
            VALUES (%s, %s, %s, 'x', %s, %s, 0)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id, f"Integration {role}", f"{user_id}@tenant.example.test", role, tenant_id),
        )
    conn.commit()


def _insert_class(conn, class_id, professor_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO classes (class_id, name, professor_id, tenant_id)
            VALUES (%s, %s, %s, %s)
            """,
            (class_id, f"Integration class {class_id}", professor_id, TENANT_ID),
        )
    conn.commit()


def _enroll(conn, class_id, student_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO class_enrollments (class_id, student_id) VALUES (%s, %s)",
            (class_id, student_id),
        )
    conn.commit()


def _insert_chapter_with_exercise(conn, class_id):
    chapter_id, exercise_id = make_chapter_id(), make_exercise_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO class_chapters (chapter_id, class_id, title) VALUES (%s, %s, 'Chapter 1')",
            (chapter_id, class_id),
        )
        cur.execute(
            """
            INSERT INTO class_exercises (exercise_id, chapter_id, class_id, title)
            VALUES (%s, %s, %s, 'Exercise 1')
            """,
            (exercise_id, chapter_id, class_id),
        )
    conn.commit()
    return chapter_id, exercise_id


def _count(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


@pytest.fixture(scope="module")
def tenant(db_conn):
    """The tenant and the acting tenant_admin, committed so the app can see them."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenants (tenant_id, name) VALUES (%s, 'Integration Tenant')
            ON CONFLICT (tenant_id) DO NOTHING
            """,
            (TENANT_ID,),
        )
    db_conn.commit()
    _insert_user(db_conn, TENANT_ADMIN_ID, "tenant_admin")

    yield TENANT_ID

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE user_id = %s", (TENANT_ADMIN_ID,))
        cur.execute("DELETE FROM tenants WHERE tenant_id = %s", (TENANT_ID,))
    db_conn.commit()


@pytest.fixture
def client(tenant):
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app.test_client()


@pytest.fixture
def tenant_admin_headers():
    token = create_jwt_token(
        TENANT_ADMIN_ID,
        role="tenant_admin",
        name="Integration tenant_admin",
        email=f"{TENANT_ADMIN_ID}@tenant.example.test",
        tenant_id=TENANT_ID,
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# DELETE /api/tenant-admin/users/<user_id>
# ---------------------------------------------------------------------------


def test_delete_student_removes_their_enrollments(client, tenant_admin_headers, db_conn):
    """Deleting an enrolled student clears the enrollment and leaves the class alone.

    This is the regression test for ``class_enrollments WHERE user_id``: against a
    real database that statement raises UndefinedColumn, the handler turns it into
    a 500, and the student is still there afterwards.
    """
    professor_id, student_id, class_id = make_user_id(), make_user_id(), make_class_id()
    _insert_user(db_conn, professor_id, "professor")
    _insert_user(db_conn, student_id, "student")
    _insert_class(db_conn, class_id, professor_id)
    _enroll(db_conn, class_id, student_id)

    try:
        res = client.delete(f"/api/tenant-admin/users/{student_id}", headers=tenant_admin_headers)
        assert res.status_code == 200, res.json

        assert _count(db_conn, "SELECT COUNT(*) FROM users WHERE user_id = %s", (student_id,)) == 0
        assert (
            _count(
                db_conn,
                "SELECT COUNT(*) FROM class_enrollments WHERE student_id = %s",
                (student_id,),
            )
            == 0
        )
        # Someone else's class must survive the student's deletion.
        assert _count(db_conn, "SELECT COUNT(*) FROM classes WHERE class_id = %s", (class_id,)) == 1
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM classes WHERE class_id = %s", (class_id,))
            cur.execute("DELETE FROM users WHERE user_id IN (%s, %s)", (professor_id, student_id))
        db_conn.commit()


def test_delete_professor_removes_their_classes_and_exercises(
    client, tenant_admin_headers, db_conn
):
    """Deleting a professor cascades through their chapters, exercises and enrollments.

    This is the regression test for ``DELETE FROM exercises``: the handler walks
    the professor's chapters and deletes each one's exercises by hand, and the
    table it named does not exist.
    """
    professor_id, student_id, class_id = make_user_id(), make_user_id(), make_class_id()
    _insert_user(db_conn, professor_id, "professor")
    _insert_user(db_conn, student_id, "student")
    _insert_class(db_conn, class_id, professor_id)
    _enroll(db_conn, class_id, student_id)
    chapter_id, exercise_id = _insert_chapter_with_exercise(db_conn, class_id)

    try:
        res = client.delete(f"/api/tenant-admin/users/{professor_id}", headers=tenant_admin_headers)
        assert res.status_code == 200, res.json

        assert (
            _count(db_conn, "SELECT COUNT(*) FROM users WHERE user_id = %s", (professor_id,)) == 0
        )
        assert _count(db_conn, "SELECT COUNT(*) FROM classes WHERE class_id = %s", (class_id,)) == 0
        assert (
            _count(
                db_conn,
                "SELECT COUNT(*) FROM class_exercises WHERE exercise_id = %s",
                (exercise_id,),
            )
            == 0
        )
        assert (
            _count(
                db_conn,
                "SELECT COUNT(*) FROM class_chapters WHERE chapter_id = %s",
                (chapter_id,),
            )
            == 0
        )
        assert (
            _count(
                db_conn, "SELECT COUNT(*) FROM class_enrollments WHERE class_id = %s", (class_id,)
            )
            == 0
        )
        # The enrolled student is not collateral damage.
        assert _count(db_conn, "SELECT COUNT(*) FROM users WHERE user_id = %s", (student_id,)) == 1
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM classes WHERE class_id = %s", (class_id,))
            cur.execute("DELETE FROM users WHERE user_id IN (%s, %s)", (professor_id, student_id))
        db_conn.commit()


def test_delete_user_outside_tenant_is_rejected(client, tenant_admin_headers, db_conn):
    """A tenant_admin cannot reach users belonging to another tenant."""
    outsider_id = make_user_id()
    _insert_user(db_conn, outsider_id, "student", tenant_id=None)

    try:
        res = client.delete(f"/api/tenant-admin/users/{outsider_id}", headers=tenant_admin_headers)
        assert res.status_code == 404
        assert _count(db_conn, "SELECT COUNT(*) FROM users WHERE user_id = %s", (outsider_id,)) == 1
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (outsider_id,))
        db_conn.commit()
