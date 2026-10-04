#!/usr/bin/env bash
# Run the game inside a private, input-less virtual KWin (own Xwayland + own D-Bus), so it never
# shows a window on the real desktop or touches the user's mouse/keyboard. Same args as run.sh.
# Env vars (PS2X_SHOT_DIR etc.) pass through. The game runs at nice 10.
root=$(cd "$(dirname "$0")" && pwd)
inner=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/ufe3-headless-XXXXXX.sh")
done_marker="$inner.done"
cat > "$inner" <<INNER
#!/usr/bin/env bash
unset WAYLAND_DISPLAY # runner is X11-only; use kwin's private Xwayland
nice -n 10 "$root/run.sh" $*
echo \$? > "$done_marker"
INNER
chmod +x "$inner"
unset DISPLAY WAYLAND_DISPLAY XAUTHORITY
setsid dbus-run-session -- kwin_wayland --virtual --no-lockscreen --socket "ufe3-headless-$$" \
    --xwayland --width 1280 --height 720 --exit-with-session "$inner" \
    2> >(grep -vE '^(kwin_|kf\.|qt\.|org\.kde|QDBus|libinput|xkbcommon)' >&2) &
pgid=$!
while kill -0 "$pgid" 2>/dev/null && [ ! -f "$done_marker" ]; do sleep 1; done
status=$(cat "$done_marker" 2>/dev/null || echo 1)
kill -TERM -- "-$pgid" 2>/dev/null
sleep 1
kill -KILL -- "-$pgid" 2>/dev/null
rm -f "$inner" "$done_marker"
exit "$status"
