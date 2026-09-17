"""Unit tests for the typesafe SDK adapter (typesafe/__init__.py)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch
from typesafe import (
    AsyncTypeSafe,
    Choice,
    Noul,
    QuestionResult,
    TypeSafeEvaluationResponse,
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
