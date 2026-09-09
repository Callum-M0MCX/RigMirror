#!/bin/bash
cd "$(dirname "$0")" || exit 1
python3 -m pip install -r requirements.txt
status=$?
echo
if [ "$status" -eq 0 ]; then
    echo "RigMirror requirements are installed."
else
    echo "The requirements installation failed."
fi
read -r -p "Press Return to close this window..."
exit "$status"
