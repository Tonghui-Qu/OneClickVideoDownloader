#!/bin/bash
#
# Native messaging launcher. Chrome execs this instead of host.py directly,
# because the /usr/bin/python3 shebang (an Xcode CLT shim) can fail to start
# under Chrome's restricted launch context. /bin/bash is always present, and
# we exec a real Python interpreter with an absolute path.

WLOG="/tmp/oneclick-wrapper.log"
echo "$(date '+%H:%M:%S') WRAPPER started argv=$*" >> "$WLOG" 2>/dev/null

DIR="$(cd "$(dirname "$0")" && pwd)"

for py in \
    /opt/homebrew/bin/python3 \
    /opt/homebrew/bin/python3.11 \
    /opt/homebrew/bin/python3.14 \
    /usr/local/bin/python3 \
    /usr/bin/python3 ; do
    if [ -x "$py" ]; then
        echo "$(date '+%H:%M:%S') WRAPPER using $py" >> "$WLOG" 2>/dev/null
        exec "$py" "$DIR/host.py" "$@"
    fi
done

echo "$(date '+%H:%M:%S') WRAPPER: no python3 found" >> "$WLOG" 2>/dev/null
exit 1
