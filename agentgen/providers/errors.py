"""Provider-neutral errors. Every failure the engine can see from a model call is one of these,
with a message written for the person using the platform and a flag saying whether retrying
later can help. Provider SDK exceptions never leak past the provider."""


class ModelError(RuntimeError):
    retryable = False
    hint = ""

    def __init__(self, message: str, *, hint: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint
        self.retry_after = retry_after

    @property
    def user_message(self) -> str:
        return f"{self} {self.hint}".strip()


class ModelNotConfigured(ModelError):
    hint = "Set ANTHROPIC_API_KEY in the server's environment and restart the server."


class ModelAuthError(ModelError):
    hint = "Check that ANTHROPIC_API_KEY on the server is a valid, active key for an organisation with API access."


class ModelBillingError(ModelError):
    hint = "Check the billing status of the Anthropic account that owns the API key."


class ModelRateLimited(ModelError):
    retryable = True
    hint = "Wait a minute and try again."


class ModelTimeout(ModelError):
    retryable = True
    hint = "Try again. If it keeps happening, lower the agent's effort or token limits, or raise AGENTGEN_MODEL_TIMEOUT_SECONDS."


class ModelUnavailable(ModelError):
    retryable = True
    hint = "The model service is unavailable or overloaded. Try again shortly."


class ModelRequestError(ModelError):
    """The request was rejected as invalid (unknown model, request too large, unsupported option)."""

    hint = "Check the model name in Settings or the agent's configuration."


class ModelRefused(ModelError):
    hint = "The model declined this request. Rephrase the requirements or task."


class ModelOutputInvalid(ModelError):
    """The answer was cut off or did not match the expected structure."""

    retryable = True
    hint = "Try again. If it keeps happening, simplify the request or raise the token limit."


class BudgetExceeded(ModelError):
    hint = "Raise the monthly budget in Settings, or wait for the next month."
