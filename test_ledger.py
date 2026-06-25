#!/usr/bin/env python3
"""Tests for the ledger. Run: python3 -m pytest test_ledger.py -v
   Or plain:                  python3 test_ledger.py
No pytest? It falls back to a tiny runner.
"""
import os
import sys
import time
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "ledger.py")


def run(args, owner=None, cwd=None, ttl=None):
    env = os.environ.copy()
    if owner:
        env["LEDGER_OWNER"] = owner
    cmd = ["python3", LEDGER]
    if ttl is not None:
        cmd += ["--ttl", str(ttl)]
    cmd += args
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def fresh():
    d = tempfile.mkdtemp(prefix="ledger-test-")
    return d


def test_claim_returns_id():
    d = fresh()
    code, out, _ = run(["claim", "task-a"], cwd=d)
    assert code == 0, "first claim should succeed"
    assert len(out) == 12, f"expected a 12-char id, got {out!r}"


def test_double_claim_fails():
    d = fresh()
    run(["claim", "task-a"], owner="a", cwd=d)
    code, _, err = run(["claim", "task-a"], owner="b", cwd=d)
    assert code == 1, "second claim on same task must fail"
    assert "held by a" in err, f"error should name the holder, got {err!r}"


def test_different_tasks_coexist():
    d = fresh()
    c1, _, _ = run(["claim", "task-a"], cwd=d)
    c2, _, _ = run(["claim", "task-b"], cwd=d)
    assert c1 == 0 and c2 == 0, "distinct tasks should both claim fine"


def test_done_frees_task():
    d = fresh()
    _, cid, _ = run(["claim", "task-a"], cwd=d)
    code, _, _ = run(["done", cid], cwd=d)
    assert code == 0
    code2, _, _ = run(["claim", "task-a"], cwd=d)
    assert code2 == 0, "task should be claimable again after done"


def test_done_by_task_name():
    d = fresh()
    run(["claim", "task-a"], cwd=d)
    code, _, _ = run(["done", "task-a"], cwd=d)
    assert code == 0, "done should accept the task string too"


def test_expired_claim_is_reclaimable():
    d = fresh()
    run(["claim", "task-a"], owner="dead", cwd=d)
    time.sleep(2)
    # ttl=1 means the 2s-old claim is stale and gets taken over
    code, _, _ = run(["claim", "task-a"], owner="alive", cwd=d, ttl=1)
    assert code == 0, "a stale claim should be reclaimable"


def test_beat_keeps_claim_alive():
    d = fresh()
    _, cid, _ = run(["claim", "task-a"], owner="worker", cwd=d)
    time.sleep(2)
    run(["beat", cid], cwd=d)
    # even with ttl=1, the beat refreshed it, so a new claim must fail
    code, _, _ = run(["claim", "task-a"], owner="other", cwd=d, ttl=1)
    assert code == 1, "a freshly-beaten claim should not be reclaimable"


def test_race_single_winner():
    d = fresh()
    procs = []
    for i in range(25):
        env = os.environ.copy()
        env["LEDGER_OWNER"] = f"racer-{i}"
        procs.append(subprocess.Popen(
            ["python3", LEDGER, "claim", "hot"],
            cwd=d, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ))
    winners = sum(1 for p in procs if p.wait() == 0)
    assert winners == 1, f"exactly one racer should win, got {winners}"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERR  {t.__name__}: {e}")
    print(f"\n{len(tests)-failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
