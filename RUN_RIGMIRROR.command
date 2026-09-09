#!/bin/bash
cd "$(dirname "$0")" || exit 1
python3 rigmirror.py
status=$?
if [ "$status" -ne 0 ]; then
    echo
    echo "RigMirror stopped with an error."
    read -r -p "Press Return to close this window..."
fi
exit "$status"
