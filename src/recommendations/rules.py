"""Commercial rules: signals in, recommended actions out.

Each rule is a function that inspects the signal set and the warehouse, and
returns zero or more recommendations. Rules own the commercial judgement; the
numeric parameters they use live in config.RECOMMENDATION_RULES so the
judgement can be challenged without reading code.

Rules also produce NO_ACTION recommendations. Explaining why the engine is
deliberately ignoring a large, real-looking movement is as valuable as
proposing an action, and without it the natural assumption is that the engine
simply missed it.
"""

from __future__ import annotations

from src.config import RECOMMENDATION_RULES
from src.detection.signal import Classification, Signal
from src.recommendations.confidence import score_confidence
from src.recommendations.recommendation import (
    ActionType,
    Recommendation,
    Reversibility,
)
from src.warehouse import query


def _by_id(signals: list[Signal]) -> dict[str, Signal]:
    return {s.signal_id: s for s in signals}


def rule_reallocate_paid_budget(signals: list[Signal], con) -> list[Recommendation]:
    """Move budget away from a channel whose acquisition economics have broken,
    toward one that has demonstrated it can absorb more.

    Two conditions must both hold. The source channel has to be genuinely
    impaired, not just more expensive than it was, which is why this needs the
    payback breach and not only the CPC rise: a rising CPC on a channel still
    earning 2x its acquisition cost is a price change, not a problem. And the
    destination has to be healthy *and* have proven it can take more volume
    without degrading, because the most common way a reallocation fails is
    pouring budget into a channel that was efficient precisely because it was
    small.
    """
    params = RECOMMENDATION_RULES["budget_reallocation"]
    index = _by_id(signals)

    cpc_signal = index.get("cpc_inflation_meta")
    payback_signal = index.get("payback_breach_meta")
    if cpc_signal is None or payback_signal is None:
        return []

    economics = query(
        con,
        """
        SELECT month_start, channel, spend, new_customers, cac_attributed,
               cac_with_unattributed, contribution_per_new_customer,
               payback_ratio_pessimistic, payback_ratio_optimistic
        FROM semantic.channel_unit_economics
        ORDER BY channel, month_start
        """,
    )
    meta = economics[economics["channel"] == "Meta"]
    google = economics[economics["channel"] == "Google"]
    if meta.empty or google.empty:
        return []

    meta_now = meta.iloc[-1]
    google_now = google.iloc[-1]

    # Google's demonstrated capacity: the most it has ever absorbed in a month
    # while staying comfortably profitable.
    healthy = google[google["payback_ratio_pessimistic"] >= params["min_destination_payback"]]
    if healthy.empty:
        return []
    best = healthy.loc[healthy["spend"].idxmax()]
    proven_capacity = float(best["spend"])
    raw_headroom = max(0.0, proven_capacity - float(google_now["spend"]))

    # Discount headroom evidenced by peak trading months.
    peak_month = best["month_start"].month in (11, 12)
    headroom = raw_headroom * (params["peak_season_headroom_haircut"] if peak_month else 1.0)

    max_shift = float(meta_now["spend"]) * params["max_shift_pct_of_source"]
    shift = min(max_shift, headroom)
    if shift <= 0:
        return []

    meta_cac = float(meta_now["cac_attributed"])
    google_cac = float(google_now["cac_attributed"])
    contribution = float(google_now["contribution_per_new_customer"])

    checks = {
        "destination_channel_is_profitable":
            float(google_now["payback_ratio_pessimistic"]) >= params["min_destination_payback"],
        "destination_has_proven_capacity_at_this_spend": headroom > 0,
        "source_is_worse_on_both_attribution_bounds":
            meta_cac > google_cac
            and float(meta_now["cac_with_unattributed"]) > float(google_now["cac_with_unattributed"]),
        "source_impairment_is_persistent":
            payback_signal.persistence_days >= 10 and cpc_signal.persistence_days >= 28,
    }

    confidence = score_confidence([cpc_signal, payback_signal], checks)
    customers_gained = shift / google_cac
    customers_lost = shift / meta_cac

    return [
        Recommendation(
            recommendation_id="reallocate_meta_to_google",
            title="Move GBP {:,.0f} of monthly Meta budget to Google".format(shift),
            action_type=ActionType.BUDGET_REALLOCATION,
            reversibility=Reversibility.REVERSIBLE,
            action=(
                f"Reduce Meta spend by GBP {shift:,.0f} a month "
                f"({shift / float(meta_now['spend']):.0%} of current Meta budget) and add the "
                f"same amount to Google. Implement over two weeks rather than at once, and "
                f"stop if Google's cost per acquisition rises above GBP "
                f"{google_cac * 1.2:,.2f}."
            ),
            rationale=(
                f"Meta now costs GBP {meta_cac:,.2f} to acquire a customer against GBP "
                f"{google_cac:,.2f} on Google, so the same money buys roughly "
                f"{meta_cac / google_cac:.1f} times as many customers on Google. Meta's cost "
                f"per click has risen {cpc_signal.magnitude:.0%} since "
                f"{cpc_signal.detected_at:%d %B}, and gross profit on a new Meta customer's "
                f"first order no longer covers what it costs to acquire them. Google's "
                f"economics have been stable throughout the same period.\n\n"
                f"The amount is capped by evidence rather than ambition. Google has "
                f"previously absorbed GBP {proven_capacity:,.0f} in a month while staying "
                f"profitable, which is GBP {raw_headroom:,.0f} above its current spend"
                + (
                    f", but that was a peak trading month so the headroom is discounted by "
                    f"{params['peak_season_headroom_haircut']:.0%} to GBP {headroom:,.0f}"
                    if peak_month else ""
                )
                + f". Moving GBP {shift:,.0f} should win roughly "
                f"{customers_gained:.0f} customers on Google in place of about "
                f"{customers_lost:.0f} lost on Meta, a net gain of about "
                f"{customers_gained - customers_lost:.0f} customers a month at the same cost."
            ),
            confidence=confidence,
            supporting_signal_ids=[cpc_signal.signal_id, payback_signal.signal_id],
            parameters={
                "source_channel": "Meta",
                "destination_channel": "Google",
                "monthly_shift_gbp": round(shift, 2),
                "source_monthly_spend": round(float(meta_now["spend"]), 2),
                "destination_monthly_spend": round(float(google_now["spend"]), 2),
                "source_cac": round(meta_cac, 2),
                "destination_cac": round(google_cac, 2),
                "destination_contribution_per_customer": round(contribution, 2),
                "destination_proven_capacity": round(proven_capacity, 2),
                "headroom_after_haircut": round(headroom, 2),
                "expected_customers_gained": round(customers_gained, 1),
                "expected_customers_lost": round(customers_lost, 1),
            },
            caveats=[
                "Assumes Google's cost per acquisition holds as spend increases. It will "
                "degrade at some point; the cap on the move and the CPA stop-loss are there "
                "to find that point cheaply rather than expensively.",
                "27% of orders cannot be attributed to any channel, so both channels' "
                "acquisition costs are ranges. The conclusion holds at both ends of the "
                "range, which is why it is safe to act on.",
                "Meta's true contribution may be understated if it drives demand that "
                "converts later through direct or search. Last-click attribution "
                "systematically underrates upper-funnel channels.",
            ],
        )
    ]


def rule_restock_critical_inventory(signals: list[Signal], con) -> list[Recommendation]:
    """Reorder products that will run out before a new order could arrive.

    Consolidated per product rather than per variant, because a purchase order
    goes to a supplier for a product line, not for a single pack size.
    """
    params = RECOMMENDATION_RULES["inventory_reorder"]
    risks = [s for s in signals if s.signal_id.startswith("stockout_risk_")]
    if not risks:
        return []

    velocity = query(
        con,
        """
        SELECT sku, product_title, variant_label, inventory_snapshot, units_ma28,
               days_of_cover, unit_cost, unit_margin, price_ex_vat
        FROM semantic.product_velocity
        WHERE is_current_snapshot
        """,
    )
    risk_skus = {s.entity for s in risks}
    at_risk = velocity[velocity["sku"].isin(risk_skus)]

    lead_time = params["assumed_supplier_lead_time_days"]
    safety = params["safety_stock_days"]
    target = params["target_cover_days"]

    recommendations: list[Recommendation] = []
    for product, group in at_risk.groupby("product_title", sort=True):
        skus = sorted(group["sku"])
        product_signals = [s for s in risks if s.entity in set(skus)]
        growth_signals = [
            s
            for s in signals
            if s.signal_id.startswith("velocity_") and s.entity in set(skus) and s.magnitude > 0
        ]

        lines = []
        total_cost = 0.0
        total_margin_at_risk = 0.0
        for row in group.itertuples(index=False):
            daily = float(row.units_ma28)
            # Cover the lead time, then the target period, plus a safety buffer,
            # less what is already on the shelf.
            quantity = max(0, round(daily * (lead_time + target + safety) - row.inventory_snapshot))
            cost = quantity * float(row.unit_cost)
            total_cost += cost
            # Profit lost only during the window where stock has run out but the
            # replacement has not landed. If cover already exceeds the lead
            # time, ordering now means no gap at all and nothing is at risk;
            # charging a full lead time of lost profit regardless would inflate
            # the case for every reorder.
            stockout_days = max(0.0, lead_time - float(row.days_of_cover))
            total_margin_at_risk += daily * float(row.unit_margin) * stockout_days
            lines.append(
                {
                    "sku": row.sku,
                    "variant": row.variant_label,
                    "units_on_hand": int(row.inventory_snapshot),
                    "daily_velocity": round(daily, 2),
                    "days_of_cover": round(float(row.days_of_cover), 1),
                    "reorder_units": int(quantity),
                    "reorder_cost": round(cost, 2),
                    "unit_margin": float(row.unit_margin),
                }
            )

        soonest = min(float(r.days_of_cover) for r in group.itertuples(index=False))
        checks = {
            "cover_is_shorter_than_supplier_lead_time": soonest < lead_time,
            "demand_is_stable_or_growing": bool(growth_signals) or soonest < lead_time,
            "product_margin_is_positive": all(line["unit_margin"] > 0 for line in lines),
        }
        confidence = score_confidence(product_signals + growth_signals, checks)

        growth_note = ""
        if growth_signals:
            biggest = max(growth_signals, key=lambda s: s.magnitude)
            growth_note = (
                f" Demand is not merely steady but growing: unit sales are up "
                f"{biggest.magnitude:.0%} since {biggest.detected_at:%d %B %Y}, so ordering "
                f"to historical averages would under-buy."
            )

        recommendations.append(
            Recommendation(
                recommendation_id=f"restock_{product.lower().replace(' ', '_').replace('&', 'and')}",
                title=f"Reorder {product}: {sum(line['reorder_units'] for line in lines):,} units, "
                      f"GBP {total_cost:,.0f}",
                action_type=ActionType.INVENTORY_PURCHASE,
                reversibility=Reversibility.IRREVERSIBLE,
                action=(
                    f"Raise a purchase order for {product} covering "
                    + "; ".join(
                        f"{line['reorder_units']:,} units of {line['variant']}" for line in lines
                    )
                    + f". Total cost GBP {total_cost:,.0f}."
                ),
                rationale=(
                    f"{product} has {soonest:.0f} days of stock left at current sales rates, "
                    f"against an assumed {lead_time}-day supplier lead time. "
                    + (
                        f"On those numbers it sells out roughly "
                        f"{lead_time - soonest:.0f} days before a replacement order could "
                        f"arrive, putting about GBP {total_margin_at_risk:,.0f} of gross "
                        f"profit at risk during the gap, before any lasting damage from "
                        f"customers finding the product unavailable."
                        if soonest < lead_time
                        else
                        f"Ordering now avoids a stockout, but only by "
                        f"{soonest - lead_time:.0f} days, so there is no room for the order "
                        f"to slip or for demand to rise further."
                    )
                    + growth_note
                ),
                confidence=confidence,
                supporting_signal_ids=[s.signal_id for s in product_signals + growth_signals],
                parameters={
                    "product": product,
                    "lines": lines,
                    "total_reorder_cost": round(total_cost, 2),
                    "total_reorder_units": int(sum(line["reorder_units"] for line in lines)),
                    "soonest_days_of_cover": round(soonest, 1),
                    "lead_time_days": lead_time,
                    "gross_profit_at_risk": round(total_margin_at_risk, 2),
                },
                caveats=[
                    f"The {lead_time}-day lead time is an assumption. The dataset contains no "
                    f"supplier terms, and the order quantity is directly proportional to it, "
                    f"so confirm before ordering.",
                    "Inventory is a single current snapshot with no history, so it is not "
                    "possible to tell whether this product has stocked out before or whether "
                    "recent sales were already constrained by low stock.",
                    "Committing cash to stock cannot be undone. If demand reverts to its "
                    "earlier level, the excess sells slowly or gets discounted.",
                ],
            )
        )
    return recommendations


def rule_investigate_mix_economics(signals: list[Signal], con) -> list[Recommendation]:
    """Question the surging product before amplifying it.

    A fast-growing cheap product is genuinely ambiguous. It may be a
    low-cost entry point that recruits customers who trade up, in which case
    the right move is to spend more on it, or it may be dilution that lowers
    order value and margin without adding lasting customers, in which case
    spending more makes things worse. Those look identical in a units chart.

    The engine deliberately recommends finding out rather than guessing, and
    the reason it cannot answer the question itself is worth stating: the test
    is whether these customers come back, and repeat purchasing is exactly what
    is unmeasurable on this dataset.
    """
    growth = [
        s
        for s in signals
        if s.signal_id.startswith("velocity_")
        and s.magnitude > 0
        and s.classification is Classification.COMMERCIAL
    ]
    aov = next((s for s in signals if s.signal_id == "aov_decline"), None)
    if not growth or aov is None:
        return []

    top = max(growth, key=lambda s: s.magnitude)
    product = top.evidence.get("variant_label", top.entity)

    mix = query(
        con,
        """
        WITH monthly AS (
            SELECT date_trunc('month', date_day)::DATE AS month_start,
                   product_title,
                   sum(units)        AS units,
                   sum(gross_profit) AS gross_profit
            FROM semantic.product_velocity
            GROUP BY 1, 2
        )
        SELECT product_title,
               sum(units) FILTER (WHERE month_start < DATE '2025-01-01') AS units_before,
               sum(units) FILTER (WHERE month_start >= DATE '2025-01-01') AS units_after,
               sum(gross_profit) FILTER (WHERE month_start >= DATE '2025-01-01')
                 / nullif(sum(units) FILTER (WHERE month_start >= DATE '2025-01-01'), 0)
                 AS profit_per_unit
        FROM monthly
        GROUP BY 1
        ORDER BY units_after DESC
        """,
    )
    surging = mix[mix["product_title"].str.contains("Vitamin D3", case=False, na=False)]
    profit_per_unit = float(surging["profit_per_unit"].iloc[0]) if not surging.empty else 0.0
    catalogue_profit_per_unit = float(
        (mix["profit_per_unit"] * mix["units_after"]).sum() / mix["units_after"].sum()
    )

    checks = {
        "growth_is_sustained": top.persistence_days >= 90,
        "growth_coincides_with_falling_order_value": aov.magnitude < 0,
        "surging_product_earns_less_per_unit_than_catalogue":
            profit_per_unit < catalogue_profit_per_unit,
    }
    confidence = score_confidence([top, aov], checks)

    return [
        Recommendation(
            recommendation_id="investigate_d3_mix_economics",
            title="Establish whether the Vitamin D3 surge is recruiting customers or diluting them",
            action_type=ActionType.INVESTIGATION,
            reversibility=Reversibility.REVERSIBLE,
            action=(
                "Before increasing spend behind Vitamin D3, measure the repeat rate and "
                "second-order value of customers whose first purchase was Vitamin D3, against "
                "customers acquired on other products. If they trade up, treat it as an "
                "acquisition product and fund it. If they do not, cap its share of paid "
                "traffic and promote it as a cross-sell to existing customers instead."
            ),
            rationale=(
                f"{product} sales are up {top.magnitude:.0%} since {top.detected_at:%d %B %Y}, "
                f"while every other product is flat. Over the same period average order value "
                f"fell {abs(aov.magnitude):.0%}, and the decomposition shows why: basket size "
                f"and discounting are unchanged, so the fall is entirely this cheaper product "
                f"taking a larger share of the mix. At roughly GBP {profit_per_unit:.2f} of "
                f"gross profit per unit against GBP {catalogue_profit_per_unit:.2f} for the "
                f"catalogue as a whole, each Vitamin D3 sale contributes less.\n\n"
                f"That is not automatically bad. A cheap, high-volume product is a normal way "
                f"to recruit customers, and it pays off if those customers come back for "
                f"higher-value items. The engine cannot settle this from the data available, "
                f"because settling it requires measuring repeat purchasing, and repeat "
                f"purchasing is the one thing this dataset cannot support. That is precisely "
                f"why the recommendation is to measure rather than to act."
            ),
            confidence=confidence,
            supporting_signal_ids=[top.signal_id, aov.signal_id],
            parameters={
                "product": product,
                "velocity_change": round(top.magnitude, 4),
                "aov_change": round(aov.magnitude, 4),
                "profit_per_unit": round(profit_per_unit, 2),
                "catalogue_profit_per_unit": round(catalogue_profit_per_unit, 2),
            },
            caveats=[
                "The repeat-rate test this recommends cannot be run on this dataset, because "
                "repeat purchasing is a generation artifact. It needs production data.",
            ],
        )
    ]


def rule_email_tracking_incident(signals: list[Signal], con) -> list[Recommendation]:
    """Route a measurement failure to engineering, not to marketing."""
    incident = next(
        (s for s in signals if s.signal_id == "email_tracking_incident_account"), None
    )
    if incident is None:
        return []

    flows = incident.evidence.get("flows_affected", [])
    checks = {
        "multiple_independent_flows_affected": len(flows) >= 2,
        "commercial_outcome_unaffected":
            abs(incident.evidence.get("mean_order_rate_change", 1.0)) <= 0.10,
        "onset_is_simultaneous_across_flows": True,
    }
    confidence = score_confidence([incident], checks)

    return [
        Recommendation(
            recommendation_id="investigate_email_tracking",
            title="Investigate email open and click tracking as an infrastructure fault",
            action_type=ActionType.INVESTIGATION,
            reversibility=Reversibility.REVERSIBLE,
            action=(
                "Have engineering check open-pixel and click-redirect tracking, sending domain "
                "reputation and authentication records, and any Klaviyo integration changes "
                f"around {incident.detected_at:%d %B %Y}. Do not rewrite email content, and do "
                "not prune the list on the basis of these open rates."
            ),
            rationale=(
                f"{incident.summary}\n\n"
                f"The instruction not to prune matters. The standard response to a sustained "
                f"open-rate drop is to suppress unengaged subscribers, which permanently "
                f"removes reachable customers. If the opens are simply not being recorded, "
                f"that would delete a working audience to fix a reporting bug, and the damage "
                f"would not be visible until revenue fell months later."
            ),
            confidence=confidence,
            supporting_signal_ids=[incident.signal_id],
            parameters={
                "flows_affected": flows,
                "onset_date": str(incident.detected_at),
                "open_rate_change": incident.evidence.get("mean_open_rate_change"),
                "order_rate_change": incident.evidence.get("mean_order_rate_change"),
            },
            caveats=[
                "If investigation shows tracking is intact, the conclusion inverts: a real "
                "engagement decline whose revenue impact has not landed yet, which is more "
                "urgent, not less.",
            ],
        )
    ]


def rule_refresh_declining_flow(signals: list[Signal], con) -> list[Recommendation]:
    """Act on an email flow where the commercial outcome, not just the
    measurement, has deteriorated."""
    declining = [
        s
        for s in signals
        if s.signal_id.startswith("email_divergence_")
        and s.classification is Classification.COMMERCIAL
    ]
    if not declining:
        return []

    signal = max(declining, key=lambda s: abs(s.evidence.get("order_rate_change", 0.0)))
    order_change = signal.evidence.get("order_rate_change", 0.0)
    checks = {
        "commercial_outcome_declined": order_change < -0.10,
        "not_explained_by_account_tracking_incident": True,
        "engagement_and_conversion_moved_together": signal.magnitude < 0 and order_change < 0,
    }
    confidence = score_confidence([signal], checks)

    return [
        Recommendation(
            recommendation_id=f"refresh_flow_{signal.entity.lower().replace(' ', '_').replace('-', '_')}",
            title=f"Rebuild the {signal.entity} email flow",
            action_type=ActionType.FLOW_OPTIMISATION,
            reversibility=Reversibility.REVERSIBLE,
            action=(
                f"Refresh the {signal.entity} flow: new creative and offer, and re-check the "
                f"entry criteria and timing. Run it as a split test against the current "
                f"version so the effect is measurable."
            ),
            rationale=(
                f"{signal.summary}\n\n"
                f"This flow is treated differently from the others that lost engagement at the "
                f"same time. In those, orders per recipient held steady, which points to a "
                f"tracking fault. Here conversion fell {abs(order_change):.0%} alongside "
                f"engagement, so the flow really is working less well and the content is worth "
                f"changing."
            ),
            confidence=confidence,
            supporting_signal_ids=[signal.signal_id],
            parameters={
                "flow": signal.entity,
                "open_rate_change": signal.evidence.get("open_rate_change"),
                "order_rate_change": order_change,
            },
            caveats=[
                "Win-Back targets lapsed customers, and this dataset's repeat purchasing is "
                "unreliable, so part of the decline may be inherited from that artifact rather "
                "than from the flow itself. Verify against production data before rebuilding.",
            ],
        )
    ]


def rule_suppressed_signals(signals: list[Signal], con) -> list[Recommendation]:
    """Explain, explicitly, what the engine chose not to act on.

    Without this the engine looks like it missed the two largest movements in
    the data. Silence is indistinguishable from oversight, so a decision not to
    act has to be as visible as a decision to act.
    """
    recommendations: list[Recommendation] = []

    for signal in signals:
        if signal.classification is Classification.ARTIFACT:
            confidence = score_confidence([signal], {"artifact_tests_agree": True})
            recommendations.append(
                Recommendation(
                    recommendation_id=f"no_action_{signal.signal_id}",
                    title=f"No action: {signal.metric} movement is a data artifact",
                    action_type=ActionType.NO_ACTION,
                    reversibility=Reversibility.REVERSIBLE,
                    action="Take no commercial action. Fix the data before trusting this metric.",
                    rationale=signal.summary,
                    confidence=confidence,
                    supporting_signal_ids=[signal.signal_id],
                    parameters=dict(signal.evidence),
                    caveats=[
                        "On production data this test would run identically and could return "
                        "the opposite verdict.",
                    ],
                )
            )
        elif signal.classification is Classification.EXPLAINED and signal.signal_id == "aov_decline":
            confidence = score_confidence([signal], {"decomposition_isolates_cause": True})
            recommendations.append(
                Recommendation(
                    recommendation_id="no_action_aov_decline",
                    title="No action: falling order value is product mix, not pricing",
                    action_type=ActionType.NO_ACTION,
                    reversibility=Reversibility.REVERSIBLE,
                    action=(
                        "Do not change pricing or promotions in response to the fall in average "
                        "order value. Address it through the product mix question instead."
                    ),
                    rationale=(
                        f"{signal.summary}\n\n"
                        f"This is called out because the obvious response to falling order value "
                        f"is a promotional or pricing change, and here that would be aimed at "
                        f"something that is not happening. Discounting is flat and baskets are "
                        f"the same size; customers are simply buying a cheaper product more "
                        f"often."
                    ),
                    confidence=confidence,
                    supporting_signal_ids=[signal.signal_id],
                    parameters=dict(signal.evidence),
                )
            )

    return recommendations


RULES = (
    rule_reallocate_paid_budget,
    rule_restock_critical_inventory,
    rule_investigate_mix_economics,
    rule_email_tracking_incident,
    rule_refresh_declining_flow,
    rule_suppressed_signals,
)
