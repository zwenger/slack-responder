#!/bin/sh
# Compatibility shim: timers installed before the Python runner still call run.sh. Run
# `python3 responder.py install` once to update the timer; the job itself is `python3 responder.py run`.
exec python3 "$(dirname "$0")/responder.py" run "$@"
