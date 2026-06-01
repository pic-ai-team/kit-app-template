#!/bin/bash
echo "run_kit_loop.sh: wrapper starting..."
# run_kit_loop.sh — auto-restart Kit on exit


# Use either repo.sh or the built kit binary. Example uses built binary + .kit
KIT_BIN="./_build/linux-aarch64/release/kit/kit"
KIT_FILE="./_build/linux-aarch64/release/apps/computex_streaming.kit"
KIT_CMD=( "$KIT_BIN" "$KIT_FILE" --no-window --/log/level=info)

while true; do
  echo "$(date): Starting Kit..."
  # Start Kit in background so we can catch signals and forward them
  "${KIT_CMD[@]}" &
  CHILD=$!

  # Forward SIGINT/SIGTERM to child so Ctrl-C kills Kit and the wrapper
  trap 'echo "run_kit_loop.sh: forwarding signal to child ($CHILD)"; kill -TERM "${CHILD}" 2>/dev/null || true; wait "${CHILD}"; exit 0' SIGINT SIGTERM

  # Wait for the child to exit
  wait "${CHILD}"
  EXIT_CODE=$?

  # Clear trap for this iteration
  trap - SIGINT SIGTERM
  echo "$(date): Kit exited with code $EXIT_CODE"

  # Optional: stop restarting on Ctrl+C/SIGINT (130) or SIGTERM (143)
  if [[ $EXIT_CODE -eq 130 ]] || [[ $EXIT_CODE -eq 143 ]]; then
    echo "Kit terminated by signal — not restarting"
    break
  fi

  echo "Restarting in 5 seconds..."
  sleep 5
done