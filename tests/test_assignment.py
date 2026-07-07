from backend import assignment


class FakeDB:
    def __init__(self):
        self.inserted = []

    def rows(self, conn, query, params=None):
        if "FROM users" in query:
            return conn.rms
        return []

    def execute(self, conn, query, params=None):
        self.inserted.append(params)


class FakeConn:
    def __init__(self, rms):
        self.rms = rms

    def commit(self):
        pass


def test_no_rms_available_returns_empty_assignment():
    fake_db = FakeDB()
    conn = FakeConn([])
    result = assignment.assign_leads_to_rms(conn, fake_db, [{"customer_id": "C1", "trust_score": 80, "tier": "Tier 1"}])
    assert result["assigned"] == []
    assert result["unassigned_reason"] == "No RM capacity available"


def test_leads_distributed_round_robin_across_two_rms():
    fake_db = FakeDB()
    conn = FakeConn([
        {"user_id": 1, "name": "RM One", "max_daily_leads": 15, "active_assigned_count": 0},
        {"user_id": 2, "name": "RM Two", "max_daily_leads": 15, "active_assigned_count": 0},
    ])
    leads = [{"customer_id": f"C{i}", "trust_score": 80, "tier": "Tier 1"} for i in range(4)]
    result = assignment.assign_leads_to_rms(conn, fake_db, leads)
    assigned_rms = [a["assigned_rm_id"] for a in result["assigned"]]
    assert assigned_rms.count(1) == 2 and assigned_rms.count(2) == 2


def test_stops_assigning_once_all_rms_at_capacity():
    fake_db = FakeDB()
    conn = FakeConn([{"user_id": 1, "name": "RM One", "max_daily_leads": 2, "active_assigned_count": 0}])
    leads = [{"customer_id": f"C{i}", "trust_score": 80, "tier": "Tier 1"} for i in range(5)]
    result = assignment.assign_leads_to_rms(conn, fake_db, leads)
    assert result["assigned_count"] == 2
    assert result["still_unassigned_count"] == 3
