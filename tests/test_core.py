import json
import os
import tempfile
import unittest

from postoffice.core import PostOffice


class PostOfficeTest(unittest.TestCase):
    def setUp(self):
        self.game = PostOffice()

    def _archive_path(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    # 1. The same parcel must not enter the inbound area twice.
    def test_duplicate_inbound_rejected_once(self):
        self.assertEqual(self.game.receive("p1", "A", 1), "received")
        self.assertEqual(self.game.receive("p1", "A", 1), "duplicate")
        self.assertEqual(len(self.game.inbound), 1)
        self.assertEqual(self.game.stats[1]["received"], 1)

    # 2. Destination processing order is deterministic and stable.
    def test_destination_sort_is_stable(self):
        for pid, dest in [("b1", "B"), ("a1", "A"), ("a2", "A")]:
            self.assertEqual(self.game.receive(pid, dest), "received")
        self.assertEqual(self.game.sort(), 3)
        self.assertEqual(self.game.bins["A"], ["a1", "a2"])
        self.assertEqual(self.game.bins["B"], ["b1"])
        self.assertEqual(self.game.inbound, [])

    def test_priority_before_ordinary_within_destination(self):
        for pid, priority in [("n1", False), ("p1", True),
                              ("n2", False), ("p2", True)]:
            self.game.receive(pid, "A", 1, priority)
        self.assertEqual(self.game.sort(), 2)
        self.assertEqual(self.game.bins["A"], ["p1", "p2"])
        self.assertEqual(sorted(self.game.inbound), ["n1", "n2"])

    # 3. A full truck rejects the load instead of overloading it.
    def test_load_rejected_when_slots_full(self):
        game = PostOffice(truck_slots=3)
        for pid, dest in [("a1", "A"), ("a2", "A"),
                          ("b1", "B"), ("b2", "B")]:
            game.receive(pid, dest)
        game.sort()
        self.assertEqual(game.load("A"), "loaded")
        self.assertEqual(len(game.truck), 2)
        self.assertEqual(game.load("B"), "no_slots")
        self.assertEqual(game.bins["B"], ["b1", "b2"])
        self.assertEqual(len(game.truck), 2)

    # 4. Returning a parcel releases its bin or inbound slot.
    def test_return_releases_bin_slot(self):
        game = PostOffice(bin_capacity=2)
        game.receive("a1", "A")
        game.receive("a2", "A")
        game.sort()
        self.assertEqual(game.return_package("a1"), "returned")
        self.assertEqual(len(game.bins["A"]), 1)
        game.receive("a3", "A")
        self.assertEqual(game.sort(), 1)
        self.assertEqual(game.bins["A"], ["a2", "a3"])

    def test_return_releases_inbound_slot(self):
        game = PostOffice(inbound_capacity=1)
        game.receive("x1", "A")
        self.assertEqual(game.receive("x2", "A"), "inbound_full")
        self.assertEqual(game.return_package("x1"), "returned")
        self.assertEqual(game.receive("x2", "A"), "received")

    # 6. A failed load rolls weight back (and changes nothing else).
    def test_failed_load_rolls_back_weight(self):
        game = PostOffice(truck_weight=3)
        game.receive("a1", "A", 2)
        game.receive("b1", "B", 4)
        game.sort()
        self.assertEqual(game.load("A"), "loaded")
        self.assertEqual(game.truck_weight, 2)
        self.assertEqual(game.load("B"), "overweight")
        self.assertEqual(game.truck_weight, 2)
        self.assertEqual(game.bins["B"], ["b1"])
        self.assertEqual(game.packages["b1"]["state"], "binned")
        game.receive("b2", "B", 1)
        game.return_package("b1")
        game.sort()
        self.assertEqual(game.load("B"), "loaded")
        self.assertEqual(game.truck_weight, 3)

    # 7. Cross-day parcels are attributed to the day they arrive/act on.
    def test_cross_day_stats_attribution(self):
        self.game.receive("p1", "A")
        self.game.next_day()
        self.assertEqual(self.game.day, 2)
        self.game.receive("p2", "A")
        self.assertEqual(self.game.stats[1]["received"], 1)
        self.assertEqual(self.game.stats[2]["received"], 1)
        self.assertEqual(self.game.stats[1]["penalty"], 0)
        self.game.return_package("p2")
        self.game.next_day()
        self.assertEqual(self.game.stats[2]["penalty"], 1)
        self.assertEqual(self.game.stats[2]["returned"], 1)
        self.assertEqual(self.game.score, -1)
        self.game.next_day()
        self.assertEqual(self.game.stats[3]["penalty"], 0)
        self.assertEqual(self.game.score, -1)

    # 8. Each day settles once, including after a reload.
    def test_settle_once_and_reload_no_double_penalty(self):
        path = self._archive_path()
        self.game.receive("p1", "A")
        self.game.next_day()
        self.game.settle()
        score_before = self.game.score
        self.assertEqual(self.game.settle(), "already_settled")
        self.assertEqual(self.game.score, score_before)
        self.game.save_to(path)
        restored = PostOffice().load_from(path)
        self.assertEqual(restored.settle(), "already_settled")
        self.assertEqual(restored.score, score_before)
        self.assertEqual(restored.stats[2]["penalty"], 1)

    # Save/load round trips the whole game and continues the RNG sequence.
    def test_save_load_roundtrip_and_seed(self):
        path = self._archive_path()
        reference = PostOffice()
        self.game.generate()
        self.game.sort()
        self.game.save_to(path)
        restored = PostOffice().load_from(path)
        self.assertEqual(restored.inbound, self.game.inbound)
        self.assertEqual(restored.bins, self.game.bins)
        self.assertEqual(restored.packages, self.game.packages)
        self.assertEqual(restored.generate(), reference.generate())

    def test_fixed_seed_deterministic(self):
        first = PostOffice(seed=123)
        second = PostOffice(seed=123)
        for _ in range(5):
            self.assertEqual(first.generate(), second.generate())

    # Illegal commands.
    def test_illegal_commands_rejected(self):
        with self.assertRaises(ValueError):
            self.game.cmd("")
        with self.assertRaises(ValueError):
            self.game.cmd("nope")
        with self.assertRaises(ValueError):
            self.game.cmd("receive onlyid")
        with self.assertRaises(ValueError):
            self.game.cmd("load")

    # Empty data is a harmless no-op.
    def test_empty_data_operations(self):
        self.assertEqual(self.game.sort(), 0)
        self.assertEqual(self.game.load("A"), "empty")
        self.assertEqual(self.game.dispatch(), 0)
        self.assertEqual(self.game.return_package("ghost"), "unknown")
        self.assertEqual(self.game.settle(), "settled")
        self.assertEqual(self.game.settle(), "already_settled")

    # Capacity boundaries.
    def test_inbound_capacity_boundary(self):
        game = PostOffice(inbound_capacity=2)
        self.assertEqual(game.receive("q1", "A"), "received")
        self.assertEqual(game.receive("q2", "B"), "received")
        self.assertEqual(game.receive("q3", "C"), "inbound_full")

    def test_bin_capacity_boundary(self):
        game = PostOffice(bin_capacity=1)
        game.receive("a1", "A")
        game.receive("a2", "A")
        self.assertEqual(game.sort(), 1)
        self.assertEqual(game.bins["A"], ["a1"])
        self.assertEqual(game.inbound, ["a2"])

    # Duplicate input through the CMD front door.
    def test_duplicate_input_via_cmd(self):
        self.assertEqual(self.game.cmd("receive z1 A 1"), "received")
        self.assertEqual(self.game.cmd("receive z1 A 1"), "duplicate")
        self.assertEqual(len(self.game.inbound), 1)

    def test_cmd_dispatch_full_turn(self):
        self.assertEqual(self.game.cmd("receive a1 A 3"), "received")
        self.assertEqual(self.game.cmd("sort"), "1")
        self.assertEqual(self.game.cmd("load A"), "loaded")
        self.assertEqual(self.game.cmd("dispatch"), "1")
        self.assertEqual(self.game.truck, [])
        self.assertEqual(self.game.truck_weight, 0)

    def test_archive_is_json(self):
        path = self._archive_path()
        self.game.receive("j1", "A")
        self.game.save_to(path)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("packages", data)
        self.assertIn("rng_state", data)
        self.assertEqual(data["settled_days"], [])

    # --- Minimal added regression tests for the fixed behaviours ---

    def test_duplicate_rejected_after_leave_and_return_again(self):
        game = self.game
        game.receive("d1", "A")
        game.return_package("d1")
        self.assertEqual(game.receive("d1", "B"), "duplicate")
        self.assertEqual(game.return_package("d1"), "already_returned")
        self.assertEqual(game.stats[1]["received"], 1)
        self.assertEqual(game.stats[1]["returned"], 1)

    def test_loaded_or_dispatched_parcel_not_returnable(self):
        game = self.game
        game.receive("l1", "A")
        game.sort()
        game.load("A")
        self.assertEqual(game.return_package("l1"), "not_returnable")
        game.dispatch()
        self.assertEqual(game.return_package("l1"), "not_returnable")
        self.assertEqual(len(game.returned), 0)

    def test_priority_parcel_kept_when_bin_full_at_boundary(self):
        game = PostOffice(bin_capacity=1, inbound_capacity=3)
        game.receive("n1", "A", 1, False)
        game.receive("p1", "A", 1, True)
        self.assertEqual(game.sort(), 1)
        self.assertEqual(game.bins["A"], ["p1"])
        self.assertEqual(game.inbound, ["n1"])
        game.return_package("p1")
        self.assertEqual(game.sort(), 1)
        self.assertEqual(game.bins["A"], ["n1"])

    def test_failed_slot_load_does_not_touch_weight_or_stats(self):
        game = PostOffice(truck_slots=2, truck_weight=10)
        game.receive("a1", "A", 4)
        game.receive("a2", "A", 4)
        game.receive("b1", "B", 4)
        game.sort()
        self.assertEqual(game.load("A"), "loaded")
        self.assertEqual(game.load("B"), "no_slots")
        self.assertEqual(game.truck_weight, 8)
        self.assertEqual(game.stats[1]["loaded"], 2)
        self.assertEqual(game.packages["b1"]["state"], "binned")

    def test_exact_limits_load_succeeds_at_boundary(self):
        game = PostOffice(truck_slots=2, truck_weight=5)
        game.receive("a1", "A", 2)
        game.receive("a2", "A", 3)
        game.sort()
        self.assertEqual(game.load("A"), "loaded")
        self.assertEqual(game.truck_weight, 5)
        self.assertEqual(len(game.truck), 2)

    def test_bad_weight_and_return_without_id_rejected(self):
        with self.assertRaises(ValueError):
            self.game.cmd("receive w1 A nope")
        with self.assertRaises(ValueError):
            self.game.cmd("return")
        with self.assertRaises(ValueError):
            self.game.cmd("save")

    def test_reload_then_advance_settles_next_day_once(self):
        path = self._archive_path()
        self.game.receive("p1", "A")
        self.game.next_day()
        self.game.save_to(path)
        restored = PostOffice().load_from(path)
        self.assertEqual(restored.stats[1]["settled"], True)
        restored.next_day()
        self.assertEqual(restored.day, 3)
        self.assertTrue(restored.stats[2]["settled"])
        self.assertEqual(restored.stats[2]["penalty"], 1)
        self.assertEqual(restored.settle(), "settled")
        self.assertEqual(restored.settle(), "already_settled")
        restored.save_to(path)
        again = PostOffice().load_from(path)
        self.assertEqual(again.day, 3)
        self.assertEqual(again.settle(), "already_settled")
        self.assertEqual(again.score, -1)


if __name__ == "__main__":
    unittest.main()
