"""Usage-aware ceilings preserve cache discounts without claiming invoice costs."""
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from weyra.contracts import Usage
from weyra.pricing import Price


def price(**changes):
    return Price.from_dict({
        "input_per_million": "3.57020110",
        "cache_read_per_million": "0.11899790",
        "output_per_million": "10.71061720",
        "cache_write_billing": "included",
        "cost_basis": "ceiling",
        **changes,
    })


def test_known_cache_hits_use_category_ceiling_without_claiming_actual_cost():
    tariff = price()
    usage = Usage(10000, 100, 9000, 0)
    expected = (Decimal(1000) * Decimal("3.57020110")
                + Decimal(9000) * Decimal("0.11899790")
                + Decimal(100) * Decimal("10.71061720")) / Decimal(1000000)
    assert tariff.upper_bound(usage) == expected
    assert tariff.upper_bound(usage) < tariff.estimate(10000, 100)
    assert tariff.actual(usage) is None
    assert tariff.cost_basis == "ceiling"


def test_unknown_cache_breakdown_retains_conservative_estimate():
    tariff = price()
    assert tariff.upper_bound(Usage(10000, 100)) == tariff.estimate(10000, 100)
    assert tariff.upper_bound(Usage(None, 100)) is None
    assert tariff.upper_bound(Usage(10000, None)) is None


def test_included_cache_writes_are_charged_once_as_ordinary_input():
    tariff = price()
    usage = Usage(10000, 0, 6000, 3000)
    expected = (Decimal(4000) * Decimal("3.57020110")
                + Decimal(6000) * Decimal("0.11899790")) / Decimal(1000000)
    assert tariff.upper_bound(usage) == expected


@pytest.mark.parametrize("read,write", [(-1, 0), (0, -1), (8, 3)])
def test_invalid_cache_counts_cannot_reduce_ceiling(read, write):
    usage = NS(input_tokens=10, output_tokens=1,
               cache_read_tokens=read, cache_write_tokens=write)
    with pytest.raises(ValueError):
        price().upper_bound(usage)


def test_exact_prices_keep_existing_actual_category_math():
    tariff = price(cost_basis="exact")
    usage = Usage(10000, 100, 9000, 0)
    expected = price().upper_bound(usage)
    assert tariff.actual(usage) == expected
    assert tariff.upper_bound(usage) == expected


def test_separately_charged_cache_writes_use_their_own_ceiling():
    tariff = price(cache_write_billing="separate", cache_write_per_million="5")
    usage = Usage(10000, 100, 6000, 3000)
    expected = (Decimal(1000) * Decimal("3.57020110")
                + Decimal(6000) * Decimal("0.11899790") + Decimal(3000) * 5
                + Decimal(100) * Decimal("10.71061720")) / Decimal(1000000)
    assert tariff.upper_bound(usage) == expected
    assert tariff.upper_bound(Usage(10000, 100, 6000)) == tariff.estimate(10000, 100)


def test_missing_required_cache_tariffs_still_cannot_claim_a_ceiling():
    tariff = price(cache_read_per_million=None)
    assert tariff.upper_bound(Usage(10, 1, 5, 0)) is None
