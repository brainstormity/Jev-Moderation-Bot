"""Unit tests for the typesafe SDK adapter (typesafe/__init__.py)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typesafe import (
    AsyncTypeSafe,
    Choice,
    Noul,
    QuestionResult,
    TypeSafeEvaluationResponse,
    TypeSafeError,
    TypeSafeAPIError,
    TypeSafeRateLimitError,
    TypeSafeAuthenticationError,
)


def test_choice_and_noul_dataclasses():
    c = Choice(
        id="spam_classification",
        options=["LEGITIMATE", "SPAM", "SCAM_LINK"],
        description="Classify message content",
    )
    assert c.id == "spam_classification"
    assert len(c.options) == 3
    assert c.description == "Classify message content"
    assert c.criteria is None

    n = Noul(
        id="requires_immediate_ban",
        description="Is this an explicit malicious scam attempt?",
    )
    assert n.id == "requires_immediate_ban"
    assert n.description == "Is this an explicit malicious scam attempt?"


def test_question_result_attributes():
    # Choice result
    raw_choice = {
        "type": "choice",
        "choice": "SCAM_LINK",
        "confidence": 0.98,
        "probabilities": {"LEGITIMATE": 0.01, "SPAM": 0.01, "SCAM_LINK": 0.98},
    }
    q_choice = QuestionResult("test_choice", "choice", raw_choice)
    assert q_choice.id == "test_choice"
    assert q_choice.choice == "SCAM_LINK"
    assert q_choice.confidence == 0.98
    assert q_choice.probabilities["SCAM_LINK"] == 0.98
    assert q_choice["choice"] == "SCAM_LINK"
    assert repr(q_choice).startswith("QuestionResult(id='test_choice'")

    # Noul result
    raw_noul = {
        "type": "noul",
        "noul": 0.95,
    }
    q_noul = QuestionResult("test_noul", "noul", raw_noul)
    assert q_noul.id == "test_noul"
    assert q_noul.noul == 0.95
    assert q_noul.confidence == 0.95
    assert repr(q_noul).startswith("QuestionResult(id='test_noul'")


def test_evaluation_response_properties():
    q_choice = QuestionResult("spam", "choice", {"choice": "SPAM", "confidence": 0.85})
    q_noul = QuestionResult("ban", "noul", {"noul": 0.20})

    resp = TypeSafeEvaluationResponse(
        model="jev-latest",
        questions={"spam": q_choice, "ban": q_noul},
        usage={"input_tokens": 100, "output_tokens": 20},
    )

    assert resp.model == "jev-latest"
    assert resp.questions["spam"].choice == "SPAM"
    assert resp.answers["ban"].noul == 0.20
    assert "spam" in resp.choices
    assert "ban" in resp.nouls
    assert resp["spam"] is q_choice
    assert resp.usage["input_tokens"] == 100


@pytest.mark.asyncio
async def test_async_typesafe_evaluate_http_fallback():
    client = AsyncTypeSafe(api_key="mock_api_key")

    mock_http_response = {
        "model": "jev-latest",
        "answers": {
            "spam_classification": {
                "type": "choice",
                "choice": "SCAM_LINK",
                "confidence": 0.99,
                "probabilities": {"LEGITIMATE": 0.0, "SPAM": 0.01, "SCAM_LINK": 0.99},
            },
            "requires_immediate_ban": {
                "type": "noul",
                "noul": 0.96,
            },
        },
        "usage": {"input_tokens": 150, "output_tokens": 35},
    }

    questions = [
        Choice(
            id="spam_classification",
            options=["LEGITIMATE", "SPAM", "SCAM_LINK"],
            description="Classify message",
        ),
        Noul(
            id="requires_immediate_ban",
            description="Require immediate ban?",
        ),
    ]

    with patch("typesafe._HAS_SDK", False):
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_resp = AsyncMock()
            mock_resp.json.return_value = mock_http_response
            mock_resp.raise_for_status = lambda: None
            mock_post.return_value = mock_resp

            resp = await client.evaluate("Test state message", questions)

            assert resp.model == "jev-latest"
            assert resp.questions["spam_classification"].choice == "SCAM_LINK"
            assert resp.questions["spam_classification"].confidence == 0.99
            assert resp.questions["requires_immediate_ban"].noul == 0.96
            assert resp.usage["input_tokens"] == 150


@pytest.mark.asyncio
async def test_async_typesafe_rate_limit_no_fallback():
    client = AsyncTypeSafe(api_key="mock_api_key")
    questions = [Choice(id="spam_classification", options=["LEGITIMATE", "SPAM"])]

    rate_limit_err = TypeSafeRateLimitError(
        status=429,
        body={"error": "Rate limit exceeded"},
        headers={"retry-after": "30"},
        message="Too Many Requests",
        endpoint="https://api.typesafe.ai/v1/systemone",
    )

    with patch("typesafe._HAS_SDK", True):
        with patch("typesafe._SdkAsyncClient") as mock_sdk_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.__aenter__.return_value = mock_client_instance
            mock_client_instance.system_one.side_effect = rate_limit_err
            mock_sdk_client_cls.return_value = mock_client_instance

            with patch("httpx.AsyncClient.post") as mock_http_post:
                with pytest.raises(TypeSafeRateLimitError) as exc_info:
                    await client.evaluate("Test spam message", questions)

                assert exc_info.value.status == 429
                # Verify that HTTP fallback was NEVER invoked on 429!
                mock_http_post.assert_not_called()


@pytest.mark.asyncio
async def test_async_typesafe_auth_error_no_fallback():
    client = AsyncTypeSafe(api_key="bad_api_key")
    questions = [Choice(id="spam_classification", options=["LEGITIMATE", "SPAM"])]

    auth_err = TypeSafeAuthenticationError(
        status=401,
        body={"error": "Invalid API key"},
        headers={},
        message="Unauthorized",
        endpoint="https://api.typesafe.ai/v1/systemone",
    )

    with patch("typesafe._HAS_SDK", True):
        with patch("typesafe._SdkAsyncClient") as mock_sdk_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.__aenter__.return_value = mock_client_instance
            mock_client_instance.system_one.side_effect = auth_err
            mock_sdk_client_cls.return_value = mock_client_instance

            with patch("httpx.AsyncClient.post") as mock_http_post:
                with pytest.raises(TypeSafeAuthenticationError) as exc_info:
                    await client.evaluate("Test spam message", questions)

                assert exc_info.value.status == 401
                # Verify that HTTP fallback was NEVER invoked on 401!
                mock_http_post.assert_not_called()


@pytest.mark.asyncio
async def test_async_typesafe_unexpected_error_falls_back_to_http():
    client = AsyncTypeSafe(api_key="mock_api_key")
    questions = [Choice(id="spam_classification", options=["LEGITIMATE", "SPAM"])]

    mock_http_response = {
        "model": "jev-latest",
        "answers": {
            "spam_classification": {
                "type": "choice",
                "choice": "LEGITIMATE",
                "confidence": 0.95,
            },
        },
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }

    with patch("typesafe._HAS_SDK", True):
        with patch("typesafe._SdkAsyncClient") as mock_sdk_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.__aenter__.return_value = mock_client_instance
            # Raise non-4xx exception (e.g. internal SDK serialization/runtime issue)
            mock_client_instance.system_one.side_effect = RuntimeError("SDK internal serialization error")
            mock_sdk_client_cls.return_value = mock_client_instance

            with patch("httpx.AsyncClient.post") as mock_http_post:
                mock_resp = AsyncMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = mock_http_response
                mock_resp.raise_for_status = lambda: None
                mock_http_post.return_value = mock_resp

                resp = await client.evaluate("Test message", questions)

                assert resp.model == "jev-latest"
                assert resp.questions["spam_classification"].choice == "LEGITIMATE"
                # Verify that HTTP fallback WAS called!
                mock_http_post.assert_called_once()


@pytest.mark.asyncio
async def test_http_fallback_maps_401_and_429_to_typed_exceptions():
    client = AsyncTypeSafe(api_key="mock_api_key")
    questions = [Choice(id="spam_classification", options=["LEGITIMATE", "SPAM"])]

    # Test 401 mapping
    with patch("typesafe._HAS_SDK", False):
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_resp_401 = AsyncMock()
            mock_resp_401.status_code = 401
            mock_resp_401.headers = {}
            mock_resp_401.json = MagicMock(return_value={"error": {"message": "Invalid credentials"}})
            mock_resp_401.text = '{"error": {"message": "Invalid credentials"}}'
            mock_post.return_value = mock_resp_401

            with pytest.raises(TypeSafeAuthenticationError) as exc_info:
                await client.evaluate("Test message", questions)
            assert exc_info.value.status == 401

    # Test 429 mapping
    with patch("typesafe._HAS_SDK", False):
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_resp_429 = AsyncMock()
            mock_resp_429.status_code = 429
            mock_resp_429.headers = {"retry-after": "60"}
            mock_resp_429.json = MagicMock(return_value={"error": {"message": "Rate limit reached"}})
            mock_resp_429.text = '{"error": {"message": "Rate limit reached"}}'
            mock_post.return_value = mock_resp_429

            with pytest.raises(TypeSafeRateLimitError) as exc_info:
                await client.evaluate("Test message", questions)
            assert exc_info.value.status == 429
            assert getattr(exc_info.value, "retry_after_ms", None) == 60000.0

