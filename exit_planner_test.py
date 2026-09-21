def plan_exit_chunks(contracts, max_chunk=10):
    """
    Break an EXIT ALL quantity into smaller market-order chunks.

    Example with max_chunk=10:
      25 -> [10, 10, 5]
      19 -> [10, 9]
       7 -> [7]
    """
    if contracts < 0:
        raise ValueError("contracts cannot be negative")

    if max_chunk <= 0:
        raise ValueError("max_chunk must be positive")

    chunks = []
    remaining = contracts

    while remaining > 0:
        qty = min(max_chunk, remaining)
        chunks.append(qty)
        remaining -= qty

    return chunks


def verify(contracts, chunk_size, expected):
    result = plan_exit_chunks(contracts, chunk_size)

    print(
        f"{contracts:>2} contracts | "
        f"max chunk {chunk_size:>2} -> {result}"
    )

    assert result == expected
    assert sum(result) == contracts
    assert all(0 < q <= chunk_size for q in result)


print("\nTESTING 10-CONTRACT EXIT CHUNKS")
verify(25, 10, [10, 10, 5])
verify(24, 10, [10, 10, 4])
verify(21, 10, [10, 10, 1])
verify(20, 10, [10, 10])
verify(19, 10, [10, 9])
verify(11, 10, [10, 1])
verify(10, 10, [10])
verify(9, 10, [9])
verify(3, 10, [3])
verify(1, 10, [1])
verify(0, 10, [])

print("\nTESTING 5-CONTRACT EXIT CHUNKS")
verify(25, 5, [5, 5, 5, 5, 5])
verify(23, 5, [5, 5, 5, 5, 3])
verify(19, 5, [5, 5, 5, 4])
verify(11, 5, [5, 5, 1])
verify(7, 5, [5, 2])
verify(5, 5, [5])
verify(2, 5, [2])
verify(1, 5, [1])

print("\nTESTING BAD INPUT")
try:
    plan_exit_chunks(-1)
    raise AssertionError("Negative quantity was accepted")
except ValueError:
    print("Negative quantity correctly rejected")

try:
    plan_exit_chunks(25, 0)
    raise AssertionError("Zero chunk size was accepted")
except ValueError:
    print("Zero chunk size correctly rejected")

print("\n================================")
print("ALL EXIT-PLANNER TESTS PASS")
print("NO IBKR ORDERS WERE SUBMITTED")
print("================================")
