import threading
import time
import unittest

from web.server import Inverter, Busy


class DummyReader:
    """Stand in for the real Reader so these tests never open a link."""
    deadline = None

    def close(self):
        pass


def fresh():
    inv = Inverter()
    inv.reader = DummyReader()      # pre-set so _run never constructs a real (connecting) Reader
    return inv


class TestLinkLocking(unittest.TestCase):
    def test_coalesces_duplicate_reads(self):
        """N identical in-flight reads (same key) run the link op ONCE; all callers get that one result."""
        inv = fresh()
        calls = []
        barrier = threading.Barrier(5)

        def slow(r):
            calls.append(1)
            time.sleep(0.3)
            return "value"

        results = []

        def do():
            barrier.wait()
            results.append(inv.use("read", slow, wait=5, check_versions=False, key="K"))

        ts = [threading.Thread(target=do) for _ in range(5)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(len(calls), 1, "duplicate reads must coalesce to one link op")
        self.assertEqual(results, ["value"] * 5, "every caller gets the leader's result")

    def test_different_keys_do_not_coalesce_and_serialize(self):
        """Different requests still run (serialized by the lock, never interleaved)."""
        inv = fresh()
        order = []
        guard = threading.Lock()

        def make(tag):
            def fn(r):
                with guard: order.append(("start", tag))
                time.sleep(0.05)
                with guard: order.append(("end", tag))
                return tag
            return fn

        ts = [threading.Thread(target=lambda t=t: inv.use("op", make(t), wait=5, check_versions=False, key=t))
              for t in ("a", "b", "c")]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(len(order), 6)
        for i in range(0, 6, 2):                     # each start is immediately followed by its own end
            self.assertEqual(order[i][0], "start")
            self.assertEqual(order[i + 1], ("end", order[i][1]))

    def test_fail_fast_when_busy(self):
        inv = fresh()
        started, release = threading.Event(), threading.Event()

        def holder(r):
            started.set(); release.wait(2); return "held"

        h = threading.Thread(target=lambda: inv.use("hold", holder, wait=True, check_versions=False))
        h.start(); started.wait(1)
        with self.assertRaises(Busy):                # wait=False while busy -> immediate Busy
            inv.use("other", lambda r: "x", wait=False, check_versions=False)
        release.set(); h.join()

    def test_wait_then_run(self):
        inv = fresh()
        started, release = threading.Event(), threading.Event()

        def holder(r):
            started.set(); release.wait(2); return "held"

        h = threading.Thread(target=lambda: inv.use("hold", holder, wait=True, check_versions=False))
        h.start(); started.wait(1)
        out = []
        w = threading.Thread(target=lambda: out.append(inv.use("waiter", lambda r: "ran", wait=5, check_versions=False)))
        w.start()
        time.sleep(0.2)
        self.assertEqual(out, [], "a waiter blocks while the link is held")
        release.set(); h.join(); w.join()
        self.assertEqual(out, ["ran"], "and runs once the link frees")

    def test_coalesced_error_is_shared(self):
        inv = fresh()
        barrier = threading.Barrier(4)

        def boom(r):
            time.sleep(0.2); raise ValueError("nope")

        errs = []

        def do():
            barrier.wait()
            try:
                inv.use("read", boom, wait=5, check_versions=False, key="K")
            except Exception as e:
                errs.append(type(e).__name__)

        ts = [threading.Thread(target=do) for _ in range(4)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(errs, ["ValueError"] * 4, "all coalesced callers share the leader's error")


if __name__ == "__main__":
    unittest.main()
