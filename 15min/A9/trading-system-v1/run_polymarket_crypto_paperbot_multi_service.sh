#!/usr/bin/env bash
set -euo pipefail

cd /root/.openclaw/workspace
mkdir -p /root/.openclaw/workspace/data /root/.openclaw/workspace/logs

LIVE_BASE="/root/.openclaw/workspace/data/polymarket_crypto_paperbot_multi_live"
LIVE_LEDGER="${LIVE_BASE}_ledger.jsonl"
LIVE_STATUS="${LIVE_BASE}_status.json"
LIVE_SUMMARY="${LIVE_BASE}_summary.json"
LIVE_ROUNDS="${LIVE_BASE}_rounds.jsonl"
LIVE_FLIPS="${LIVE_BASE}_flips.jsonl"
LIVE_SIGNALS="${LIVE_BASE}_signals.jsonl"
LOG="/root/.openclaw/workspace/logs/polymarket_crypto_paperbot_multi_service.log"

bootstrap_live_ledger() {
  if [ -s "$LIVE_LEDGER" ]; then
    return 0
  fi
  local seed
  seed=$(ls -1t /root/.openclaw/workspace/data/polymarket_crypto_paperbot_multi_*_ledger.jsonl 2>/dev/null | grep -v 'polymarket_crypto_paperbot_multi_live_ledger.jsonl' | head -n 1 || true)
  if [ -n "$seed" ] && [ -f "$seed" ]; then
    python3 - "$seed" "$LIVE_LEDGER" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, 'r', encoding='utf-8') as f, open(dst, 'a', encoding='utf-8') as out:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get('type') in ('open', 'close'):
            out.write(json.dumps(obj, ensure_ascii=False) + '\n')
PY
    echo "[paperbot-service] bootstrapped live ledger from $seed" | tee -a "$LOG"
  fi
}

current_balance() {
  python3 - "$LIVE_STATUS" "$LIVE_LEDGER" <<'PY'
import json, os, sys
status_path, ledger_path = sys.argv[1], sys.argv[2]
def balance_from_status(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            obj = json.load(f)
        bal = (((obj or {}).get('stats') or {}).get('balance'))
        if isinstance(bal, (int, float)):
            return float(bal)
    except Exception:
        pass
    return None

def balance_from_ledger(path):
    if not os.path.exists(path):
        return None
    last = None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                bal = obj.get('balance')
                if isinstance(bal, (int, float)):
                    last = float(bal)
    except Exception:
        return None
    return last

bal = balance_from_status(status_path)
if bal is None:
    bal = balance_from_ledger(ledger_path)
if bal is None:
    bal = 10.0
print(f"{bal:.4f}")
PY
}

bootstrap_live_ledger

while true; do
  INIT_BALANCE=$(current_balance)
  echo "[paperbot-service] starting cycle balance=${INIT_BALANCE}" | tee -a "$LOG"

  if ! node /root/.openclaw/workspace/scripts/polymarket_crypto_paperbot_multi.js \
    --duration 1020 \
    --report-interval 60 \
    --initial-balance "$INIT_BALANCE" \
    --stake 1.2 \
    --min-stake 0.5 \
    --max-concurrent-trades 1 \
    --one-trade-per-round true \
    --min-poly-lag 0.015 \
    --min-round-move-bps 1.8 \
    --min-sources-agree 1 \
    --min-estimated-win-prob 0.54 \
    --max-flips-per-round 3 \
    --min-round-quality 4.2 \
    --min-seconds-since-last-flip 45 \
    --min-entry-window-minutes 2 \
    --max-source-spread-bps 12 \
    --signal-window-ms 1000 \
    --history-window-ms 900000 \
    --symbols BTC,ETH,SOL,XRP,DOGE,HYPE,BNB \
    --focus-window-minutes 15 \
    --output-json "$LIVE_SUMMARY" \
    --ledger-jsonl "$LIVE_LEDGER" \
    --status-json "$LIVE_STATUS" \
    --round-log-jsonl "$LIVE_ROUNDS" \
    --flip-log-jsonl "$LIVE_FLIPS" \
    --signal-log-jsonl "$LIVE_SIGNALS" \
    >> "$LOG" 2>&1; then
    echo "[paperbot-service] cycle failed balance=${INIT_BALANCE}" | tee -a "$LOG"
  else
    echo "[paperbot-service] cycle finished balance=${INIT_BALANCE}" | tee -a "$LOG"
  fi

  sleep 5
done
