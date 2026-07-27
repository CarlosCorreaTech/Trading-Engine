"""Outcome models, one per action type.

Each model answers the same question: if we take this action instead of doing
nothing, how much extra gross profit results, and how uncertain is that? The
counterfactual matters. Simulating the outcome of the action alone would
attribute profit that would have happened anyway.
"""

from __future__ import annotations

import numpy as np

from src.config import AUTONOMY_GATE, RECOMMENDATION_RULES, SIMULATION
from src.recommendations.recommendation import ActionType, Recommendation
from src.simulation.monte_carlo import (
    bootstrap_mean,
    block_bootstrap,
    draw_lead_time,
    draw_spend_elasticity,
)
from src.simulation.outcome import SimulationResult, summarise
from src.warehouse import query

BUDGET_HORIZON_DAYS = 90
INVENTORY_HORIZON_DAYS = 180

# Annual cost of holding stock: tied-up capital, storage, and the expiry risk
# that applies to supplements. Applied to whatever is left unsold at the end of
# the horizon.
INVENTORY_HOLDING_COST_ANNUAL = 0.25

# Probability that a recently surged product's demand reverts toward its
# pre-surge level within the planning horizon.
#
# This is an assumption, and it exists because bootstrapping recent history
# cannot produce it. Resampling the last 90 days can only generate futures that
# look like the recent past, so a model built on it will never simulate the one
# scenario that makes a large stock purchase dangerous: that the surge it was
# sized against was temporary. Without this term every restock returns a 100%
# probability of profit, which is a property of the method rather than of the
# world.
#
# 15% for a trend that has held about five months. Nothing in the data supports
# a precise figure; the purpose is to stop the simulation asserting a certainty
# it has no basis for, and to make the downside visible to the autonomy gate.
DEMAND_REVERSION_PROBABILITY = 0.15

STRESS = AUTONOMY_GATE["stress"]


def simulate_budget_reallocation(
    recommendation: Recommendation, con, rng: np.random.Generator, stress: bool = False
) -> SimulationResult:
    """Incremental gross profit from moving spend between two channels.

    Structure: the shifted money buys customers on the destination channel at a
    cost that worsens as spend rises, and stops buying them on the source
    channel at its current cost. The profit difference is the outcome.

    One deliberate conservatism: customers lost from the source are priced at
    its *average* cost per acquisition. In reality the budget cut comes off the
    least efficient spend, so the true marginal cost is higher and fewer
    customers are lost than modelled. The simulation therefore understates the
    benefit, which is the right direction to be wrong in.

    Under `stress` the two assumptions the recommendation lists as caveats are
    set against it simultaneously: Google wins no extra customers for the extra
    money, and every unattributed order turns out to have been Meta's.
    """
    params = recommendation.parameters
    n = SIMULATION["n_draws"]
    lookback = SIMULATION["bootstrap_window_days"]

    daily = query(
        con,
        f"""
        SELECT channel, date_day, cac_attributed_7d, cac_with_unattributed_7d,
               new_customer_gross_profit, new_customers
        FROM semantic.channel_daily_performance
        WHERE channel IN ('Meta', 'Google')
          AND date_day > (SELECT max(date_day) - {lookback} FROM semantic.channel_daily_performance)
        ORDER BY channel, date_day
        """,
    )
    source = daily[daily["channel"] == params["source_channel"]]
    destination = daily[daily["channel"] == params["destination_channel"]]

    def contribution_series(frame):
        customers = frame["new_customers"].to_numpy(dtype=float)
        profit = frame["new_customer_gross_profit"].to_numpy(dtype=float)
        return np.divide(profit, customers, out=np.zeros_like(profit), where=customers > 0)

    # Attribution uncertainty. 27% of orders have no usable referrer, so each
    # channel's true acquisition cost lies somewhere between the pessimistic
    # bound (credit only what is provable) and the optimistic one (share out the
    # unattributed pool). A single weight is drawn per iteration and applied to
    # both channels, because the same missing-referrer mechanism affects both;
    # drawing independently would invent a scenario where attribution is broken
    # for Meta and intact for Google, which is not how it fails. The
    # consequence, visible in the output, is that attribution error largely
    # cancels in a comparison, which is why the recommendation is robust to it.
    attribution_weight = rng.uniform(0.0, 1.0, size=n)

    # Under stress, attribution is resolved in the direction that hurts: the
    # source channel gets the optimistic bound (its cheapest defensible
    # acquisition cost) and the destination gets the pessimistic one.
    source_weight = 0.0 if stress else attribution_weight
    destination_weight = 1.0 if stress else attribution_weight

    def blended_cac(frame, weight) -> np.ndarray:
        pessimistic = bootstrap_mean(
            frame["cac_attributed_7d"].to_numpy(dtype=float), n, BUDGET_HORIZON_DAYS, rng
        )
        optimistic = bootstrap_mean(
            frame["cac_with_unattributed_7d"].to_numpy(dtype=float), n, BUDGET_HORIZON_DAYS, rng
        )
        return weight * pessimistic + (1 - weight) * optimistic

    source_cac = blended_cac(source, source_weight)
    destination_cac = blended_cac(destination, destination_weight)
    source_contribution = bootstrap_mean(
        contribution_series(source), n, BUDGET_HORIZON_DAYS, rng
    )
    destination_contribution = bootstrap_mean(
        contribution_series(destination), n, BUDGET_HORIZON_DAYS, rng
    )

    # Drawn even when the value is about to be overridden, so that the stressed
    # run consumes the generator in exactly the same order as the nominal one.
    # The two then differ only by the assumption under test, not by sampling
    # noise, which is what makes the comparison between them meaningful.
    elasticity = draw_spend_elasticity(n, rng)
    if stress:
        elasticity = np.full(n, float(STRESS["budget_elasticity"]))

    spend_uplift = params["monthly_shift_gbp"] / params["destination_monthly_spend"]
    destination_cac_effective = destination_cac * (1 + spend_uplift) ** elasticity

    total_shift = params["monthly_shift_gbp"] * (BUDGET_HORIZON_DAYS / 30.0)
    customers_gained = total_shift / destination_cac_effective
    customers_lost = total_shift / source_cac

    incremental = (
        customers_gained * destination_contribution - customers_lost * source_contribution
    )

    # What would have to be true for this to lose money. Solving the break-even
    # condition for the elasticity is more useful than the probability alone,
    # because it converts "we are confident" into a falsifiable claim someone
    # with market knowledge can argue with. An answer above 1.0 means no
    # physically possible response from the destination channel makes the move
    # unprofitable, which is why the probability comes out at 100%.
    ratio = (
        np.median(source_cac) * np.median(destination_contribution)
    ) / (np.median(destination_cac) * np.median(source_contribution))
    breakeven_elasticity = float(np.log(ratio) / np.log(1 + spend_uplift))

    return summarise(
        samples=incremental,
        recommendation_id=recommendation.recommendation_id,
        metric="Incremental gross profit",
        horizon_days=BUDGET_HORIZON_DAYS,
        random_seed=SIMULATION["random_seed"],
        assumptions={
            "scenario": "stressed" if stress else "nominal",
            "breakeven_elasticity": round(breakeven_elasticity, 2),
            "breakeven_interpretation": (
                f"The move only loses money if the destination channel's elasticity exceeds "
                f"{breakeven_elasticity:.2f}. Values above 1.0 are not physically possible, "
                f"since that would mean spending more wins fewer customers in absolute terms. "
                f"This is why the probability of a positive return is so high: it is not "
                f"model confidence, it is that the gap between the two channels' acquisition "
                f"costs is too large for diminishing returns to close."
                if breakeven_elasticity > 1.0
                else
                f"The move loses money if the destination channel's elasticity exceeds "
                f"{breakeven_elasticity:.2f}, which is within the plausible range, so the "
                f"outcome genuinely depends on how Google responds."
            ),
            "total_budget_shifted": round(total_shift, 2),
            "destination_spend_uplift_pct": round(spend_uplift, 4),
            "spend_elasticity_prior": "Beta(2, 4) on [0, 1], mean 0.33 - assumed, not "
                                      "estimable from this data",
            "median_elasticity_drawn": round(float(np.median(elasticity)), 3),
            "attribution_treatment": "Uniform draw between the pessimistic and optimistic "
                                     "CAC bounds, applied to both channels together since "
                                     "the missing-referrer mechanism is common to both",
            "source_cac_median": round(float(np.median(source_cac)), 2),
            "destination_cac_median": round(float(np.median(destination_cac)), 2),
            "destination_cac_after_uplift_median": round(
                float(np.median(destination_cac_effective)), 2
            ),
            "bootstrap_window_days": lookback,
            "conservatism": "Customers lost are priced at the source channel's average "
                            "acquisition cost, though a budget cut removes its least "
                            "efficient spend first, so the benefit is understated",
        },
    )


def simulate_inventory_purchase(
    recommendation: Recommendation, con, rng: np.random.Generator, stress: bool = False
) -> SimulationResult:
    """Incremental gross profit from ordering now versus ordering late.

    The counterfactual is the decision that matters. Comparing "order" against
    "never order again" would credit the order with every unit it ever sells,
    which is both enormous and meaningless: the stock gets bought eventually
    either way. The real choice is *when*, and acting now buys one thing, which
    is a shorter gap with an empty shelf.

    So the two arms are:
      order now    delivery lands after the lead time, and any demand between
                   running out and delivery is lost.
      order late   nobody acts until the shelf is empty, so the order is placed
                   at that point and the gap is a full lead time long.

    The difference is the sales rescued. Against it sits the cost of holding
    stock earlier than necessary, and the risk that demand falls back and the
    order oversupplies.

    Demand is resampled from the SKU's own recent daily sales, so a volatile
    product produces a wider outcome distribution than a steady one without
    that having to be specified anywhere. Lost demand is treated as gone rather
    than deferred: someone who cannot buy a supplement today buys it elsewhere.

    Under `stress` the order is judged against the world it was not sized for:
    the surge behind it unwinds with certainty rather than with 15% probability,
    products with no surge still sell a quarter below recent history, and the
    supplier delivers 30% late.
    """
    params = recommendation.parameters
    rules = RECOMMENDATION_RULES["inventory_reorder"]
    n = SIMULATION["n_draws"]
    horizon = INVENTORY_HORIZON_DAYS

    skus = [line["sku"] for line in params["lines"]]
    placeholders = ", ".join(f"'{sku}'" for sku in skus)
    history = query(
        con,
        f"""
        SELECT sku, date_day, units
        FROM semantic.product_velocity
        WHERE sku IN ({placeholders})
          AND date_day > (SELECT max(date_day) - {SIMULATION['bootstrap_window_days']}
                          FROM semantic.product_velocity)
        ORDER BY sku, date_day
        """,
    )

    lead_time = draw_lead_time(rules["assumed_supplier_lead_time_days"], n, rng)
    if stress:
        lead_time = lead_time * float(STRESS["inventory_lead_time_multiplier"])
    total_incremental = np.zeros(n)

    # Cash still sitting in unsold stock at the end of the horizon, tracked
    # separately from profit on purpose.
    #
    # The profit model charges leftover stock a carrying cost, which for a
    # 180-day horizon is about 12% of its value. That is the right number for
    # "was this order profitable" and the wrong one for "can we afford to be
    # wrong", because the exposure on an irreversible purchase is the capital
    # itself, not the interest on it. The autonomy gate needs the capital.
    total_unsold_capital = np.zeros(n)

    # One reversion draw for the whole product, not one per variant: if the
    # trend behind Vitamin D3 unwinds, it unwinds for both the 1000IU and the
    # 4000IU pack. Drawing separately would let one variant collapse while its
    # sibling thrived, diversifying away a risk that is not diversifiable.
    #
    # As in the budget model, the draw happens even when stress is about to
    # replace it, so both runs consume the generator identically.
    reversion_ratio = float(params.get("demand_reversion_ratio", 1.0))
    reverts = rng.random(n) < DEMAND_REVERSION_PROBABILITY
    demand_scale = np.where(reverts & (reversion_ratio < 1.0), reversion_ratio, 1.0)
    if stress:
        # A product sized against a surge is stressed by that surge unwinding.
        # A product with no surge has nothing to unwind, so it gets the generic
        # haircut instead: recent history is not guaranteed to repeat either.
        demand_scale = np.full(
            n, min(reversion_ratio, float(STRESS["inventory_demand_haircut"]))
        )

    for line in params["lines"]:
        sku_history = history[history["sku"] == line["sku"]]["units"].to_numpy(dtype=float)
        demand_paths = block_bootstrap(sku_history, n, horizon, rng) * demand_scale[:, None]
        cumulative = np.cumsum(demand_paths, axis=1)
        total_demand = cumulative[:, -1]

        on_hand = float(line["units_on_hand"])
        ordered = float(line["reorder_units"])
        margin = float(line["unit_margin"])
        unit_cost = float(line["reorder_cost"]) / ordered if ordered else 0.0

        # The day the shelf empties, which depends on how fast demand actually
        # arrives rather than on the average used to size the order.
        runs_out_on = np.argmax(cumulative >= on_hand, axis=1).astype(float)
        never_runs_out = cumulative[:, -1] < on_hand
        runs_out_on[never_runs_out] = horizon

        arrival_now = np.round(lead_time)
        arrival_late = runs_out_on + np.round(lead_time)

        def demand_between(start: np.ndarray, end: np.ndarray) -> np.ndarray:
            """Units demanded in a window, from the cumulative demand paths."""
            lo = np.clip(start, 0, horizon - 1).astype(int)
            hi = np.clip(end, 0, horizon - 1).astype(int)
            rows = np.arange(n)
            return np.maximum(0.0, cumulative[rows, hi] - cumulative[rows, lo])

        # Sales lost while the shelf is empty, under each arm.
        lost_ordering_now = demand_between(runs_out_on, np.maximum(runs_out_on, arrival_now))
        lost_ordering_late = demand_between(runs_out_on, arrival_late)

        rescued_units = np.maximum(0.0, lost_ordering_late - lost_ordering_now)

        # Ordering now means holding the stock from the earlier arrival date, so
        # the extra carrying period is the gap between the two arrival dates.
        days_held_earlier = np.maximum(0.0, arrival_late - arrival_now)
        holding_cost = (
            ordered * unit_cost * INVENTORY_HOLDING_COST_ANNUAL * (days_held_earlier / 365.0)
        )

        # Oversupply risk: whatever is still unsold at the end of the horizon
        # was bought too early, and for supplements it ages toward expiry.
        sold_by_horizon = np.minimum(total_demand - lost_ordering_now, on_hand + ordered)
        leftover = np.maximum(0.0, on_hand + ordered - sold_by_horizon)
        oversupply_cost = leftover * unit_cost * INVENTORY_HOLDING_COST_ANNUAL * (horizon / 365.0)

        total_incremental += rescued_units * margin - holding_cost - oversupply_cost
        total_unsold_capital += leftover * unit_cost

    return summarise(
        samples=total_incremental,
        recommendation_id=recommendation.recommendation_id,
        metric="Incremental gross profit",
        horizon_days=horizon,
        random_seed=SIMULATION["random_seed"],
        assumptions={
            "scenario": "stressed" if stress else "nominal",
            "assumed_lead_time_days": rules["assumed_supplier_lead_time_days"],
            "lead_time_distribution": "Lognormal, skewed late, since deliveries miss late "
                                      "more often than early",
            "median_lead_time_drawn": round(float(np.median(lead_time)), 1),
            "holding_cost_annual_pct": INVENTORY_HOLDING_COST_ANNUAL,
            "demand_source": f"Block bootstrap of the last "
                             f"{SIMULATION['bootstrap_window_days']} days of actual sales",
            "stockout_behaviour": "Demand arising while out of stock is lost, not deferred",
            "counterfactual": "Ordering now versus ordering once the shelf is empty. "
                              "Comparing against never reordering would credit the order "
                              "with sales it would have made anyway",
            "demand_reversion_probability": (
                float(STRESS["inventory_reversion_probability"])
                if stress
                else (DEMAND_REVERSION_PROBABILITY if reversion_ratio < 1.0 else 0.0)
            ),
            "demand_reversion_ratio": reversion_ratio,
            "demand_scale_applied": round(float(np.median(demand_scale)), 4),
            "capital_unsold_at_horizon_p50": round(
                float(np.percentile(total_unsold_capital, 50)), 2
            ),
            "capital_unsold_at_horizon_p95": round(
                float(np.percentile(total_unsold_capital, 95)), 2
            ),
            "reorder_cost": params["total_reorder_cost"],
        },
    )


SIMULATORS = {
    ActionType.BUDGET_REALLOCATION: simulate_budget_reallocation,
    ActionType.INVENTORY_PURCHASE: simulate_inventory_purchase,
}

NOT_SIMULATED_REASON = {
    ActionType.INVESTIGATION: (
        "Not simulated. The payoff of an investigation is information, not cash flow, and "
        "its value depends entirely on what it finds. Putting a number on it would be "
        "inventing one."
    ),
    ActionType.FLOW_OPTIMISATION: (
        "Not simulated. The uplift from new email creative cannot be estimated from "
        "historical data, since the historical data contains no comparable creative change. "
        "The recommendation is to run it as a split test, which measures the effect rather "
        "than assuming it."
    ),
    ActionType.NO_ACTION: (
        "Not simulated. Taking no action has no incremental outcome to model."
    ),
}
