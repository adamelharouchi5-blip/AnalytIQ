"""
Tests for the data flywheel instrumentation layer.

Covers:
- flywheel_repository: every public function with a mocked connection
- exercise_generator: _gen_meta injection and _extract_topics
- blueprints/exercises: _classify_error_category helper
- blueprints/flywheel: API endpoints (auth, authorization, response shape)
"""

import json
import pytest
from unittest.mock import MagicMock, Mock, patch


# ===========================================================================
# flywheel_repository tests
# ===========================================================================


@pytest.fixture
def mock_conn():
    """Return a mock psycopg2 connection with a configurable cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


class TestInsertGenerationEvent:
    def test_returns_uuid_on_success(self, mock_conn):
        from repositories.flywheel_repository import insert_generation_event

        conn, cur = mock_conn
        cur.fetchone.return_value = ("abc-123",)

        result = insert_generation_event(conn, {"exercise_id": "ex_1", "class_id": "cls_1"})
        assert result == "abc-123"
        cur.execute.assert_called_once()

    def test_returns_none_on_db_error(self, mock_conn):
        from repositories.flywheel_repository import insert_generation_event

        conn, cur = mock_conn
        cur.execute.side_effect = Exception("DB error")

        result = insert_generation_event(conn, {"exercise_id": "ex_1"})
        assert result is None

    def test_handles_empty_event_data(self, mock_conn):
        from repositories.flywheel_repository import insert_generation_event

        conn, cur = mock_conn
        cur.fetchone.return_value = ("new-uuid",)

        result = insert_generation_event(conn, {})
        assert result == "new-uuid"


class TestInsertOutcomeEvent:
    def test_returns_uuid_on_success(self, mock_conn):
        from repositories.flywheel_repository import insert_outcome_event

        conn, cur = mock_conn
        cur.fetchone.return_value = ("outcome-uuid",)

        result = insert_outcome_event(
            conn,
            {
                "exercise_id": "ex_1",
                "user_id": "user_1",
                "passed": True,
                "total_score": 85,
                "tests_passed": 4,
                "tests_total": 5,
            },
        )
        assert result == "outcome-uuid"

    def test_returns_none_on_error(self, mock_conn):
        from repositories.flywheel_repository import insert_outcome_event

        conn, cur = mock_conn
        cur.execute.side_effect = RuntimeError("connection lost")

        result = insert_outcome_event(conn, {"exercise_id": "ex_1"})
        assert result is None


class TestGetOutcomeCount:
    def test_returns_count(self, mock_conn):
        from repositories.flywheel_repository import get_outcome_count_for_user_exercise

        conn, cur = mock_conn
        cur.fetchone.return_value = (7,)

        count = get_outcome_count_for_user_exercise(conn, "user_1", "ex_1")
        assert count == 7

    def test_returns_zero_on_error(self, mock_conn):
        from repositories.flywheel_repository import get_outcome_count_for_user_exercise

        conn, cur = mock_conn
        cur.execute.side_effect = Exception("oops")

        count = get_outcome_count_for_user_exercise(conn, "user_1", "ex_1")
        assert count == 0


class TestUpsertQualityScore:
    def test_does_not_raise(self, mock_conn):
        from repositories.flywheel_repository import upsert_quality_score

        conn, cur = mock_conn
        # Should return None silently
        upsert_quality_score(conn, {"exercise_id": "ex_1", "quality_score": 0.75})
        cur.execute.assert_called_once()

    def test_swallows_db_error(self, mock_conn):
        from repositories.flywheel_repository import upsert_quality_score

        conn, cur = mock_conn
        cur.execute.side_effect = Exception("unique violation")

        # Must not raise
        upsert_quality_score(conn, {"exercise_id": "ex_1"})


class TestGetQualityScore:
    def test_returns_dict_when_found(self, mock_conn):
        from repositories.flywheel_repository import get_quality_score
        from datetime import datetime

        conn, cur = mock_conn
        cur.fetchone.return_value = (
            "ex_1", None, 20, 10, 0.4, 0.6, 2.5, 120, 0.0, 0.2,
            0.72, "good", "logic_error", {}, datetime(2026, 1, 1), 20,
        )

        result = get_quality_score(conn, "ex_1")
        assert result is not None
        assert result["exercise_id"] == "ex_1"
        assert result["quality_label"] == "good"
        assert result["quality_score"] == 0.72

    def test_returns_none_when_not_found(self, mock_conn):
        from repositories.flywheel_repository import get_quality_score

        conn, cur = mock_conn
        cur.fetchone.return_value = None

        assert get_quality_score(conn, "ex_unknown") is None

    def test_returns_none_on_error(self, mock_conn):
        from repositories.flywheel_repository import get_quality_score

        conn, cur = mock_conn
        cur.execute.side_effect = Exception("DB down")

        assert get_quality_score(conn, "ex_1") is None


class TestGetClassQualityScores:
    def test_returns_list(self, mock_conn):
        from repositories.flywheel_repository import get_class_quality_scores
        from datetime import datetime

        conn, cur = mock_conn
        cur.fetchall.return_value = [
            ("ex_1", None, 10, 5, 0.3, 0.5, 3.0, None, 0.0, 0.4,
             0.55, "good", None, {}, datetime(2026, 1, 1), 10),
        ]

        results = get_class_quality_scores(conn, "cls_1")
        assert len(results) == 1
        assert results[0]["exercise_id"] == "ex_1"

    def test_returns_empty_list_on_error(self, mock_conn):
        from repositories.flywheel_repository import get_class_quality_scores

        conn, cur = mock_conn
        cur.execute.side_effect = Exception("oops")

        assert get_class_quality_scores(conn, "cls_1") == []


class TestGetExercisesNeedingUpdate:
    def test_returns_candidates(self, mock_conn):
        from repositories.flywheel_repository import get_exercises_needing_quality_update

        conn, cur = mock_conn
        cur.fetchall.return_value = [("ex_1", None, 8), ("ex_2", "gen-uuid", 12)]

        results = get_exercises_needing_quality_update(conn, min_outcomes=5)
        assert len(results) == 2
        assert results[0]["exercise_id"] == "ex_1"
        assert results[0]["new_outcome_count"] == 8

    def test_returns_empty_on_error(self, mock_conn):
        from repositories.flywheel_repository import get_exercises_needing_quality_update

        conn, cur = mock_conn
        cur.execute.side_effect = Exception("timeout")

        assert get_exercises_needing_quality_update(conn) == []


# ===========================================================================
# exercise_generator tests
# ===========================================================================


class TestExtractTopics:
    def test_returns_list(self):
        from services.exercise_generator import _extract_topics

        topics = _extract_topics("Compute mean of DataFrame column", "Use pandas to calculate the average age")
        assert isinstance(topics, list)
        assert len(topics) > 0

    def test_filters_stop_words(self):
        from services.exercise_generator import _extract_topics

        topics = _extract_topics("Write a function", "Use the value and return the result")
        # Stop words like "write", "use", "the", "and", "return" should be filtered
        assert "write" not in topics
        assert "the" not in topics

    def test_deduplicates(self):
        from services.exercise_generator import _extract_topics

        topics = _extract_topics("pandas pandas pandas", "pandas dataframe")
        assert topics.count("pandas") == 1

    def test_max_ten_topics(self):
        from services.exercise_generator import _extract_topics

        long_title = " ".join(f"topic{i}" for i in range(20))
        topics = _extract_topics(long_title, "")
        assert len(topics) <= 10


class TestGenerateExercisesGenMeta:
    def test_gen_meta_present_in_each_exercise(self):
        """Each validated exercise should have a _gen_meta key."""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_exercises = [
            {
                "language": "Python",
                "title": "Compute mean",
                "description": "Write a function compute_mean(numbers) that returns the mean.",
                "difficulty": "easy",
                "solution_code": "def compute_mean(numbers):\n    return sum(numbers) / len(numbers)",
                "test_cases": [{"input": [1, 2, 3], "expected_output": 2.0}],
            }
        ]
        gemini_response = {
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(mock_exercises)}]}}
            ],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 200},
        }

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = gemini_response

        with patch("requests.post", return_value=mock_resp):
            result = generate_exercises_from_pdf_content(
                "some chapter text", "Test Chapter", api_key="fake-key"
            )

        assert len(result) == 1
        meta = result[0]["_gen_meta"]
        assert meta["gemini_model"] == "gemini-flash-latest"
        assert meta["prompt_tokens"] == 100
        assert meta["completion_tokens"] == 200
        assert meta["difficulty_generated"] == "easy"
        assert meta["was_truncated"] is False
        assert isinstance(meta["detected_topics"], list)

    def test_truncation_flag_set(self):
        from services.exercise_generator import generate_exercises_from_pdf_content, _TRUNCATION_LIMIT

        long_text = "a" * (_TRUNCATION_LIMIT + 100)
        mock_exercises = [
            {
                "language": "Python",
                "title": "Test",
                "description": "desc",
                "difficulty": "easy",
                "solution_code": "def f(): pass",
            }
        ]
        gemini_response = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(mock_exercises)}]}}],
        }
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = gemini_response

        with patch("requests.post", return_value=mock_resp):
            result = generate_exercises_from_pdf_content(long_text, "Chapter", api_key="fake-key")

        assert result[0]["_gen_meta"]["was_truncated"] is True
        assert result[0]["_gen_meta"]["source_char_count"] == _TRUNCATION_LIMIT

    def test_source_content_type_passed_through(self):
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_exercises = [
            {"language": "Python", "title": "T", "description": "D", "difficulty": "easy",
             "solution_code": "def f(): pass"}
        ]
        gemini_response = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(mock_exercises)}]}}],
        }
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = gemini_response

        with patch("requests.post", return_value=mock_resp):
            result = generate_exercises_from_pdf_content(
                "text", "Chapter", api_key="fake-key", source_content_type="ipynb"
            )

        assert result[0]["_gen_meta"]["source_content_type"] == "ipynb"


# ===========================================================================
# blueprints/exercises: _classify_error_category
# ===========================================================================


class TestClassifyErrorCategory:
    def setup_method(self):
        from blueprints.exercises import _classify_error_category
        self.classify = _classify_error_category

    def test_all_pass_returns_none(self):
        results = [{"passed": True}, {"passed": True}]
        assert self.classify(results) == "none"

    def test_timeout_detected(self):
        results = [{"passed": False, "error": "Timed out after 5 s."}]
        assert self.classify(results) == "timeout"

    def test_syntax_error_detected(self):
        results = [{"passed": False, "error": "SyntaxError: invalid syntax on line 3"}]
        assert self.classify(results) == "syntax_error"

    def test_logic_error_when_output_differs(self):
        results = [{"passed": False, "actual": 42, "printed": None, "error": None}]
        assert self.classify(results) == "logic_error"

    def test_most_common_wins(self):
        results = [
            {"passed": False, "error": "SyntaxError: x"},
            {"passed": False, "error": "SyntaxError: y"},
            {"passed": False, "actual": 1, "error": None},
        ]
        assert self.classify(results) == "syntax_error"


# ===========================================================================
# blueprints/flywheel API endpoint tests
# ===========================================================================


class TestFlywheelEndpoints:
    def test_get_exercise_quality_requires_auth(self, client):
        resp = client.get("/api/exercises/ex_1/quality")
        assert resp.status_code == 401

    def test_get_exercise_quality_returns_404_when_no_score(self, client):
        with patch("blueprints.flywheel.get_db_connection", return_value=MagicMock()):
            with patch("blueprints.flywheel.flywheel_repo.get_quality_score", return_value=None):
                with patch("blueprints.flywheel.exercise_repo.get_exercise_owner", return_value="user_1"):
                    resp = client.get(
                        "/api/exercises/ex_1/quality",
                        headers={"X-Test-User-ID": "user_1", "X-Test-User-Role": "professor"},
                    )
        assert resp.status_code == 404

    def test_get_exercise_quality_returns_score(self, client):
        quality_data = {
            "exercise_id": "ex_1",
            "quality_score": 0.72,
            "quality_label": "good",
            "total_attempts": 20,
            "first_attempt_pass_rate": 0.45,
        }
        with patch("blueprints.flywheel.get_db_connection", return_value=MagicMock()):
            with patch("blueprints.flywheel.flywheel_repo.get_quality_score", return_value=quality_data):
                with patch("blueprints.flywheel.exercise_repo.get_exercise_owner", return_value="user_1"):
                    resp = client.get(
                        "/api/exercises/ex_1/quality",
                        headers={"X-Test-User-ID": "user_1", "X-Test-User-Role": "professor"},
                    )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["quality_label"] == "good"

    def test_get_class_exercise_quality_requires_professor(self, client):
        resp = client.get(
            "/api/classes/cls_1/exercise-quality",
            headers={"X-Test-User-ID": "user_1", "X-Test-User-Role": "student"},
        )
        assert resp.status_code == 403

    def test_flywheel_health_requires_superadmin(self, client):
        resp = client.get(
            "/api/flywheel/health",
            headers={"X-Test-User-ID": "user_1", "X-Test-User-Role": "professor"},
        )
        assert resp.status_code == 403

    def test_flywheel_health_returns_data(self, client):
        health_data = {
            "total_exercises": 100,
            "instrumented_exercises": 40,
            "coverage_pct": 40.0,
            "total_outcome_events": 500,
            "exercises_with_quality_scores": 20,
            "quality_distribution": {"good": 15, "too_hard": 5},
            "top_problematic": [],
        }
        with patch("blueprints.flywheel.get_db_connection", return_value=MagicMock()):
            with patch("blueprints.flywheel.flywheel_repo.get_flywheel_health", return_value=health_data):
                resp = client.get(
                    "/api/flywheel/health",
                    headers={"X-Test-User-ID": "admin_1", "X-Test-User-Role": "superadmin"},
                )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["coverage_pct"] == 40.0

    def test_generation_effectiveness_requires_superadmin(self, client):
        resp = client.get(
            "/api/flywheel/generation-effectiveness",
            headers={"X-Test-User-ID": "user_1", "X-Test-User-Role": "professor"},
        )
        assert resp.status_code == 403

    def test_compute_quality_requires_superadmin(self, client):
        resp = client.post(
            "/api/flywheel/compute-quality",
            headers={"X-Test-User-ID": "user_1", "X-Test-User-Role": "professor"},
        )
        assert resp.status_code == 403

    def test_compute_quality_runs_synchronously_in_test_mode(self, client, monkeypatch):
        import config

        monkeypatch.setattr("core.database.get_db_connection", lambda: MagicMock())
        # The route branches on config.FEATURE_ASYNC_TASKS, which config reads
        # from the environment when it is first imported -- that can happen during
        # collection, before conftest's session fixture sets FEATURE_ASYNC_TASKS.
        # Pin the attribute so the synchronous branch is exercised either way.
        monkeypatch.setattr(config, "FEATURE_ASYNC_TASKS", False)

        with patch("workers.tasks.compute_exercise_quality_scores", return_value={"exercises_scored": 3}):
            resp = client.post(
                "/api/flywheel/compute-quality",
                headers={"X-Test-User-ID": "admin_1", "X-Test-User-Role": "superadmin"},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "completed"
        assert data["exercises_scored"] == 3
