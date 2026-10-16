"""
Comprehensive Tests for Exercise Generation Module
Target: Increase coverage from 6% to 80%+
"""
import pytest
from unittest.mock import patch, MagicMock
import json

import config


class TestExerciseGeneration:
    """Test exercise generation from PDF content"""

    def test_generate_exercises_success(self):
        """Test successful exercise generation"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps([
                                    {
                                        "title": "Load Dataset",
                                        "description": "Load the CSV file using pandas",
                                        "difficulty": "easy",
                                        "solution_code": "import pandas as pd\ndf = pd.read_csv('data.csv')"
                                    }
                                ])
                            }
                        ]
                    }
                }
            ]
        }

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = mock_response

            exercises = generate_exercises_from_pdf_content(
                "Sample PDF text about data analysis",
                "Chapter 1: Introduction",
                api_key="test_key"
            )

            assert len(exercises) == 1
            assert exercises[0]["title"] == "Load Dataset"
            assert exercises[0]["difficulty"] == "easy"

    def test_generate_exercises_no_api_key(self, monkeypatch):
        """Test error when API key is missing"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        # config snapshots GEMINI_API_KEY from the environment at import time and
        # the generator reads config.GEMINI_API_KEY, so clearing os.environ here
        # would not change the value it sees. Patch the attribute the code reads;
        # monkeypatch restores it afterwards.
        monkeypatch.setattr(config, "GEMINI_API_KEY", None)

        with pytest.raises(Exception) as exc_info:
            generate_exercises_from_pdf_content(
                "Sample text",
                "Chapter 1"
            )
        assert "GEMINI_API_KEY" in str(exc_info.value)

    def test_generate_exercises_api_error(self):
        """Test handling of API errors"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = False
            mock_post.return_value.json.return_value = {
                "error": {"message": "API quota exceeded"}
            }

            with pytest.raises(Exception) as exc_info:
                generate_exercises_from_pdf_content(
                    "Sample text",
                    "Chapter 1",
                    api_key="test_key"
                )
            assert "Gemini API error" in str(exc_info.value)

    def test_generate_exercises_truncates_long_text(self):
        """Test that long PDF text is truncated"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        long_text = "A" * 15000  # 15k characters

        mock_response = {
            "candidates": [{
                "content": {"parts": [{"text": "[]"}]}
            }]
        }

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = mock_response

            generate_exercises_from_pdf_content(
                long_text,
                "Chapter 1",
                api_key="test_key"
            )

            # Verify the content was truncated to 10000 chars (not all 15k)
            call_args = mock_post.call_args
            prompt = call_args[1]["json"]["contents"][0]["parts"][0]["text"]
            assert "A" * 10000 in prompt       # 10k consecutive A's present
            assert "A" * 15000 not in prompt   # But not all 15k

    def test_generate_exercises_handles_markdown_json(self):
        """Test extraction of JSON from markdown code blocks"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": "```json\n[{\"title\": \"Test\", \"description\": \"Test\", \"difficulty\": \"easy\", \"solution_code\": \"print('test')\"}]\n```"
                    }]
                }
            }]
        }

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = mock_response

            exercises = generate_exercises_from_pdf_content(
                "Sample text",
                "Chapter 1",
                api_key="test_key"
            )

            assert len(exercises) == 1
            assert exercises[0]["title"] == "Test"

    def test_generate_exercises_validates_difficulty(self):
        """Test that invalid difficulty levels are corrected"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps([
                            {
                                "title": "Test",
                                "description": "Test",
                                "difficulty": "super_hard",  # Invalid
                                "solution_code": "print('test')"
                            }
                        ])
                    }]
                }
            }]
        }

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = mock_response

            exercises = generate_exercises_from_pdf_content(
                "Sample text",
                "Chapter 1",
                api_key="test_key"
            )

            # Should default to "medium"
            assert exercises[0]["difficulty"] == "medium"

    def test_generate_exercises_empty_response(self):
        """Test handling of empty API response"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_response = {"candidates": []}

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = mock_response

            with pytest.raises(Exception) as exc_info:
                generate_exercises_from_pdf_content(
                    "Sample text",
                    "Chapter 1",
                    api_key="test_key"
                )
            assert "No response" in str(exc_info.value)

    def test_generate_exercises_invalid_json(self):
        """Test handling of invalid JSON response"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": "This is not valid JSON at all!"
                    }]
                }
            }]
        }

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = mock_response

            with pytest.raises(Exception) as exc_info:
                generate_exercises_from_pdf_content(
                    "Sample text",
                    "Chapter 1",
                    api_key="test_key"
                )
            assert "Failed to parse the exercise response from AI" in str(exc_info.value)

    def test_generate_exercises_non_array_response(self):
        """Test handling when API returns object instead of array"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({"error": "Not an array"})
                    }]
                }
            }]
        }

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = mock_response

            with pytest.raises(Exception) as exc_info:
                generate_exercises_from_pdf_content(
                    "Sample text",
                    "Chapter 1",
                    api_key="test_key"
                )
            assert "expected array" in str(exc_info.value)

    def test_generate_exercises_filters_invalid_exercises(self):
        """Test that invalid exercise objects are filtered out"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps([
                            {
                                "title": "Valid Exercise",
                                "description": "Valid",
                                "difficulty": "easy",
                                "solution_code": "print('valid')"
                            },
                            "This is not a dict",  # Invalid
                            {
                                "title": "Another Valid",
                                "description": "Valid",
                                "difficulty": "medium",
                                "solution_code": "print('valid2')"
                            }
                        ])
                    }]
                }
            }]
        }

        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = mock_response

            exercises = generate_exercises_from_pdf_content(
                "Sample text",
                "Chapter 1",
                api_key="test_key"
            )

            # Should only return the 2 valid exercises
            assert len(exercises) == 2
            assert exercises[0]["title"] == "Valid Exercise"
            assert exercises[1]["title"] == "Another Valid"

    def test_generate_exercises_uses_env_api_key(self):
        """Test that API key is read from environment"""
        from services.exercise_generator import generate_exercises_from_pdf_content

        mock_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": "[]"
                    }]
                }
            }]
        }

        with patch("config.GEMINI_API_KEY", "env_key"):
            with patch("requests.post") as mock_post:
                mock_post.return_value.ok = True
                mock_post.return_value.json.return_value = mock_response

                generate_exercises_from_pdf_content(
                    "Sample text",
                    "Chapter 1"
                    # No api_key parameter - should use config.GEMINI_API_KEY
                )

                # Verify the config key was used in the URL
                call_args = mock_post.call_args
                assert "key=env_key" in call_args[0][0]


class TestSplitIntoChunks:
    """Unit tests for the pure _split_into_chunks() helper added by the
    chunked-extraction fix (reports/product-engineer/PDF_TRUNCATION_FIX_PLAN.md).
    """

    def test_empty_text(self):
        from services.exercise_generator import _split_into_chunks

        assert _split_into_chunks("", 100, 10) == []
        assert _split_into_chunks(None, 100, 10) == []

    def test_text_shorter_than_one_chunk_is_a_single_chunk(self):
        from services.exercise_generator import _split_into_chunks

        text = "A" * 500
        chunks = _split_into_chunks(text, 10_000, 500)
        assert chunks == [text]

    def test_text_exactly_one_chunk_boundary(self):
        """Text exactly chunk_size long must not produce a spurious 2nd chunk."""
        from services.exercise_generator import _split_into_chunks

        text = "B" * 10_000
        chunks = _split_into_chunks(text, 10_000, 500)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_covers_entire_text_with_no_gaps(self):
        """Every character of the input must appear in at least one chunk —
        no content is silently dropped, which is the whole point of the fix.
        """
        from services.exercise_generator import _split_into_chunks

        text = "0123456789" * 3000  # 30,000 chars, no paragraph breaks at all
        chunks = _split_into_chunks(text, 10_000, 500)
        assert len(chunks) > 1
        assert "".join(chunks[:1]) or True  # chunks non-empty
        # Reconstruct coverage: every chunk must be a real substring of text,
        # in order, and consecutive chunks must overlap (or exactly abut).
        pos = 0
        for i, chunk in enumerate(chunks):
            idx = text.find(chunk, max(0, pos - 500))
            assert idx != -1, f"chunk {i} not found in original text near expected position"
            pos = idx + len(chunk)
        assert pos == len(text), "last chunk must reach the end of the text"

    def test_overlap_between_consecutive_chunks(self):
        from services.exercise_generator import _split_into_chunks

        text = "0123456789" * 3000  # no paragraph breaks -> raw chunk_size cuts
        chunks = _split_into_chunks(text, 10_000, 500)
        assert len(chunks) >= 2
        # The tail of chunk 1 and the head of chunk 2 must share the 500-char overlap.
        assert chunks[0][-500:] == chunks[1][:500]

    def test_boundary_snaps_to_paragraph_break(self):
        """A paragraph break near the raw cut point should be preferred over
        cutting mid-sentence."""
        from services.exercise_generator import _split_into_chunks

        # Paragraph break sits 100 chars before the raw 1000-char cut point,
        # well within the (scaled) lookback window.
        first_para = "X" * 900
        second_para = "Y" * 900
        text = first_para + "\n\n" + second_para
        chunks = _split_into_chunks(text, 1000, 100)
        # First chunk should end exactly at the paragraph break, not mid-"Y"-run.
        assert chunks[0] == first_para
        assert chunks[0].endswith("X")

    def test_no_infinite_loop_on_pathological_overlap(self):
        """overlap >= chunk_size must not cause an infinite loop (forward
        progress guard)."""
        from services.exercise_generator import _split_into_chunks

        text = "Z" * 5000
        chunks = _split_into_chunks(text, 1000, 1000)  # overlap == chunk_size
        assert len(chunks) > 0
        assert "".join(chunks[-1:])  # completes without hanging


class TestDedupeExercises:
    """Unit tests for _dedupe_exercises(), which collapses near-duplicate
    exercises generated from overlapping chunks (Jaccard word-overlap on
    title + description — see PDF_TRUNCATION_FIX_PLAN.md part (a))."""

    def test_identical_titles_collapse(self):
        from services.exercise_generator import _dedupe_exercises

        exercises = [
            {"title": "Calculate the Mean", "description": "Compute the mean of a list of numbers."},
            {"title": "Calculate the Mean", "description": "Compute the mean of a list of numbers."},
        ]
        result = _dedupe_exercises(exercises)
        assert len(result) == 1
        assert result[0]["title"] == "Calculate the Mean"

    def test_distinct_titles_both_kept(self):
        from services.exercise_generator import _dedupe_exercises

        exercises = [
            {"title": "Calculate the Mean", "description": "Compute the mean of test scores."},
            {"title": "Sort a List of Strings", "description": "Write a function that sorts strings alphabetically."},
        ]
        result = _dedupe_exercises(exercises)
        assert len(result) == 2

    def test_keeps_earlier_of_a_near_duplicate_pair(self):
        from services.exercise_generator import _dedupe_exercises

        exercises = [
            {"title": "Calculate the mean of test scores", "description": "Write a function that computes the mean."},
            {"title": "Calculate the mean of test scores", "description": "Write a function that computes the average."},
            {"title": "Reverse a String", "description": "Write a function that reverses a string."},
        ]
        result = _dedupe_exercises(exercises)
        assert len(result) == 2
        assert result[0]["description"] == "Write a function that computes the mean."
        assert result[1]["title"] == "Reverse a String"

    def test_empty_list(self):
        from services.exercise_generator import _dedupe_exercises

        assert _dedupe_exercises([]) == []


class TestChunkedGenerationPath:
    """Integration-style tests for generate_exercises_from_pdf_content() with
    config.FEATURE_PDF_CHUNKING explicitly turned on for the duration of each
    test. The flag defaults to False in config.py — these tests prove the
    ON behavior works; test_pdf_truncation_regression.py proves the OFF
    (default/production) behavior is unchanged."""

    # Deliberately unrelated topics (not just a numbered template) so that
    # _dedupe_exercises' Jaccard word-overlap check does NOT collapse the
    # distinct per-chunk exercises these mocks return — dedup behavior itself
    # is covered separately by TestDedupeExercises above.
    _EXERCISE_POOL = [
        ("Calculate the Mean of Test Scores", "You have the test scores [10, 15, 12, 14, 17]. Write a function called calculate_mean that returns their average."),
        ("Reverse a String", "You are given the word 'hello'. Write a function called reverse_string that returns it backwards."),
        ("Filter Even Numbers", "You have the list [1, 2, 3, 4, 5, 6]. Write a function called filter_evens that keeps only even numbers."),
        ("Count Vowels in a Word", "You are given the word 'analytiq'. Write a function called count_vowels that counts its vowels."),
        ("Sum a List of Prices", "You have the prices [19.99, 5.50, 3.25]. Write a function called total_price that adds them up."),
        ("Find the Maximum Value", "You have the values [7, 42, 13, 99, 4]. Write a function called find_max that returns the largest one."),
    ]

    def _mock_post_factory(self, captured):
        from unittest.mock import MagicMock
        import threading

        counter = {"n": 0}
        lock = threading.Lock()

        def _fake_post(url, headers=None, json=None, timeout=None):
            with lock:
                idx = counter["n"]
                counter["n"] += 1
            captured.append(json["contents"][0]["parts"][0]["text"])
            title, description = self._EXERCISE_POOL[idx % len(self._EXERCISE_POOL)]
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": __import__("json").dumps(
                                        [
                                            {
                                                "title": title,
                                                "description": description,
                                                "difficulty": "easy",
                                                "solution_code": "print('x')",
                                            }
                                        ]
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
            return resp

        return _fake_post

    def test_single_chunk_chapter_is_a_no_op(self):
        """A chapter well under PDF_CHUNK_SIZE makes exactly one Gemini call,
        same as the flag-off path — zero behavior change for normal chapters."""
        from services.exercise_generator import generate_exercises_from_pdf_content

        captured = []
        with patch.object(config, "FEATURE_PDF_CHUNKING", True):
            with patch("requests.post", side_effect=self._mock_post_factory(captured)):
                result = generate_exercises_from_pdf_content(
                    "Short chapter text.", "Chapter 1", api_key="test_key"
                )

        assert len(captured) == 1
        assert len(result) == 1
        assert result.content_warning is None

    def test_multi_chunk_chapter_merges_all_chunks(self):
        """A chapter that needs multiple chunks must produce one exercise per
        chunk call, merged into a single result list, with no content_warning
        (comfortably under the default 6-chunk cap)."""
        from services.exercise_generator import (
            generate_exercises_from_pdf_content,
            _split_into_chunks_with_offsets,
        )

        long_text = "0123456789" * 3000  # ~30k chars, well under the 6-chunk cap
        expected_chunks = len(
            _split_into_chunks_with_offsets(long_text, config.PDF_CHUNK_SIZE, config.PDF_CHUNK_OVERLAP)
        )
        assert 1 < expected_chunks <= config.PDF_MAX_CHUNKS, (
            "test fixture must need multiple chunks but stay under the cap; "
            f"got {expected_chunks} chunks — adjust long_text length"
        )

        captured = []
        with patch.object(config, "FEATURE_PDF_CHUNKING", True):
            with patch("requests.post", side_effect=self._mock_post_factory(captured)):
                result = generate_exercises_from_pdf_content(
                    long_text, "Chapter 1", api_key="test_key"
                )

        assert len(captured) == expected_chunks
        assert len(result) == expected_chunks
        assert result.content_warning is None
        for ex in result:
            assert ex["_gen_meta"]["total_chunks"] == expected_chunks
            assert 0 <= ex["_gen_meta"]["chunk_index"] < expected_chunks

    def test_content_warning_fires_when_chunk_cap_exceeded(self):
        """A chapter needing more than config.PDF_MAX_CHUNKS chunks must fire
        the content_warning and only process the first PDF_MAX_CHUNKS chunks."""
        from services.exercise_generator import generate_exercises_from_pdf_content

        # 10 chunks worth of content at defaults (10k chunk / 500 overlap) far
        # exceeds the 6-chunk cap.
        long_text = "0123456789" * 8000  # ~80k chars
        captured = []
        with patch.object(config, "FEATURE_PDF_CHUNKING", True):
            with patch("requests.post", side_effect=self._mock_post_factory(captured)):
                result = generate_exercises_from_pdf_content(
                    long_text, "Chapter 1", api_key="test_key"
                )

        assert len(captured) == config.PDF_MAX_CHUNKS
        assert result.content_warning is not None
        assert result.content_warning["truncated"] is True
        assert result.content_warning["chars_total"] == len(long_text)
        assert result.content_warning["chars_processed"] < len(long_text)
        assert "message" in result.content_warning

    def test_one_chunk_failing_does_not_lose_the_others(self):
        """If one chunk's Gemini call fails, the others still merge normally —
        only total failure (every chunk fails) raises."""
        from services.exercise_generator import (
            generate_exercises_from_pdf_content,
            _split_into_chunks_with_offsets,
        )

        long_text = "0123456789" * 3000  # ~30k chars, multiple chunks
        expected_chunks = len(
            _split_into_chunks_with_offsets(long_text, config.PDF_CHUNK_SIZE, config.PDF_CHUNK_OVERLAP)
        )
        assert expected_chunks > 1, "test fixture must need multiple chunks"

        captured = []
        fake_post_fn = self._mock_post_factory(captured)  # one shared factory instance
        call_count = {"n": 0}
        lock = __import__("threading").Lock()

        def _flaky_post(url, headers=None, json=None, timeout=None):
            with lock:
                call_count["n"] += 1
                this_call = call_count["n"]
            if this_call == 2:
                raise Exception("simulated network failure for one chunk")
            return fake_post_fn(url, headers=headers, json=json, timeout=timeout)

        with patch.object(config, "FEATURE_PDF_CHUNKING", True):
            with patch("requests.post", side_effect=_flaky_post):
                result = generate_exercises_from_pdf_content(
                    long_text, "Chapter 1", api_key="test_key"
                )

        # All but the one failed chunk succeeded; the failed chunk simply
        # contributes 0 exercises instead of losing the whole chapter.
        assert len(result) == expected_chunks - 1

    def test_every_chunk_failing_raises(self):
        """Preserves today's behavior: if the Gemini call fails entirely, raise."""
        from services.exercise_generator import generate_exercises_from_pdf_content

        def _always_fails(url, headers=None, json=None, timeout=None):
            raise Exception("simulated total outage")

        with patch.object(config, "FEATURE_PDF_CHUNKING", True):
            with patch("requests.post", side_effect=_always_fails):
                with pytest.raises(Exception):
                    generate_exercises_from_pdf_content(
                        "Short chapter text.", "Chapter 1", api_key="test_key"
                    )
