#!/bin/bash
# Retry wrapper for launchd jobs. Right after a reboot, iCloud (bird/cloudd) can hold a
# transient lock on files under ~/Documents while re-syncing, which makes Python's own
# file read fail with OSError: [Errno 11] Resource deadlock avoided before the app's
# retry-next-run logic ever gets a chance to run. Retry the whole invocation a few times
# with a short delay so a job doesn't sit dead until the next scheduled slot.
set -uo pipefail

max_attempts=5
delay=20

for ((i = 1; i <= max_attempts; i++)); do
    if "$@"; then
        exit 0
    fi
    echo "Attempt $i/$max_attempts failed, retrying in ${delay}s..." >&2
    sleep "$delay"
done

echo "All $max_attempts attempts failed." >&2
exit 1
