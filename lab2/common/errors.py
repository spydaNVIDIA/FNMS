"""Typed failure classes separate source failures, budgets and provider failures."""


class BudgetStop(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class SourceUnavailable(Exception):
    pass


class ProviderFailure(Exception):
    def __init__(self, code, message, retryable=False, retry_after=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.retry_after = retryable, retry_after
