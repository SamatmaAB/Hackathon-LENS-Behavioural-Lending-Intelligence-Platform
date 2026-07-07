from backend import geo


class FakeDB:
    @staticmethod
    def rows(conn, query, params=()):
        return conn  # conn is pre-loaded with fake rows in these tests


def test_empty_leads_returns_empty_list():
    result = geo.build_geo_distribution(conn=[], db=FakeDB)
    assert result == []


def test_unknown_city_is_skipped_without_error():
    rows = [{"city": "Atlantis", "state": "Nowhere", "trust_score": 80, "tier": "Tier 1"}]
    result = geo.build_geo_distribution(conn=rows, db=FakeDB)
    assert result == []  # Atlantis isn't in CITY_COORDS — silently skipped, not an error


def test_aggregates_correctly_across_multiple_leads_same_city():
    rows = [
        {"city": "Mumbai", "state": "Maharashtra", "trust_score": 80, "tier": "Tier 1"},
        {"city": "Mumbai", "state": "Maharashtra", "trust_score": 60, "tier": "Tier 2"},
        {"city": "Pune", "state": "Maharashtra", "trust_score": 90, "tier": "Tier 1"},
    ]
    result = geo.build_geo_distribution(conn=rows, db=FakeDB)
    mumbai = next(r for r in result if r["city"] == "Mumbai")
    assert mumbai["lead_count"] == 2
    assert mumbai["tier1_count"] == 1
    assert mumbai["avg_trust_score"] == 70.0  # (80+60)/2


def test_null_trust_score_treated_as_zero_not_error():
    rows = [{"city": "Chennai", "state": "Tamil Nadu", "trust_score": None, "tier": "Tier 2"}]
    result = geo.build_geo_distribution(conn=rows, db=FakeDB)
    assert result[0]["avg_trust_score"] == 0.0


def test_result_sorted_by_lead_count_descending():
    rows = (
        [{"city": "Pune", "state": "Maharashtra", "trust_score": 70, "tier": "Tier 2"}] +
        [{"city": "Mumbai", "state": "Maharashtra", "trust_score": 70, "tier": "Tier 2"}] * 3
    )
    result = geo.build_geo_distribution(conn=rows, db=FakeDB)
    assert result[0]["city"] == "Mumbai"
    assert result[1]["city"] == "Pune"
