"""
LENS GEO - City-level lead concentration (Feature 6, additive/bonus)
=====================================================================
Maps each of the 12 synthetic-dataset cities to approximate lat/lng and
aggregates lead counts / avg trust score per city so the frontend can
render an illustrative concentration map. This is NOT a precision GIS
trace - coordinates are city-centroid approximations for visualization
only, projected with a simple linear equirectangular transform.
"""

# (city_name -> (lat, lng)). Matches backend/data_gen.py CITIES list.
CITY_COORDS = {
    "Mumbai": (19.0760, 72.8777),
    "Bengaluru": (12.9716, 77.5946),
    "Pune": (18.5204, 73.8567),
    "Chennai": (13.0827, 80.2707),
    "Hyderabad": (17.3850, 78.4867),
    "Delhi": (28.7041, 77.1025),
    "Ahmedabad": (23.0225, 72.5714),
    "Jaipur": (26.9124, 75.7873),
    "Kochi": (9.9312, 76.2673),
    "Lucknow": (26.8467, 80.9462),
    "Nagpur": (21.1458, 79.0882),
    "Indore": (22.7196, 75.8577),
}


def build_geo_distribution(conn, db):
    """
    Aggregates leads by customer city. Returns a list of dicts:
    [{city, state, lat, lng, lead_count, tier1_count, avg_trust_score}, ...]
    Cities with zero leads are omitted. Unknown cities (not in CITY_COORDS)
    are skipped from the map but do not error the endpoint.
    """
    rows = db.rows(
        conn,
        """
        SELECT c.city, c.state, l.trust_score, l.tier
        FROM leads l
        JOIN customers c ON c.customer_id = l.customer_id
        """,
    )

    agg = {}
    for row in rows:
        city = row.get("city")
        if city is None or city not in CITY_COORDS:
            continue
        bucket = agg.setdefault(city, {
            "city": city,
            "state": row.get("state"),
            "lat": CITY_COORDS[city][0],
            "lng": CITY_COORDS[city][1],
            "lead_count": 0,
            "tier1_count": 0,
            "_trust_sum": 0.0,
        })
        bucket["lead_count"] += 1
        bucket["_trust_sum"] += row["trust_score"] or 0.0
        if row["tier"] == "Tier 1":
            bucket["tier1_count"] += 1

    result = []
    for bucket in agg.values():
        avg_trust = round(bucket["_trust_sum"] / bucket["lead_count"], 1) if bucket["lead_count"] else 0.0
        result.append({
            "city": bucket["city"],
            "state": bucket["state"],
            "lat": bucket["lat"],
            "lng": bucket["lng"],
            "lead_count": bucket["lead_count"],
            "tier1_count": bucket["tier1_count"],
            "avg_trust_score": avg_trust,
        })

    result.sort(key=lambda r: r["lead_count"], reverse=True)
    return result
