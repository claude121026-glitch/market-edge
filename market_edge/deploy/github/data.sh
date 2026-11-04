#!/usr/bin/env bash
# Keeps Market Edge's data in a GitHub release (tag "data") so every GitHub Actions run sees the latest copy.
#   data.sh pull [--allow-missing]   download + unpack data.tar.gz into market_edge/data
#   data.sh push                     pack market_edge/data (without the raw cache) and upload it
#   data.sh push-file FILE           upload one extra file (e.g. a live session's intraday bars)
#   data.sh pull-intraday DAY        download that day's intraday-DAY-*.db files into /tmp/intraday
#   data.sh prune DAYS               delete intraday-*.db assets older than DAYS days
# Needs GH_TOKEN (GitHub Actions: ${{ github.token }}) and runs from the repository root.
set -euo pipefail
TAG="data"
retry() { local n=0; until "$@"; do n=$((n+1)); [[ $n -ge 4 ]] && return 1; sleep $((n*10)); done; }

ensure_release() {
  gh release view "$TAG" >/dev/null 2>&1 || \
    gh release create "$TAG" --title "Market Edge data" --notes "Database and reports, updated by GitHub Actions. Don't delete." >/dev/null
}

case "${1:-}" in
  pull)
    mkdir -p market_edge
    if retry gh release download "$TAG" -p data.tar.gz -D /tmp --clobber; then
      tar -xzf /tmp/data.tar.gz -C market_edge
      echo "data: pulled $(du -h /tmp/data.tar.gz | cut -f1)"
    elif [[ "${2:-}" == "--allow-missing" ]]; then
      echo "data: no data yet (first run)"; mkdir -p market_edge/data
    else
      echo "::error::No data release yet. Run the 'market-edge jobs' workflow manually with command 'bootstrap' first."; exit 1
    fi ;;
  push)
    ensure_release
    tar -czf /tmp/data.tar.gz -C market_edge --exclude=data/raw data
    retry gh release upload "$TAG" /tmp/data.tar.gz --clobber
    echo "data: pushed $(du -h /tmp/data.tar.gz | cut -f1)" ;;
  push-file)
    ensure_release
    retry gh release upload "$TAG" "$2" --clobber ;;
  pull-intraday)
    mkdir -p /tmp/intraday
    gh release download "$TAG" -p "intraday-$2-*.db" -D /tmp/intraday --clobber 2>/dev/null || echo "data: no intraday files for $2"
    ls /tmp/intraday 2>/dev/null || true ;;
  prune)
    cutoff=$(date -d "-${2:-14} days" +%Y%m%d)
    gh release view "$TAG" --json assets -q '.assets[].name' | grep -E '^intraday-[0-9]{8}-' | while read -r a; do
      d=${a:9:8}; [[ "$d" < "$cutoff" ]] && gh release delete-asset "$TAG" "$a" -y && echo "pruned $a"
    done || true ;;
  *) echo "usage: data.sh pull|push|push-file FILE|pull-intraday YYYYMMDD|prune DAYS"; exit 2 ;;
esac
