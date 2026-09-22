"""TypeSafe AI SDK adapter module.

Provides AsyncTypeSafe, Choice, and Noul classes designed for asynchronous
evaluation against TypeSafe AI System One decision models (such as Jev).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Union

logger = logging.getLogger("typesafe")

try:
    from typesafe_sdk import (
        AsyncTypeSafeClient as _SdkAsyncClient,
        Choice as _SdkChoice,
        Noul as _SdkNoul,
        TypeSafeError,
        TypeSafeAPIError,
        TypeSafeRateLimitError,
        TypeSafeAuthenticationError,
        TypeSafeBadRequestError,
        TypeSafePermissionDeniedError,
        TypeSafeNotFoundError,
        TypeSafeUnprocessableEntityError,
        TypeSafeInternalServerError,
        TypeSafeAPIConnectionError,
        TypeSafeAPITimeoutError,
    )
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False

    class TypeSafeError(Exception):
        """Base exception for TypeSafe API errors."""
        pass

    class TypeSafeAPIError(TypeSafeError):
        """An unsuccessful HTTP response with status code."""
        def __init__(
            self,
            status: int = 0,
            body: Any = None,
            headers: Any = None,
            message: Optional[str] = None,
            endpoint: Optional[str] = None,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            msg = message or f"HTTP status {status}"
            super().__init__(msg, *args)
            self.status = status
            self.body = body
            self.headers = headers
            self.endpoint = endpoint

    class TypeSafeRateLimitError(TypeSafeAPIError):
        def __init__(
            self,
            status: int = 429,
            body: Any = None,
            headers: Any = None,
            message: Optional[str] = None,
            endpoint: Optional[str] = None,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            super().__init__(status, body, headers, message, endpoint, *args, **kwargs)
            self.retry_after_ms: Optional[float] = None
            if headers and hasattr(headers, "get"):
                retry_header = headers.get("retry-after")
                if retry_header:
                    try:
                        self.retry_after_ms = float(retry_header) * 1000.0
                    except (ValueError, TypeError):
                        pass

    class TypeSafeAuthenticationError(TypeSafeAPIError):
        pass

    class TypeSafeBadRequestError(TypeSafeAPIError):
        pass

    class TypeSafePermissionDeniedError(TypeSafeAPIError):
        pass

    class TypeSafeNotFoundError(TypeSafeAPIError):
        pass

    class TypeSafeUnprocessableEntityError(TypeSafeAPIError):
        pass

    class TypeSafeInternalServerError(TypeSafeAPIError):
        pass

    class TypeSafeAPIConnectionError(TypeSafeError):
        pass

    class TypeSafeAPITimeoutError(TypeSafeAPIConnectionError):
        pass

import httpx


@dataclass
class Choice:
    """Represents a discrete choice question for TypeSafe System One."""
    id: str
    options: List[str] = field(default_factory=list)
    description: str = ""
    criteria: Optional[Mapping[str, Any]] = None


@dataclass
class Noul:
    """Represents a yes/no probability evaluation question for TypeSafe System One."""
    id: str
    description: str = ""
    criteria: Optional[Mapping[str, Any]] = None


class QuestionResult:
    """Encapsulates the answer and confidence metrics for a single evaluated question."""

    def __init__(
        self,
        question_id: str,
        question_type: str,
        raw_data: Mapping[str, Any],
    ) -> None:
        self.id = question_id
        self.type = question_type
        self.raw_data = dict(raw_data)

        # Choice attributes
        self.choice: Optional[str] = raw_data.get("choice")
        self.confidence: float = float(raw_data.get("confidence", 0.0))
        self.probabilities: Dict[str, float] = {
            k: float(v) for k, v in raw_data.get("probabilities", {}).items()
        }

        # Noul attributes
        self.noul: float = float(raw_data.get("noul", 0.0))

        # If it's a Noul question and confidence isn't set, assign noul probability
        if self.type == "noul" and "confidence" not in raw_data:
            self.confidence = self.noul

    def __getitem__(self, item: str) -> Any:
        return self.raw_data.get(item)

    def get(self, item: str, default: Any = None) -> Any:
        return self.raw_data.get(item, default)

    def __repr__(self) -> str:
        if self.type == "choice":
            return (
                f"QuestionResult(id='{self.id}', type='choice', "
                f"choice='{self.choice}', confidence={self.confidence:.3f})"
            )
        return (
            f"QuestionResult(id='{self.id}', type='noul', "
            f"noul={self.noul:.3f})"
        )


class TypeSafeEvaluationResponse:
    """Encapsulates the full evaluation response from TypeSafe System One."""

    def __init__(
        self,
        model: str,
        questions: Dict[str, QuestionResult],
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.model = model
        self.questions = questions
        self.answers = questions  # Alias for typesafe_sdk compatibility
        self.usage = usage or {}

    @property
    def choices(self) -> Dict[str, QuestionResult]:
        return {k: v for k, v in self.questions.items() if v.type == "choice"}

    @property
    def nouls(self) -> Dict[str, QuestionResult]:
        return {k: v for k, v in self.questions.items() if v.type == "noul"}

    def __getitem__(self, item: str) -> QuestionResult:
        return self.questions[item]

    def __repr__(self) -> str:
        return (
            f"TypeSafeEvaluationResponse(model='{self.model}', "
            f"questions={list(self.questions.keys())})"
        )


class AsyncTypeSafe:
    """Asynchronous client for interacting with the TypeSafe AI System One decision engine."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "jev-latest",
        base_url: str = "https://api.typesafe.ai",
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def evaluate(
        self,
        state: Union[str, Dict[str, Any], List[Any]],
        questions: List[Union[Choice, Noul]],
        model: Optional[str] = None,
    ) -> TypeSafeEvaluationResponse:
        """Evaluate a state string or structured object against a list of questions."""
        target_model = model or self.model

        # Build questions mapping
        questions_payload: Dict[str, Any] = {}
        for q in questions:
            if isinstance(q, Choice):
                criteria = q.criteria if q.criteria is not None else {opt: None for opt in q.options}
                questions_payload[q.id] = {
                    "type": "choice",
                    "instructions": q.description,
                    "criteria": criteria,
                }
            elif isinstance(q, Noul):
                noul_dict: Dict[str, Any] = {
                    "type": "noul",
                    "instructions": q.description,
                }
                if q.criteria is not None:
                    noul_dict["criteria"] = q.criteria
                questions_payload[q.id] = noul_dict
            else:
                raise TypeError(f"Unsupported question type: {type(q)}")

        # Try using typesafe-sdk if available
        if _HAS_SDK:
            try:
                sdk_questions: Dict[str, Any] = {}
                for q in questions:
                    if isinstance(q, Choice):
                        criteria = q.criteria if q.criteria is not None else {opt: None for opt in q.options}
                        sdk_questions[q.id] = _SdkChoice(
                            instructions=q.description,
                            criteria=criteria,
                        )
                    elif isinstance(q, Noul):
                        sdk_questions[q.id] = _SdkNoul(
                            instructions=q.description,
                            criteria=q.criteria,
                        )

                async with _SdkAsyncClient(
                    api_key=self.api_key or "placeholder_key",
                    model=target_model,
                    base_url=self.base_url,
                    timeout=self.timeout,
                ) as sdk_client:
                    sdk_response = await sdk_client.system_one(
                        state=state,
                        questions=sdk_questions,
                        model=target_model,
                    )

                results: Dict[str, QuestionResult] = {}
                for qid, ans in sdk_response.answers.items():
                    raw_dict: Dict[str, Any] = {"type": ans.type}
                    if hasattr(ans, "choice"):
                        raw_dict["choice"] = ans.choice
                    if hasattr(ans, "confidence"):
                        raw_dict["confidence"] = ans.confidence
                    if hasattr(ans, "probabilities"):
                        raw_dict["probabilities"] = getattr(ans, "probabilities", {})
                    if hasattr(ans, "noul"):
                        raw_dict["noul"] = ans.noul
                    results[qid] = QuestionResult(qid, ans.type, raw_dict)

                usage = {}
                if hasattr(sdk_response, "usage") and sdk_response.usage:
                    usage = {
                        "input_tokens": getattr(sdk_response.usage, "input_tokens", 0),
                        "output_tokens": getattr(sdk_response.usage, "output_tokens", 0),
                    }
                return TypeSafeEvaluationResponse(target_model, results, usage)

            except TypeSafeAPIError as exc:
                # Do NOT fall back on 4xx errors (e.g. 429 rate limits, 401 bad keys, 400 bad requests).
                # The SDK client has already retried if applicable. Falling back to raw HTTP
                # makes redundant requests, worsening rate limits and masking typed errors.
                if 400 <= getattr(exc, "status", 0) < 500:
                    raise
                logger.warning(
                    "TypeSafe SDK returned HTTP status %s, attempting direct HTTP fallback: %s",
                    getattr(exc, "status", "unknown"),
                    exc,
                )
            except (
                TypeSafeRateLimitError,
                TypeSafeAuthenticationError,
                TypeSafeBadRequestError,
                TypeSafePermissionDeniedError,
                TypeSafeNotFoundError,
                TypeSafeUnprocessableEntityError,
            ):
                raise
            except Exception as exc:
                # Fall back to direct HTTP call below for unexpected SDK internal/wrapper issues
                logger.warning(
                    "TypeSafe SDK client failed, attempting direct HTTP fallback: %s",
                    exc,
                )

        # Direct HTTP fallback
        url = f"{self.base_url}/v1/systemone"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "state": state,
            "model": target_model,
            "questions": questions_payload,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            status_code = getattr(resp, "status_code", 200)
            if isinstance(status_code, int) and status_code >= 400:
                self._handle_http_error(resp, url)
            if hasattr(resp, "raise_for_status") and callable(resp.raise_for_status):
                resp.raise_for_status()
            data = resp.json()
            if hasattr(data, "__await__"):
                data = await data

        answers_data = data.get("answers", {})
        results = {}
        for qid, ans in answers_data.items():
            results[qid] = QuestionResult(qid, ans.get("type", "unknown"), ans)

        return TypeSafeEvaluationResponse(
            model=data.get("model", target_model),
            questions=results,
            usage=data.get("usage", {}),
        )

    @staticmethod
    def _handle_http_error(resp: Any, url: str) -> None:
        """Map HTTP error response to structured TypeSafe exception."""
        status = int(getattr(resp, "status_code", 500))
        headers = getattr(resp, "headers", {})
        body = None
        message = ""
        try:
            raw_json = resp.json()
            if not hasattr(raw_json, "__await__"):
                body = raw_json
                if isinstance(body, dict) and "error" in body:
                    err_obj = body["error"]
                    message = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                else:
                    message = str(body)
        except Exception:
            pass

        if not message:
            message = getattr(resp, "text", "") or f"HTTP status {status}"

        if status == 401:
            raise TypeSafeAuthenticationError(status, body, headers, message=message, endpoint=url)
        elif status == 429:
            raise TypeSafeRateLimitError(status, body, headers, message=message, endpoint=url)
        elif status == 400:
            raise TypeSafeBadRequestError(status, body, headers, message=message, endpoint=url)
        elif status == 403:
            raise TypeSafePermissionDeniedError(status, body, headers, message=message, endpoint=url)
        elif status == 404:
            raise TypeSafeNotFoundError(status, body, headers, message=message, endpoint=url)
        elif status == 422:
            raise TypeSafeUnprocessableEntityError(status, body, headers, message=message, endpoint=url)
        elif status >= 500:
            raise TypeSafeInternalServerError(status, body, headers, message=message, endpoint=url)
        else:
            raise TypeSafeAPIError(status, body, headers, message=message, endpoint=url)


__all__ = [
    "AsyncTypeSafe",
    "Choice",
    "Noul",
    "QuestionResult",
    "TypeSafeEvaluationResponse",
    "TypeSafeError",
    "TypeSafeAPIError",
    "TypeSafeRateLimitError",
    "TypeSafeAuthenticationError",
    "TypeSafeBadRequestError",
    "TypeSafePermissionDeniedError",
    "TypeSafeNotFoundError",
    "TypeSafeUnprocessableEntityError",
    "TypeSafeInternalServerError",
    "TypeSafeAPIConnectionError",
    "TypeSafeAPITimeoutError",
]

