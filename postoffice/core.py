"""Post office sorting center CMD game core.

Turn based loop: receive packages, sort them into destination bins, load
trucks, return rejected parcels and settle the daily performance.

Design notes for the fixed behaviours:

1. Duplicate inbound parcels (same package id) are rejected, never processed
   twice.
2. Destination processing order is fully deterministic: destination code
   first, priority parcels before ordinary ones, then FIFO by receive order.
3. A truck is only mutated after every parcel fits both the slot count and
   the weight limit; otherwise loading is rejected as a whole.
4. Returning a parcel frees its inbound slot or destination bin slot.
5. Sorting never evicts a priority parcel: ordinary parcels go into free
   slots only; priority parcels take the earliest available slot.
6. A failed load changes nothing, including the truck's current weight.
7. Stats are attributed to the day the action happens on, so a parcel
   received on a later day never pollutes an earlier daily report.
8. Each day is settled at most once; the settled flag is persisted, which
   prevents stale penalties being re-applied after an archive reload.
"""

import json
import random

DEFAULT_SEED = 60
DEFAULT_INBOUND_CAPACITY = 6
DEFAULT_BIN_CAPACITY = 2
DEFAULT_TRUCK_SLOTS = 3
DEFAULT_TRUCK_WEIGHT = 10
OVERDUE_PENALTY = 1
DESTINATIONS = ("A", "B", "C", "D")


def _empty_day_stats(day):
    return {
        "day": day,
        "received": 0,
        "sorted": 0,
        "loaded": 0,
        "returned": 0,
        "penalty": 0,
        "overdue": 0,
        "settled": False,
    }


def _rng_state_to_json(state):
    version, internal, gauss = state
    return {"version": version, "internal": list(internal), "gauss": gauss}


def _rng_state_from_json(data):
    return (data["version"], tuple(data["internal"]), data["gauss"])


class PostOffice:
    def __init__(self, seed=DEFAULT_SEED, inbound_capacity=DEFAULT_INBOUND_CAPACITY,
                 bin_capacity=DEFAULT_BIN_CAPACITY, truck_slots=DEFAULT_TRUCK_SLOTS,
                 truck_weight=DEFAULT_TRUCK_WEIGHT):
        self.seed = seed
        self.day = 1
        self.turn = 0
        self.inbound_capacity = inbound_capacity
        self.bin_capacity = bin_capacity
        self.truck_slots = truck_slots
        self.truck_weight_limit = truck_weight
        self.inbound = []
        self.bins = {}
        self.truck = []
        self.truck_weight = 0
        self.returned = []
        self.packages = {}
        self.seq = 0
        self.score = 0
        self.stats = {1: _empty_day_stats(1)}
        self.settled_days = []
        self.audit = []
        self._rng = random.Random(seed)

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------
    def receive(self, package_id, destination, weight=1, priority=False, day=None):
        """Accept one parcel into the inbound area.

        Returns ``"received"`` or a stable rejection reason string.
        """
        if day is None:
            day = self.day
        if package_id in self.packages:
            self.audit.append(["duplicate", package_id, day])
            return "duplicate"
        if len(self.inbound) >= self.inbound_capacity:
            self.audit.append(["inbound_full", package_id, day])
            return "inbound_full"
        package = {
            "id": package_id,
            "destination": destination,
            "weight": weight,
            "priority": bool(priority),
            "day": day,
            "received_day": day,
            "state": "inbound",
        }
        self.packages[package_id] = package
        self.inbound.append(package_id)
        self._stats_for(day)["received"] += 1
        self.audit.append(["received", package_id, destination, day])
        return "received"

    def generate(self):
        """Generate one deterministic parcel from the seeded RNG."""
        package_id = "P%03d" % (self.seq + 1)
        self.seq += 1
        destination = self._rng.choice(DESTINATIONS)
        weight = self._rng.randint(1, self.truck_weight_limit)
        priority = self._rng.random() < 0.3
        return self.receive(package_id, destination, weight, priority)

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------
    def _sort_key(self, package_id):
        package = self.packages[package_id]
        return (package["destination"], 0 if package["priority"] else 1,
                package["received_day"], self.inbound.index(package_id))

    def sort(self):
        """Sort every inbound parcel into destination bins.

        Destination order is stable (destination, priority, FIFO) and no
        parcel is ever evicted: parcels that do not fit stay inbound.
        """
        moved = 0
        for package_id in sorted(self.inbound, key=self._sort_key):
            package = self.packages[package_id]
            destination = package["destination"]
            bin_parcels = self.bins.setdefault(destination, [])
            if len(bin_parcels) >= self.bin_capacity:
                self.audit.append(["bin_full", package_id, destination, self.day])
                continue
            bin_parcels.append(package_id)
            package["state"] = "binned"
            self.inbound.remove(package_id)
            moved += 1
            self.stats[self.day]["sorted"] += 1
            self.audit.append(["sorted", package_id, destination, self.day])
        return moved

    # ------------------------------------------------------------------
    # Truck loading
    # ------------------------------------------------------------------
    def load(self, destination, weight_limit=None):
        """Load parcels of one destination bin onto the truck.

        The whole load is validated before any state changes, so a rejected
        load leaves bin, truck and truck weight untouched.
        """
        if weight_limit is None:
            weight_limit = self.truck_weight_limit
        bin_parcels = list(self.bins.get(destination, []))
        if not bin_parcels:
            return "empty"
        total_weight = sum(self.packages[pid]["weight"] for pid in bin_parcels)
        if len(self.truck) + len(bin_parcels) > self.truck_slots:
            self.audit.append(["load_rejected_slots", destination, self.day])
            return "no_slots"
        if self.truck_weight + total_weight > weight_limit:
            self.audit.append(["load_rejected_weight", destination, total_weight, self.day])
            return "overweight"
        for package_id in bin_parcels:
            package = self.packages[package_id]
            package["state"] = "loaded"
            package["loaded_day"] = self.day
            self.truck.append(package_id)
            self.stats[self.day]["loaded"] += 1
        self.truck_weight += total_weight
        self.bins[destination] = []
        self.audit.append(["loaded", destination, bin_parcels, total_weight, self.day])
        return "loaded"

    # ------------------------------------------------------------------
    # Returns
    # ------------------------------------------------------------------
    def return_package(self, package_id):
        """Return a parcel, releasing whatever slot it currently occupies."""
        package = self.packages.get(package_id)
        if package is None:
            return "unknown"
        state = package["state"]
        if state == "inbound":
            self.inbound.remove(package_id)
        elif state == "binned":
            bin_parcels = self.bins.get(package["destination"], [])
            if package_id in bin_parcels:
                bin_parcels.remove(package_id)
        elif state == "returned":
            return "already_returned"
        else:
            return "not_returnable"
        package["state"] = "returned"
        package["return_day"] = self.day
        self.returned.append(package_id)
        self.stats[self.day]["returned"] += 1
        self.audit.append(["returned", package_id, self.day])
        return "returned"

    # ------------------------------------------------------------------
    # Dispatch / daily settlement
    # ------------------------------------------------------------------
    def dispatch(self):
        """Dispatch the truck; only loaded parcels count toward the score."""
        count = len(self.truck)
        weight = self.truck_weight
        for package_id in self.truck:
            package = self.packages[package_id]
            package["state"] = "dispatched"
            package["dispatch_day"] = self.day
        self.audit.append(["dispatched", count, weight, self.day])
        self.truck = []
        self.truck_weight = 0
        return count

    def _stats_for(self, day):
        stats = self.stats.get(day)
        if stats is None:
            stats = _empty_day_stats(day)
            self.stats[day] = stats
        return stats

    def settle(self):
        """Settle the current day exactly once.

        Parcels still inbound at the first settlement after they were
        received incur one overdue penalty each. Repeated calls are no-ops
        and reloads restore the settled marker, so penalties never double
        count.
        """
        stats = self.stats[self.day]
        if stats["settled"]:
            self.audit.append(["settle_skipped", self.day])
            return "already_settled"
        # A parcel left inbound on the first settlement after it was
        # received is overdue; it is only charged at that one point.
        overdue = [pid for pid in self.inbound
                   if self.packages[pid]["received_day"] == self.day - 1]
        penalty = len(overdue) * OVERDUE_PENALTY
        stats["overdue"] = len(overdue)
        stats["penalty"] = penalty
        stats["settled"] = True
        self.settled_days.append(self.day)
        self.score -= penalty
        self.audit.append(["settled", self.day, overdue, penalty])
        return "settled"

    def next_day(self):
        """Settle (once) and advance to the next day."""
        self.settle()
        self.day += 1
        self._stats_for(self.day)
        self.turn += 1
        self.audit.append(["next_day", self.day])
        return self.day

    # ------------------------------------------------------------------
    # CMD dispatch
    # ------------------------------------------------------------------
    def cmd(self, line):
        parts = line.split()
        if not parts:
            raise ValueError("empty command")
        name, args = parts[0], parts[1:]
        if name == "receive":
            if len(args) < 2:
                raise ValueError("receive requires id destination [weight] [priority]")
            package_id, destination = args[0], args[1]
            weight = int(args[2]) if len(args) > 2 else 1
            priority = len(args) > 3 and args[3] in ("1", "priority", "true")
            return self.receive(package_id, destination, weight, priority)
        if name == "generate":
            return self.generate()
        if name == "sort":
            return str(self.sort())
        if name == "load":
            if not args:
                raise ValueError("load requires destination")
            weight_limit = int(args[1]) if len(args) > 1 else None
            return self.load(args[0], weight_limit)
        if name == "return":
            if not args:
                raise ValueError("return requires package id")
            return self.return_package(args[0])
        if name == "dispatch":
            return str(self.dispatch())
        if name == "settle":
            return self.settle()
        if name == "nextday":
            return str(self.next_day())
        if name == "status":
            return self.status()
        if name == "save":
            if not args:
                raise ValueError("save requires path")
            self.save_to(args[0])
            return "saved"
        if name == "load":
            if not args:
                raise ValueError("load requires path")
            self.load_from(args[0])
            return "loaded"
        raise ValueError("unknown command: %s" % name)

    def status(self):
        return ("day=%d inbound=%d bins=%s truck=%d/%d weight=%d/%d score=%d"
                % (self.day, len(self.inbound),
                   {d: len(p) for d, p in self.bins.items()},
                   len(self.truck), self.truck_slots,
                   self.truck_weight, self.truck_weight_limit, self.score))

    # ------------------------------------------------------------------
    # JSON archive
    # ------------------------------------------------------------------
    def save_to(self, path):
        state = {
            "seed": self.seed,
            "day": self.day,
            "turn": self.turn,
            "inbound_capacity": self.inbound_capacity,
            "bin_capacity": self.bin_capacity,
            "truck_slots": self.truck_slots,
            "truck_weight_limit": self.truck_weight_limit,
            "inbound": list(self.inbound),
            "bins": {d: list(p) for d, p in self.bins.items()},
            "truck": list(self.truck),
            "truck_weight": self.truck_weight,
            "returned": list(self.returned),
            "packages": self.packages,
            "seq": self.seq,
            "score": self.score,
            "stats": {str(d): s for d, s in self.stats.items()},
            "settled_days": list(self.settled_days),
            "audit": [list(event) for event in self.audit],
            "rng_state": _rng_state_to_json(self._rng.getstate()),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)

    def load_from(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.seed = state["seed"]
        self.day = state["day"]
        self.turn = state.get("turn", 0)
        self.inbound_capacity = state["inbound_capacity"]
        self.bin_capacity = state["bin_capacity"]
        self.truck_slots = state["truck_slots"]
        self.truck_weight_limit = state["truck_weight_limit"]
        self.inbound = list(state["inbound"])
        self.bins = {d: list(p) for d, p in state["bins"].items()}
        self.truck = list(state["truck"])
        self.truck_weight = state["truck_weight"]
        self.returned = list(state["returned"])
        self.packages = dict(state["packages"])
        self.seq = state["seq"]
        self.score = state["score"]
        self.stats = {int(d): dict(s) for d, s in state["stats"].items()}
        self.settled_days = list(state["settled_days"])
        self.audit = [list(event) for event in state["audit"]]
        self._rng = random.Random(self.seed)
        self._rng.setstate(_rng_state_from_json(state["rng_state"]))
        return self
