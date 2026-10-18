from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from gj_common.logging import current_request_id, get_logger

log = get_logger(__name__)


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable code, e.g. `cart_invalid`, `insufficient_credits`",
                      examples=["not_found"])
    message: str = Field(description="Human-readable message, safe to show to users", examples=["Order not found"])
    details: Any = Field(None, description="Extra context, e.g. validation errors or the cart problems list")


class ErrorResponse(BaseModel):
    """Every non-2xx response has this shape."""

    error: ErrorBody
    request_id: str | None = Field(None, description="Quote this to support; also in the `X-Request-ID` header")


ERROR_RESPONSES: dict = {
    "4XX": {"model": ErrorResponse, "description": "Client error — see `error.code`"},
    "5XX": {"model": ErrorResponse, "description": "Server or upstream error"},
}


class AppError(Exception):
    status_code = 400
    code = "bad_request"
    message = "Bad request"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        super().__init__(self.message)


class NotFound(AppError):
    status_code, code, message = 404, "not_found", "Resource not found"


class Conflict(AppError):
    status_code, code, message = 409, "conflict", "Conflict"


class Unauthorized(AppError):
    status_code, code, message = 401, "unauthorized", "Authentication required"


class Forbidden(AppError):
    status_code, code, message = 403, "forbidden", "Not allowed"


class PaymentRequired(AppError):
    status_code, code, message = 402, "insufficient_credits", "Not enough credits"


class ValidationFailed(AppError):
    status_code, code, message = 422, "validation_failed", "Validation failed"


class TooManyRequests(AppError):
    status_code, code, message = 429, "rate_limited", "Too many requests"


class UpstreamError(AppError):
    status_code, code, message = 502, "upstream_error", "Upstream service failed"


def _body(code: str, message: str, details: Any = None) -> dict:
    return {
        "error": {"code": code, "message": message, "details": jsonable_encoder(details)},
        "request_id": current_request_id(),
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(_body("validation_failed", "Request validation failed", exc.errors()), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return JSONResponse(_body("http_error", str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(_body("internal_error", "Internal server error"), status_code=500)
