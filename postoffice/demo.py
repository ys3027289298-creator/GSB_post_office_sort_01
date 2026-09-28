"""Fixed-seed, non-interactive demo run of the sorting center."""

from .core import PostOffice

SCRIPT = [
    "receive p1 A 3 1",
    "receive p2 A 2 0",
    "receive p3 B 8 0",
    "receive p1 A 3 1",
    "sort",
    "load A",
    "load B",
    "dispatch",
    "settle",
    "nextday",
    "generate",
    "generate",
    "sort",
    "nextday",
    "status",
]


def run(seed=60):
    office = PostOffice(seed=seed)
    print("seed=%d" % office.seed)
    for line in SCRIPT:
        print("> %s -> %s" % (line, office.cmd(line)))
    print("final: %s" % office.status())
    return office


if __name__ == "__main__":
    run()
