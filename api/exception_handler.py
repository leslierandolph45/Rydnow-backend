"""DRF exception handler: one consistent error envelope for the whole API.

Response shape:
    {"error": {"code": "...", "message": "...", "details": {...}}}

Enable in settings.py:
    REST_FRAMEWORK = {
        "EXCEPTION_HANDLER": "api.exception_handler.rydnow_exception_handler",
    }
"""
import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .exceptions import RateLimitExceededError, RydnowError

logger = logging.getLogger(__name__)


def rydnow_exception_handler(exc, context):
    view = context.get("view")
    view_name = type(view).__name__ if view else "unknown"

    # Our own domain errors
    if isinstance(exc, RydnowError):
        log = logger.error if exc.http_status >= 500 else logger.info
        log(
            "domain_error",
            extra={"code": exc.code, "view": view_name, "status": exc.http_status},
        )
        headers = {}
        if isinstance(exc, RateLimitExceededError) and exc.retry_after:
            headers["Retry-After"] = str(exc.retry_after)
        return Response({"error": exc.to_dict()}, status=exc.http_status, headers=headers)

    # DRF's own errors (validation, auth, 404...) -> same envelope
    response = drf_exception_handler(exc, context)
    if response is not None:
        data = response.data
        message = data.get("detail") if isinstance(data, dict) else None
        response.data = {
            "error": {
                "code": getattr(getattr(exc, "default_code", None), "__str__", lambda: "error")(),
                "message": str(message) if message else "Request failed.",
                **({"details": data} if not message else {}),
            }
        }
        return response

    # Unhandled -> let Django return 500 (and your error tracker catch it)
    logger.exception("unhandled_exception", extra={"view": view_name})
    return None
