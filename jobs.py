"""Cron entry point: `python jobs.py <tick|rollup|prune|report>`."""

import sys

import ledger

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ledger.JOBS:
        sys.exit(f"usage: jobs.py {{{'|'.join(ledger.JOBS)}}}")
    print(ledger.run(ledger.JOBS[sys.argv[1]]), flush=True)
