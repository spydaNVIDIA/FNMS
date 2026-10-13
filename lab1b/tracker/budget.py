"""Hard runtime budgets that the model cannot alter."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import TrackerConfig
from .errors import BudgetExceeded
from .types import Failure


@dataclass
class BudgetUsage:
    steps: int = 0
    searches: int = 0
    fetches: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    credits: float = 0
    cost_usd: float = 0


class Budget:
    def __init__(self, config: TrackerConfig):
        self.config = config
        self.usage = BudgetUsage()

    def _check(self, value: float, limit: float, name: str) -> None:
        if value > limit:
            raise BudgetExceeded(Failure("budget", False, f"{name} budget exhausted"))

    def start_step(self) -> None:
        self._check(self.usage.steps + 1, self.config.limits.max_steps, "step")
        self.usage.steps += 1

    def before_model(self, estimated_input_tokens: int, reserved_output_tokens: int) -> None:
        self._check(
            self.usage.model_calls + 1, self.config.limits.max_model_calls, "model call"
        )
        self._check(
            self.usage.input_tokens + estimated_input_tokens,
            self.config.limits.max_input_tokens,
            "input token",
        )
        self._check(
            self.usage.output_tokens + reserved_output_tokens,
            self.config.limits.max_output_tokens,
            "output token",
        )
        input_rate = float(self.config.model.get("input_cost_per_million", 0))
        output_rate = float(self.config.model.get("output_cost_per_million", 0))
        estimated_cost = (
            estimated_input_tokens * input_rate + reserved_output_tokens * output_rate
        ) / 1_000_000
        self._check(
            self.usage.cost_usd + estimated_cost,
            self.config.limits.max_cost_usd,
            "cost",
        )
        self.usage.model_calls += 1

    def record_model(self, input_tokens: int, output_tokens: int) -> float:
        input_rate = float(self.config.model.get("input_cost_per_million", 0))
        output_rate = float(self.config.model.get("output_cost_per_million", 0))
        cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.usage.cost_usd += cost
        self._check(self.usage.input_tokens, self.config.limits.max_input_tokens, "input token")
        self._check(self.usage.output_tokens, self.config.limits.max_output_tokens, "output token")
        self._check(self.usage.cost_usd, self.config.limits.max_cost_usd, "cost")
        return cost

    def before_search(self, credits: float = 1) -> None:
        self._check(self.usage.searches + 1, self.config.limits.max_searches, "search")
        self._check(self.usage.credits + credits, self.config.limits.max_credits, "credit")
        self.usage.searches += 1
        self.usage.credits += credits

    def before_fetch(self) -> None:
        self._check(self.usage.fetches + 1, self.config.limits.max_fetches, "fetch")
        self.usage.fetches += 1

    def as_dict(self) -> dict:
        return asdict(self.usage)
