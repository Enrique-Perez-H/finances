#!/bin/sh
# Fails if a file that commonly holds real financial data or secrets is staged.
bad=0
for f in "$@"; do
  case "$f" in
    *.db|*.sqlite|*.sqlite3|*.enc|*.csv|*.ofx|*.qif|*.xlsx|*.pem|*.key|.env|.env.*|data/*)
      echo "Refusing to commit possible private data: $f" >&2; bad=1 ;;
  esac
done
exit $bad
