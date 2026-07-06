"""
LENS CLARITY Time-Series Income Reconstruction (Feature 5)
===========================================================
Seasonality-aware alternative to the flat 90-day average for gig/freelancer/
self-employed customers with enough UPI_CREDIT history.

Only used when:
  1. Customer is non-salaried (Gig Worker, Freelancer, or Self-Employed)
  2. >= 12 UPI_CREDIT transactions are available
  3. >= 8 weekly buckets can be formed

Falls back gracefully (returns None) otherwise — caller uses original engine.
"""


def reconstruct_income_timeseries(txns: list) -> dict | None:
    """
    Attempt STL time-series decomposition over weekly UPI_CREDIT sums.
    Returns {"synthetic_monthly_income": float, "method": str} or None.
    """
    credits = [t for t in txns if t.get("type") == "UPI_CREDIT" and t.get("amount", 0) > 100]
    if len(credits) < 12:
        return None

    try:
        import pandas as pd
        from statsmodels.tsa.seasonal import STL

        df = pd.DataFrame(credits)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()

        weekly = df["amount"].resample("W").sum()
        if len(weekly) < 8:
            return None

        stl = STL(weekly, period=4, robust=True)   # ~4-week seasonal cycle
        result = stl.fit()

        # Project forward using trend + seasonal from the last 4 weeks
        recent_trend    = float(result.trend[-4:].mean())
        recent_seasonal = float(result.seasonal[-4:].mean())
        projected_weekly = max(recent_trend + recent_seasonal, 0.0)
        projected_monthly = round(projected_weekly * 4.33, 2)

        return {
            "synthetic_monthly_income": projected_monthly,
            "method": f"STL time-series decomposition (trend + seasonal, {len(weekly)}-week series, 4-week cycle)",
        }

    except ImportError:
        return None   # statsmodels not installed
    except Exception as e:
        print(f"[clarity_ts] STL decomposition failed: {e} — using flat-average fallback")
        return None
