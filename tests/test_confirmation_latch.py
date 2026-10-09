"""`utils/confirmation.py::ConfirmationLatch` testleri: sesli ve düğmeli onayın yarışı.

Karar mantığının tamamı burada; overlay ve sesli döngü yalnızca bu sınıfa karar yazar.
"""

from __future__ import annotations

import threading

from utils.confirmation import ConfirmationLatch


def test_no_decision_means_refusal() -> None:
    latch = ConfirmationLatch()

    assert latch.is_decided() is False
    assert latch.approved is False, "karar yokken güvenli taraf RED olmalı"
    assert latch.source == ""


def test_first_decision_wins_and_later_ones_are_ignored() -> None:
    latch = ConfirmationLatch()

    latch.decide(True, "düğme")
    latch.decide(False, "ses")

    assert latch.is_decided() is True
    assert latch.approved is True
    assert latch.source == "düğme"


def test_concurrent_decisions_produce_exactly_one_consistent_winner() -> None:
    latch = ConfirmationLatch()

    def decide(index: int) -> None:
        latch.decide(index % 2 == 0, f"t{index}")

    threads = [threading.Thread(target=decide, args=(i,)) for i in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Kazananın kaynağı ve değeri birbiriyle tutarlı olmalı (karışmamalı).
    winner = int(latch.source[1:])
    assert latch.approved is (winner % 2 == 0)
