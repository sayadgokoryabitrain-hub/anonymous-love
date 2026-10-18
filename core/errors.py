from __future__ import annotations


class AppError(Exception):
    status_code = 400

    def __init__(self, code: str, message_key: str, *, status_code: int | None = None, **params: object) -> None:
        super().__init__(code)
        self.code = code
        self.message_key = message_key
        self.params = params
        if status_code is not None:
            self.status_code = status_code


class InsufficientFunds(AppError):
    def __init__(self) -> None:
        super().__init__("insufficient_funds", "wallet.insufficient")


class NotVerified(AppError):
    def __init__(self) -> None:
        super().__init__("not_verified", "verify.locked")


class AuthError(AppError):
    def __init__(self, code: str = "unauthorized") -> None:
        super().__init__(code, "errors.not_found", status_code=401)
