"""Concrete detectors.

Each returns a list of Signal records. Detectors decide *what moved* and
*whether it is real*; they deliberately do not decide what to do about it,
which is the recommendation engine's job. Keeping that boundary means the
commercial rules can change without touching the statistics.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import numpy as np
import pandas as pd

from src.config import ANALYSIS_END, DETECTION
from src.detection.signal import Classification, Signal, SignalType
from src.detection.statistics import (
    compare_periods,
    cusum_changepoint_span,
    deseasonalise,
    sustained_breach,
    trailing_consecutive_run,
)
from src.warehouse import query


def _trailing_breach(
    dates: pd.Series,
    series: pd.Series,
    threshold_value: float,
    above: bool = True,
) -> tuple[int, dt.date | None]:
    """Length and start date of the breach run ending at the last observation.

    Returns (0, None) when the metric is not currently in breach, which is the
    signal to stay quiet: a problem that has already resolved is not a decision.
    """
    flags = (series > threshold_value) if above else (series < threshold_value)
    flags = flags.fillna(False)
    run = trailing_consecutive_run(flags.to_numpy())
    if run == 0:
        return 0, None
    start = pd.to_datetime(dates.iloc[len(dates) - run]).date()
    return run, start


def detect_cpc_inflation(con, quality: dict[str, float]) -> list[Signal]:
    """Sustained cost-per-click inflation on a paid channel.

    CPC is the earliest honest warning of an auction turning against you. It
    moves before CAC does, and unlike CAC it does not depend on attribution, so
    it is measurable even with a 27% referrer gap. That independence is why it
    is the primary detector rather than CAC.
    """
    signals: list[Signal] = []
    frame = query(
        con,
        """
        SELECT date_day, channel, cpc, cpc_ma7, spend, clicks
        FROM semantic.channel_daily_performance
        WHERE spend IS NOT NULL AND clicks > 0  -- paid channels are the ones with spend
        ORDER BY channel, date_day
        """,
    )
    dq = quality.get("channel_efficiency", 1.0)
    window = DETECTION["baseline_window_days"]

    for channel, group in frame.groupby("channel", sort=True):
        group = group.reset_index(drop=True)
        if len(group) < window * 2:
            continue

        adjusted = deseasonalise(group["date_day"], group["cpc"])
        span = cusum_changepoint_span(
            adjusted,
            baseline_window=window,
            drift=DETECTION["cusum_drift"],
            threshold=DETECTION["cusum_threshold"],
        )
        if span is None:
            continue

        # Baseline runs to the change's origin, not to the detection point.
        # CUSUM confirms a shift only after the evidence accumulates, so the
        # days between origin and alarm are already post-change; leaving them
        # in the baseline would drag it toward the new level and understate
        # the very movement being reported.
        origin, _ = span
        baseline = group["cpc"].iloc[:origin]
        recent = group["cpc"].iloc[-window:]
        magnitude, p_value = compare_periods(baseline, recent)
        if magnitude < DETECTION["cpc_inflation_pct"]:
            continue

        baseline_median = float(np.median(baseline))
        persistence, started = _trailing_breach(
            group["date_day"],
            group["cpc_ma7"],
            baseline_median * (1 + DETECTION["cpc_inflation_pct"]),
        )
        if persistence < DETECTION["min_consecutive_days"]:
            continue

        current = float(recent.median())
        signals.append(
            Signal(
                signal_id=f"cpc_inflation_{channel.lower()}",
                signal_type=SignalType.TREND_BREAK,
                classification=Classification.COMMERCIAL,
                entity=channel,
                metric="cpc",
                metric_family="channel_efficiency",
                detected_at=started or pd.to_datetime(group["date_day"].iloc[origin]).date(),
                direction="increase",
                baseline_value=baseline_median,
                current_value=current,
                magnitude=magnitude,
                persistence_days=persistence,
                p_value=p_value,
                data_quality_score=dq,
                summary=(
                    f"{channel} cost per click has risen {magnitude:.0%}, from "
                    f"GBP {baseline_median:.2f} to GBP {current:.2f}, and has stayed "
                    f"elevated for {persistence} consecutive days since "
                    f"{started:%d %B %Y}."
                ),
                evidence={
                    "baseline_cpc": round(baseline_median, 4),
                    "current_cpc": round(current, 4),
                    "changepoint_date": str(pd.to_datetime(group["date_day"].iloc[origin]).date()),
                    "recent_spend_28d": round(float(group["spend"].iloc[-window:].sum()), 2),
                    "recent_clicks_28d": int(group["clicks"].iloc[-window:].sum()),
                    "clicks_lost_vs_baseline": int(
                        group["spend"].iloc[-window:].sum() / baseline_median
                        - group["clicks"].iloc[-window:].sum()
                    ),
                },
            )
        )
    return signals


def detect_payback_breach(con, quality: dict[str, float]) -> list[Signal]:
    """First-order payback falling below break-even on a paid channel.

    This is the threshold that turns a cost problem into a commercial decision.
    CPC rising is uncomfortable; acquisition costing more than the first order
    earns means the brand is paying for customers whose return cannot be
    verified on this dataset.

    Measured on a 7-day rolling window because daily new-customer counts per
    channel are in the tens, where a single day's ratio is noise.

    Uses the sustained-breach rule rather than an unbroken run. A payback ratio
    that has fallen to break-even oscillates either side of 1.0, and on this
    data Meta spends most of June below the line but happens to close the final
    day at 1.001. Demanding an unbroken run ending today would report no problem
    at all in a channel whose economics have clearly turned.
    """
    signals: list[Signal] = []
    frame = query(
        con,
        """
        SELECT date_day, channel, payback_ratio_7d, cac_attributed_7d,
               cac_with_unattributed_7d, spend, new_customers
        FROM semantic.channel_daily_performance
        WHERE spend IS NOT NULL  -- paid channels are the ones with spend
        ORDER BY channel, date_day
        """,
    )
    dq = quality.get("channel_efficiency", 1.0)
    window = DETECTION["baseline_window_days"]

    for channel, group in frame.groupby("channel", sort=True):
        group = group.reset_index(drop=True)
        below = (group["payback_ratio_7d"] < 1.0).fillna(False)
        persistence, start_index = sustained_breach(
            below.to_numpy(),
            window=window,
            min_share=0.5,
            min_run=DETECTION["min_consecutive_days"],
        )
        if persistence == 0:
            continue
        started = pd.to_datetime(group["date_day"].iloc[start_index]).date()
        baseline = group["payback_ratio_7d"].iloc[:window]
        recent = group["payback_ratio_7d"].iloc[-window:]
        magnitude, p_value = compare_periods(baseline, recent)

        current = float(recent.median())
        baseline_median = float(np.median(baseline.dropna()))
        cac_pess = float(group["cac_attributed_7d"].iloc[-1])
        cac_opt = float(group["cac_with_unattributed_7d"].iloc[-1])

        signals.append(
            Signal(
                signal_id=f"payback_breach_{channel.lower()}",
                signal_type=SignalType.THRESHOLD_BREACH,
                classification=Classification.COMMERCIAL,
                entity=channel,
                metric="payback_ratio_7d",
                metric_family="channel_efficiency",
                detected_at=started,
                direction="decrease",
                baseline_value=baseline_median,
                current_value=current,
                magnitude=magnitude,
                persistence_days=persistence,
                p_value=p_value,
                data_quality_score=dq,
                summary=(
                    f"{channel} first-order payback has been below break-even on "
                    f"{persistence} of the last {window} days, starting "
                    f"{started:%d %B %Y}. Gross profit on a new customer's first order "
                    f"now covers about {current:.0%} of what it costs to acquire them, "
                    f"down from {baseline_median:.0%} at the start of the year. Acquiring "
                    f"a customer through {channel} no longer pays for itself on the first "
                    f"purchase, and on this dataset repeat revenue cannot be verified."
                ),
                evidence={
                    "payback_now": round(current, 3),
                    "payback_baseline": round(baseline_median, 3),
                    "days_in_breach": persistence,
                    "window_days": window,
                    "cac_pessimistic": round(cac_pess, 2),
                    "cac_optimistic": round(cac_opt, 2),
                    "breach_start": str(started),
                },
            )
        )
    return signals


def detect_product_velocity_change(con, quality: dict[str, float]) -> list[Signal]:
    """Sustained change in how fast a SKU sells.

    The changepoint is located on the SKU's *share* of total daily units rather
    than on its raw unit count, which matters more than it sounds. Raw volume
    carries the brand's whole seasonal cycle: November and December run about
    1.6x the trough, so a CUSUM baselined on summer fires for every SKU as soon
    as peak season arrives. Detecting on share removes that automatically,
    because a demand wave that lifts everything leaves shares unchanged. Only a
    SKU genuinely outgrowing the rest of the catalogue moves its share.

    On this dataset that correction is decisive: detecting on raw units dates
    the Vitamin D3 surge to October, which is just Christmas, whereas detecting
    on share dates it to February, which is when it actually happened.

    Magnitude is then reported on absolute units, since that is what determines
    how much stock to buy.
    """
    signals: list[Signal] = []
    frame = query(
        con,
        """
        SELECT date_day, sku, variant_label, units, units_ma28, price_ex_vat, unit_margin
        FROM semantic.product_velocity
        ORDER BY sku, date_day
        """,
    )
    dq = quality.get("product_velocity", 1.0)
    window = DETECTION["baseline_window_days"]

    daily_total = frame.groupby("date_day")["units"].sum().rename("total_units")
    frame = frame.merge(daily_total, on="date_day", how="left")
    frame["unit_share"] = np.where(
        frame["total_units"] > 0, frame["units"] / frame["total_units"], np.nan
    )

    for sku, group in frame.groupby("sku", sort=True):
        group = group.reset_index(drop=True)
        if len(group) < window * 3:
            continue

        adjusted = deseasonalise(group["date_day"], group["unit_share"])
        span = cusum_changepoint_span(
            adjusted,
            baseline_window=window * 2,
            drift=DETECTION["cusum_drift"],
            threshold=DETECTION["cusum_threshold"],
        )
        if span is None:
            continue

        # As with CPC: the baseline stops at the change's origin so the ramp-up
        # days between origin and alarm do not contaminate it, and the reported
        # start date is when the shift began rather than when it was proved.
        origin, _ = span
        baseline = group["units"].iloc[:origin]
        recent = group["units"].iloc[-window * 2:]
        magnitude, p_value = compare_periods(baseline, recent)
        if abs(magnitude) < DETECTION["velocity_change_pct"]:
            continue

        baseline_daily = float(baseline.mean())
        current_daily = float(recent.mean())
        direction = "increase" if magnitude > 0 else "decrease"
        changepoint_date = pd.to_datetime(group["date_day"].iloc[origin]).date()

        signals.append(
            Signal(
                signal_id=f"velocity_{sku.lower().replace('-', '_')}",
                signal_type=SignalType.VELOCITY_CHANGE,
                classification=Classification.COMMERCIAL,
                entity=sku,
                metric="units_per_day",
                metric_family="product_velocity",
                detected_at=changepoint_date,
                direction=direction,
                baseline_value=baseline_daily,
                current_value=current_daily,
                magnitude=magnitude,
                persistence_days=int(len(group) - origin),
                p_value=p_value,
                data_quality_score=dq,
                summary=(
                    f"{group['variant_label'].iloc[0]} sales have {direction}d "
                    f"{abs(magnitude):.0%}, from {baseline_daily:.1f} to "
                    f"{current_daily:.1f} units a day, sustained since "
                    f"{changepoint_date:%d %B %Y}."
                ),
                evidence={
                    "variant_label": group["variant_label"].iloc[0],
                    "baseline_units_per_day": round(baseline_daily, 2),
                    "current_units_per_day": round(current_daily, 2),
                    "baseline_unit_share": round(float(group["unit_share"].iloc[:origin].mean()), 4),
                    "current_unit_share": round(float(group["unit_share"].iloc[-window * 2:].mean()), 4),
                    "unit_margin": float(group["unit_margin"].iloc[0]),
                    "price_ex_vat": float(group["price_ex_vat"].iloc[0]),
                },
            )
        )
    return signals


def detect_inventory_risk(con, quality: dict[str, float]) -> list[Signal]:
    """SKUs that will run out sooner than they can realistically be restocked.

    Uses 28-day trailing velocity against the current stock snapshot. The
    snapshot has no history, so this is a forward projection only and says
    nothing about past stockouts.
    """
    frame = query(
        con,
        """
        SELECT sku, variant_label, inventory_snapshot, units_ma28, days_of_cover,
               price_ex_vat, unit_margin, unit_cost
        FROM semantic.product_velocity
        WHERE is_current_snapshot
          AND days_of_cover IS NOT NULL
          AND days_of_cover < ?
        ORDER BY days_of_cover
        """,
        params=[DETECTION["inventory_cover_days_critical"]],
    )
    dq = quality.get("product_velocity", 1.0)
    signals: list[Signal] = []
    # The snapshot is "as of now", and now for this dataset is the end of the
    # analysis window rather than the wall clock.
    snapshot_date = dt.date.fromisoformat(ANALYSIS_END)

    for row in frame.itertuples(index=False):
        cover = float(row.days_of_cover)
        daily = float(row.units_ma28)
        signals.append(
            Signal(
                signal_id=f"stockout_risk_{row.sku.lower().replace('-', '_')}",
                signal_type=SignalType.INVENTORY_RISK,
                classification=Classification.COMMERCIAL,
                entity=row.sku,
                metric="days_of_cover",
                metric_family="product_velocity",
                detected_at=snapshot_date,
                direction="decrease",
                baseline_value=float(DETECTION["inventory_cover_days_critical"]),
                current_value=cover,
                magnitude=(cover - DETECTION["inventory_cover_days_critical"])
                / DETECTION["inventory_cover_days_critical"],
                # Persistence normally means "how long has this been true", but
                # stock level is a single snapshot with no history, so that
                # question has no answer here. What persistence is standing in
                # for downstream is how much evidence sits behind the estimate,
                # and the cover figure rests on the 28-day trailing velocity.
                # Reporting 1 would score this as a one-day blip and penalise a
                # well-evidenced projection for the source's lack of history.
                persistence_days=DETECTION["baseline_window_days"],
                p_value=None,
                data_quality_score=dq,
                summary=(
                    f"{row.variant_label} has {int(row.inventory_snapshot)} units left "
                    f"and is selling {daily:.1f} a day, which is {cover:.0f} days of "
                    f"cover. Below the {DETECTION['inventory_cover_days_critical']:.0f}-day "
                    f"threshold for reordering."
                ),
                evidence={
                    "variant_label": row.variant_label,
                    "units_on_hand": int(row.inventory_snapshot),
                    "daily_velocity": round(daily, 2),
                    "days_of_cover": round(cover, 1),
                    "unit_margin": float(row.unit_margin),
                    "unit_cost": float(row.unit_cost),
                    "daily_gross_profit_at_risk": round(daily * float(row.unit_margin), 2),
                },
            )
        )
    return signals


def detect_aov_decline(con, quality: dict[str, float]) -> list[Signal]:
    """Falling average order value, decomposed into its three possible causes.

    AOV can only fall three ways: smaller baskets, deeper discounts, or a mix
    shift toward cheaper products. Each demands a completely different response,
    and an undecomposed "AOV is down" alert reliably produces the wrong one,
    usually a promotional change aimed at a problem that is not promotional.

    When the decomposition shows the fall is mix-driven, the signal is
    classified EXPLAINED. It is real, but it is a consequence of a product
    trend that is already reported separately, and acting on it directly would
    mean fixing the wrong thing.
    """
    frame = query(
        con,
        """
        SELECT date_day, aov, units_per_order, avg_unit_price, discount_rate, orders
        FROM semantic.daily_business_metrics
        WHERE orders > 0
        ORDER BY date_day
        """,
    )
    if len(frame) < 180:
        return []

    dq = quality.get("revenue", 1.0)
    first, last = frame.iloc[:90], frame.iloc[-90:]

    aov_change, p_value = compare_periods(first["aov"], last["aov"])
    if abs(aov_change) < 0.05:
        return []

    basket_change, _ = compare_periods(first["units_per_order"], last["units_per_order"])
    price_change, _ = compare_periods(first["avg_unit_price"], last["avg_unit_price"])
    discount_change, _ = compare_periods(first["discount_rate"], last["discount_rate"])

    # Attribute the movement to whichever driver actually moved. Basket size and
    # discount rate here are flat to within a couple of percent, so a mix shift
    # is the only remaining explanation.
    mix_driven = abs(price_change) > 0.03 and abs(basket_change) < 0.03 and abs(discount_change) < 0.10
    classification = Classification.EXPLAINED if mix_driven else Classification.COMMERCIAL

    if mix_driven:
        summary = (
            f"Average order value has fallen {abs(aov_change):.0%}, from GBP "
            f"{first['aov'].median():.2f} to GBP {last['aov'].median():.2f}. This is a "
            f"product mix effect, not a pricing or promotions problem: basket size is "
            f"unchanged ({basket_change:+.1%}) and the discount rate is unchanged "
            f"({discount_change:+.1%}), while average unit price has fallen "
            f"{abs(price_change):.0%} as sales shift toward cheaper products. No "
            f"promotional action is warranted."
        )
    else:
        summary = (
            f"Average order value has fallen {abs(aov_change):.0%}. Basket size moved "
            f"{basket_change:+.1%} and discount rate {discount_change:+.1%}, so this is "
            f"not explained by product mix alone and needs investigation."
        )

    return [
        Signal(
            signal_id="aov_decline",
            signal_type=SignalType.COMPOSITION_SHIFT,
            classification=classification,
            entity="Business",
            metric="aov",
            metric_family="revenue",
            detected_at=pd.to_datetime(last["date_day"].iloc[0]).date(),
            direction="decrease" if aov_change < 0 else "increase",
            baseline_value=float(first["aov"].median()),
            current_value=float(last["aov"].median()),
            magnitude=aov_change,
            persistence_days=90,
            p_value=p_value,
            data_quality_score=dq,
            summary=summary,
            evidence={
                "aov_change": round(aov_change, 4),
                "units_per_order_change": round(basket_change, 4),
                "avg_unit_price_change": round(price_change, 4),
                "discount_rate_change": round(discount_change, 4),
                "mix_driven": mix_driven,
            },
        )
    ]


def detect_email_engagement_divergence(con, quality: dict[str, float]) -> list[Signal]:
    """Engagement metrics moving while the commercial outcome does not.

    This is the detector that separates a measurement failure from a real
    audience problem, and the logic is deliberately simple because the
    inference is strong: opens and clicks are recorded by the email platform
    through tracking pixels and redirects, while orders are recorded by
    Shopify. If the first two collapse and the third does not, the emails are
    still doing their job and the instrumentation is not.

    Getting this wrong is expensive in a specific way. The natural response to
    a 20% drop in open rate is to rewrite subject lines and prune the list,
    which costs weeks and, if the list pruning goes ahead, permanently destroys
    reachable audience to fix a bug in a pixel.
    """
    frame = query(
        con,
        """
        SELECT date_day, flow_name, recipients, opens, clicks, orders, revenue_net
        FROM semantic.email_flow_performance
        ORDER BY flow_name, date_day
        """,
    )
    dq = quality.get("email_engagement", 1.0)
    signals: list[Signal] = []

    upstream_bar = DETECTION["divergence_upstream_pct"]
    downstream_bar = DETECTION["divergence_downstream_tolerance_pct"]

    for flow, group in frame.groupby("flow_name", sort=True):
        group = group.reset_index(drop=True)
        if len(group) < 20:
            continue

        # Split at the detected changepoint in open rate rather than at an
        # arbitrary fraction of the series. Splitting into fixed thirds mixes
        # pre- and post-change weeks into the "after" block and dilutes the
        # measured drop below the detection bar, which is exactly what happened
        # here: the real fall is around 20% but a thirds split reported 13%.
        open_rate = np.where(
            group["recipients"] > 0, group["opens"] / group["recipients"], np.nan
        )
        span = cusum_changepoint_span(
            deseasonalise(group["date_day"], pd.Series(open_rate)),
            baseline_window=max(8, len(group) // 4),
            drift=DETECTION["cusum_drift"],
            threshold=DETECTION["cusum_threshold"],
        )
        if span is None:
            continue

        # Split with a guard band. The decline here is gradual rather than a
        # step: the CUSUM's origin marks where engagement started slipping and
        # its alarm marks where the evidence became conclusive, and the weeks
        # in between are transitional. Ending the baseline at the origin keeps
        # pre-change weeks pure; starting the "after" block at the alarm keeps
        # it to weeks that are certainly in the new regime. Including the
        # transition in either block dilutes the very contrast being measured.
        origin, detection = span
        early, late = group.iloc[:origin], group.iloc[detection:]
        if len(early) < 5 or len(late) < 5:
            continue
        onset_date = pd.to_datetime(group["date_day"].iloc[origin]).date()

        def rate(block: pd.DataFrame, numerator: str) -> float:
            recipients = block["recipients"].sum()
            return float(block[numerator].sum() / recipients) if recipients else float("nan")

        open_early, open_late = rate(early, "opens"), rate(late, "opens")
        click_early, click_late = rate(early, "clicks"), rate(late, "clicks")
        order_early, order_late = rate(early, "orders"), rate(late, "orders")

        rev_early = float(early["revenue_net"].sum() / early["recipients"].sum())
        rev_late = float(late["revenue_net"].sum() / late["recipients"].sum())

        open_change = (open_late - open_early) / open_early
        click_change = (click_late - click_early) / click_early
        order_change = (order_late - order_early) / order_early
        revenue_change = (rev_late - rev_early) / rev_early

        engagement_fell = open_change <= -upstream_bar and click_change <= -upstream_bar
        if not engagement_fell:
            continue

        # The test is whether the commercial outcome *deteriorated*, not whether
        # it moved. An outcome that held steady or improved while engagement
        # collapsed is evidence for a measurement fault, and improving is
        # stronger evidence than flat, not weaker. Testing the absolute size of
        # the move would perversely classify the clearest cases as audience
        # decay because the outcome moved too far in the right direction.
        outcome_deteriorated = (
            order_change < -downstream_bar or revenue_change < -downstream_bar
        )

        if not outcome_deteriorated:
            classification = Classification.DATA_QUALITY
            if order_change > downstream_bar or revenue_change > downstream_bar:
                outcome_phrase = (
                    f"orders per recipient are actually up {order_change:+.0%} and revenue "
                    f"per recipient up {revenue_change:+.0%}"
                )
            else:
                outcome_phrase = (
                    f"orders per recipient are flat ({order_change:+.1%}) and revenue per "
                    f"recipient is flat ({revenue_change:+.1%})"
                )
            summary = (
                f"{flow} open rate is down {abs(open_change):.0%} and click rate down "
                f"{abs(click_change):.0%}, but {outcome_phrase}. Opens and clicks are "
                f"measured by the email platform while orders are measured by Shopify, so "
                f"engagement falling while the commercial outcome does not points to a "
                f"tracking or deliverability fault, not to disengaged customers. "
                f"Investigate the instrumentation before changing anything about the flow."
            )
        else:
            classification = Classification.COMMERCIAL
            summary = (
                f"{flow} engagement and commercial outcome are both falling: opens "
                f"{open_change:+.0%}, clicks {click_change:+.0%}, orders per recipient "
                f"{order_change:+.0%}, revenue per recipient {revenue_change:+.0%}. "
                f"This is genuine audience decay rather than a measurement problem."
            )

        signals.append(
            Signal(
                signal_id=f"email_divergence_{flow.lower().replace(' ', '_').replace('-', '_')}",
                signal_type=SignalType.RATIO_DIVERGENCE,
                classification=classification,
                entity=flow,
                metric="open_rate",
                metric_family="email_engagement",
                detected_at=onset_date,
                direction="decrease",
                baseline_value=open_early,
                current_value=open_late,
                magnitude=open_change,
                persistence_days=int(
                    (pd.to_datetime(group["date_day"].iloc[-1]) - pd.Timestamp(onset_date)).days
                ),
                p_value=None,
                data_quality_score=dq,
                summary=summary,
                evidence={
                    "onset_date": str(onset_date),
                    "confirmed_date": str(pd.to_datetime(group["date_day"].iloc[detection]).date()),
                    "open_rate_change": round(open_change, 4),
                    "click_rate_change": round(click_change, 4),
                    "order_rate_change": round(order_change, 4),
                    "revenue_per_recipient_change": round(revenue_change, 4),
                    "open_rate_before": round(open_early, 4),
                    "open_rate_after": round(open_late, 4),
                    "order_rate_before": round(order_early, 5),
                    "order_rate_after": round(order_late, 5),
                },
            )
        )

    return _consolidate_email_signals(signals, dq)


def _consolidate_email_signals(signals: list[Signal], dq: float) -> list[Signal]:
    """Roll simultaneous per-flow divergences up into one account-level incident.

    When several independent flows lose engagement at the same time in the same
    way, they are not several problems. Welcome Series and Post-Purchase go to
    completely different audiences at different moments in the customer
    lifecycle; the only thing they share is the sending infrastructure. Their
    moving together is itself the diagnosis, and it is stronger evidence of an
    account-level fault than any single flow could be.

    It also matters operationally. Six near-identical alerts is how alerting
    systems get muted. One incident with six flows as evidence is something
    somebody actions.
    """
    divergences = [s for s in signals if s.classification is Classification.DATA_QUALITY]
    if len(divergences) < 2:
        return signals

    affected = sorted(s.entity for s in divergences)
    mean_open = float(np.mean([s.evidence["open_rate_change"] for s in divergences]))
    mean_click = float(np.mean([s.evidence["click_rate_change"] for s in divergences]))
    mean_order = float(np.mean([s.evidence["order_rate_change"] for s in divergences]))
    earliest = min(s.detected_at for s in divergences)

    account_signal = Signal(
        signal_id="email_tracking_incident_account",
        signal_type=SignalType.RATIO_DIVERGENCE,
        classification=Classification.DATA_QUALITY,
        entity="Klaviyo account",
        metric="open_rate",
        metric_family="email_engagement",
        detected_at=earliest,
        direction="decrease",
        baseline_value=float(np.mean([s.baseline_value for s in divergences])),
        current_value=float(np.mean([s.current_value for s in divergences])),
        magnitude=mean_open,
        persistence_days=max(s.persistence_days for s in divergences),
        p_value=None,
        data_quality_score=dq,
        summary=(
            f"{len(divergences)} email flows lost engagement simultaneously from around "
            f"{earliest:%d %B %Y}: open rates down {abs(mean_open):.0%} and click rates "
            f"down {abs(mean_click):.0%} on average, while orders per recipient held "
            f"({mean_order:+.1%}). The affected flows ({', '.join(affected)}) target "
            f"different audiences at different lifecycle stages and share nothing except "
            f"the sending infrastructure, so a simultaneous identical drop across all of "
            f"them is an account-level tracking or deliverability fault rather than a "
            f"content problem. Treat as one incident, not several."
        ),
        evidence={
            "flows_affected": affected,
            "flow_count": len(divergences),
            "flow_onsets": {s.entity: str(s.detected_at) for s in divergences},
            "mean_open_rate_change": round(mean_open, 4),
            "mean_click_rate_change": round(mean_click, 4),
            "mean_order_rate_change": round(mean_order, 4),
        },
    )

    # Per-flow signals are kept as evidence but demoted so they cannot each
    # spawn their own recommendation.
    consolidated = [account_signal]
    for signal in signals:
        if signal.classification is Classification.DATA_QUALITY:
            consolidated.append(
                replace(signal, classification=Classification.EXPLAINED)
            )
        else:
            consolidated.append(signal)
    return consolidated
