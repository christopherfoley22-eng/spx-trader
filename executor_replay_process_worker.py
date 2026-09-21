"""Fresh-process CLI for durable, synthetic Executor replay tests only."""

import argparse
import json
import os
import sys

from executor_replay import ReplaySession
from executor_replay_process import DurableReplayRunner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session")
    parser.add_argument("database")
    parser.add_argument("--stop")
    parser.add_argument("--start-index", type=int)
    args = parser.parse_args()
    session = ReplaySession.from_json(args.session)

    def checkpoint(label):
        if label == args.stop:
            os._exit(73)  # Deliberate process death, without Python cleanup.

    runner = DurableReplayRunner(session, args.database, checkpoint)
    try:
        print(json.dumps(runner.run(args.start_index), sort_keys=True))
    finally:
        runner.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Errors are intentionally terse; runtime account identifiers stay out of logs.
        print("BLOCKED: " + type(exc).__name__, file=sys.stderr)
        sys.exit(3)
