"""Errors shared by the compute service."""

from __future__ import annotations

from typing import Optional

from determined_compute.core.api_client import APIError, SubmissionUncertainError

ComputeError = APIError


class _CodedComputeError(APIError):
    code = "compute_error"

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message, code=code or self.code, retryable=False)


class ValidationError(_CodedComputeError):
    code = "invalid_request"


class NotFoundError(_CodedComputeError):
    code = "not_found"


class ConflictError(_CodedComputeError):
    code = "conflict"


__all__ = [
    "APIError",
    "ComputeError",
    "ConflictError",
    "NotFoundError",
    "SubmissionUncertainError",
    "ValidationError",
]
