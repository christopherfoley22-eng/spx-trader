from dataclasses import dataclass

@dataclass
class C:
    symbol: str = "SPX"
    secType: str = "OPT"
    tradingClass: str = "SPXW"
    expiration: str = "20260921"
    right: str = "C"
    strike: float = 6700.0
    multiplier: str = "100"
    currency: str = "USD"
    conId: int = 123456789
    bid: float = 10.00
    ask: float = 10.10

def gate(c, requested_right="C"):
    checks = [
        (c.symbol == "SPX", "WRONG_SYMBOL"),
        (c.secType == "OPT", "WRONG_SECURITY_TYPE"),
        (c.tradingClass == "SPXW", "WRONG_TRADING_CLASS"),
        (c.expiration == "20260921", "WRONG_EXPIRATION"),
        (c.right == requested_right, "WRONG_OPTION_RIGHT"),
        (c.strike == 6700.0, "WRONG_STRIKE"),
        (c.multiplier == "100", "WRONG_MULTIPLIER"),
        (c.currency == "USD", "WRONG_CURRENCY"),
        (c.conId > 0, "INVALID_CONID"),
        (c.bid is not None and c.bid > 0, "INVALID_BID"),
        (c.ask is not None and c.ask > 0, "INVALID_ASK"),
        (
            c.bid is not None and
            c.ask is not None and
            c.ask >= c.bid,
            "CROSSED_MARKET"
        ),
    ]

    for ok, reason in checks:
        if not ok:
            return False, reason

    return True, "EXACT_CONTRACT_CONFIRMED"

def test(name, mutate=None, requested="C", expected=None):
    c = C()

    if mutate:
        field, value = mutate
        setattr(c, field, value)

    allowed, reason = gate(c, requested)

    if expected is None:
        assert allowed
    else:
        assert not allowed
        assert reason == expected, (reason, expected)

    print(name + ": PASS")

print()
print("========================================")
print("EXACT SPXW CONTRACT SAFETY TESTS")
print("========================================")

test("1. EXACT CONTRACT")
test("2. WRONG SYMBOL BLOCKED",
     ("symbol", "SPY"), expected="WRONG_SYMBOL")
test("3. WRONG SECURITY TYPE BLOCKED",
     ("secType", "IND"), expected="WRONG_SECURITY_TYPE")
test("4. WRONG TRADING CLASS BLOCKED",
     ("tradingClass", "SPX"), expected="WRONG_TRADING_CLASS")
test("5. WRONG EXPIRATION BLOCKED",
     ("expiration", "20260922"), expected="WRONG_EXPIRATION")
test("6. WRONG CALL/PUT BLOCKED",
     ("right", "P"), expected="WRONG_OPTION_RIGHT")
test("7. WRONG STRIKE BLOCKED",
     ("strike", 6705.0), expected="WRONG_STRIKE")
test("8. WRONG MULTIPLIER BLOCKED",
     ("multiplier", "50"), expected="WRONG_MULTIPLIER")
test("9. WRONG CURRENCY BLOCKED",
     ("currency", "EUR"), expected="WRONG_CURRENCY")
test("10. INVALID CONID BLOCKED",
     ("conId", 0), expected="INVALID_CONID")
test("11. INVALID BID BLOCKED",
     ("bid", 0), expected="INVALID_BID")
test("12. INVALID ASK BLOCKED",
     ("ask", 0), expected="INVALID_ASK")
test("13. CROSSED MARKET BLOCKED",
     ("bid", 10.20), expected="CROSSED_MARKET")

p = C(right="P")
allowed, reason = gate(p, "P")
assert allowed
print("14. EXACT PUT CONTRACT: PASS")

allowed, reason = gate(p, "C")
assert not allowed and reason == "WRONG_OPTION_RIGHT"
print("15. PUT/CALL MISMATCH BLOCKED: PASS")

print()
print("========================================")
print("ALL EXACT-CONTRACT ATTACK TESTS PASS")
print("WRONG CONTRACTS FAIL CLOSED")
print("SIMULATION ONLY")
print("ZERO IBKR ORDERS")
print("========================================")
