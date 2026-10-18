import gas_reserve_alert as g

def test_decide():
    d = g.decide
    assert d(197.2, "ok", 1000) == (None, "ok")                       # healthy: silent
    t, s = d(49.9, "ok", 1000); assert "CRITICAL" in t and g.RESERVE_ADDRESS in t and s == "low:1000"
    assert d(40, "low:1000", 1000 + 3600) == (None, "low:1000")       # no spam within 24 h
    t, s = d(40, "low:1000", 1000 + 86400); assert t and s == f"low:{1000 + 86400}"  # daily reminder
    t, s = d(60, "low:1000", 5000); assert "recharged" in t and s == "ok"
    assert d(50.0, "ok", 1) == (None, "ok")                            # exactly 5 % is not below

if __name__ == "__main__":
    test_decide(); print("decide ok")
    print("live reserveBalance:", round(g.read_reserve_mon(), 3), "MON")
    print("---"); print(g._low_text(42.5))
