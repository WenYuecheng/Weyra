"""Explicit CNY prices and exact conservative per-attempt cost estimates."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Price:
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None
    currency: str = "CNY"
    cost_basis: str = "exact"
    cache_write_billing: str = "separate"

    @classmethod
    def from_dict(cls, values):
        allowed = {
            "currency",
            "input_per_million",
            "output_per_million",
            "cache_read_per_million",
            "cache_write_per_million",
            "cost_basis",
            "cache_write_billing",
        }
        if set(values) - allowed or values.get("currency", "CNY") != "CNY":
            raise ValueError("Pricing requires explicit CNY rates")
        if not {"input_per_million", "output_per_million"} <= set(values):
            raise ValueError("Pricing requires input and output rates")
        basis = values.get("cost_basis", "exact")
        write_billing = values.get("cache_write_billing", "separate")
        if basis not in {"exact", "ceiling"} or write_billing not in {"separate", "included"}:
            raise ValueError("Invalid explicit billing policy")
        if write_billing == "included" and values.get("cache_write_per_million") is not None:
            raise ValueError("Included cache writes cannot have a separate tariff")
        rates = {}
        for key in allowed - {"currency", "cost_basis", "cache_write_billing"}:
            value = values.get(key)
            if value is None:
                rates[key] = None
                continue
            try:
                rate = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise ValueError("Invalid price rate") from None
            if not rate.is_finite() or rate < 0:
                raise ValueError("Price rates must be finite and nonnegative")
            rates[key] = rate
        return cls(**rates, cost_basis=basis, cache_write_billing=write_billing)

    def to_dict(self):
        return {
            "currency": self.currency,
            **{
                key: str(value)
                for key, value in vars(self).items()
                if key != "currency" and value is not None
            },
        }

    def estimate(self, input_tokens, output_tokens):
        rate = max(
            self.input_per_million,
            self.cache_read_per_million or self.input_per_million,
            self.cache_write_per_million or self.input_per_million,
        )
        return (
            Decimal(input_tokens) * rate + Decimal(output_tokens) * self.output_per_million
        ) / Decimal(1000000)

    def actual(self, usage):
        if self.cost_basis != "exact" or usage.input_tokens is None or usage.output_tokens is None:
            return None
        if (
            (self.cache_read_per_million is not None and usage.cache_read_tokens is None)
            or (self.cache_write_per_million is not None and usage.cache_write_tokens is None)
            or (
                usage.cache_write_tokens
                and self.cache_write_per_million is None
                and self.cache_write_billing != "included"
            )
            or (usage.cache_read_tokens and self.cache_read_per_million is None)
        ):
            return None
        read, write = usage.cache_read_tokens or 0, usage.cache_write_tokens or 0
        if min(read, write) < 0 or read + write > usage.input_tokens:
            raise ValueError("Cache usage exceeds total input tokens")
        uncached = (
            usage.input_tokens - read - (write if self.cache_write_billing == "separate" else 0)
        )
        cost = (
            Decimal(uncached) * self.input_per_million
            + Decimal(usage.output_tokens) * self.output_per_million
        )
        cost += Decimal(read) * (
            self.cache_read_per_million
            if self.cache_read_per_million is not None
            else self.input_per_million
        )
        cost += Decimal(write if self.cache_write_billing == "separate" else 0) * (
            self.cache_write_per_million
            if self.cache_write_per_million is not None
            else self.input_per_million
        )
        return cost / Decimal(1000000)

    def upper_bound(self, usage):
        if usage.input_tokens is None or usage.output_tokens is None:
            return None
        if (usage.cache_read_tokens and self.cache_read_per_million is None) or (
            usage.cache_write_tokens
            and self.cache_write_per_million is None
            and self.cache_write_billing != "included"
        ):
            return None
        return self.estimate(usage.input_tokens, usage.output_tokens)
