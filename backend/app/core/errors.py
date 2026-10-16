class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class PipelineError(Exception):
    """A failure with a message that is safe and useful to show the user."""


class Cancelled(Exception):
    pass
