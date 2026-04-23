# Auto-render Stage 2: FL render settings dialog

**Status:** Currently relies on FL remembering the user's prior Mode + format
setup. This doc sketches the full Stage 2 automation so cold-start (fresh
install, FL forgot, different mode needed) works without a human.

Can't be built from a Linux dev box — FL's render dialog is a Delphi/VCL
control set that needs to be inspected on an actual running Windows FL
instance. When you next work from Windows, use the scaffolding below to
inspect the actual hwnd/class tree and fill in the specifics.

## What currently happens (working path)

1. Save As dialog confirmed with Alt+S → filename committed.
2. FL opens its own render-settings dialog (VCL, modal, sometimes doesn't
   take foreground).
3. `workspace._try_auto_render` waits 1.5s then sends Enter twice.
4. If FL remembers the Mode from the user's last manual export, Enter clicks
   the Start button (it's the default). Export runs.

Fails when FL doesn't remember Mode — Enter hits the wrong control.

## What we need to automate

Three fields on FL's render settings dialog:

| Field | Current behavior | Needed |
|-------|------------------|--------|
| Mode dropdown | Defaults to last-used ("Tracks (separate audio files)" after one manual batch export) | Explicitly set based on `batch: bool` — "Tracks (separate audio files)" if batch, "Full song" if single master. |
| Format | WAV 16-bit / 24-bit / etc. | Lock to WAV 24-bit (analysis-friendly). |
| Start button | Default focus usually (but not guaranteed) | Explicit click, not Enter-is-default. |

## Scaffolding to inspect the dialog

```python
# Add this after the Save As closes in _try_auto_render, guarded by a
# STUDIOMIND_DIALOG_DEBUG=1 env var. Run once on Windows, capture output,
# then replace with targeted logic below.

def _enumerate_all_top_level() -> list[dict]:
    """Every visible top-level window currently on screen."""
    import ctypes
    user32 = ctypes.windll.user32
    results: list[dict] = []
    Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(128)
        text = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 128)
        user32.GetWindowTextW(hwnd, text, 256)
        # FL's render settings dialog is a VCL TForm descendant, class name
        # usually starts with "T" and contains "Form" or is a specific tag.
        if cls.value.startswith("T") or "Render" in text.value:
            results.append({"hwnd": hwnd, "cls": cls.value, "text": text.value})
        return True

    user32.EnumWindows(Proc(_cb), 0)
    return results

# On Windows: run a batch export, let it get past Save As, capture the
# top-level list. The render-settings dialog's class name will be something
# like "TForm1", "TRenderingForm", or a similarly distinctive VCL name.
```

## Likely control structure (guessed — verify on Windows)

FL's render dialog is typically:

```
TRenderingForm (hwnd_root)
├── TComboBox "cboMode"       — Mode dropdown
├── TComboBox "cboFormat"     — WAV/MP3/FLAC
├── TButton   "btnStart"      — Start render
├── TCheckBox                 — "Save slices to audio files"
├── TCheckBox                 — "Split mixer tracks" (on in batch mode)
└── TCheckBox                 — various quality/dither options
```

Delphi VCL exposes these through `GetClassName` as `TComboBox`, `TButton`,
etc. The text on buttons comes from `GetWindowTextW`.

## Approach to set Mode

```python
def _configure_fl_render_settings(
    dialog_hwnd: int,
    batch: bool,
    stop_event: threading.Event | None = None,
) -> bool:
    """
    Stage 2: set Mode, Format, click Start.

    Dialog structure (VCL):
      - TComboBox with Mode items (index 0 = Full song, 1 = Pattern,
        2 = Tracks (separate files), 3 = Selected tracks, ...)
      - TButton with text "Start" or "&Start"
    """
    import ctypes
    user32 = ctypes.windll.user32

    CB_SETCURSEL = 0x014E  # ComboBox: select item by index

    combos = []
    buttons = []

    def _cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(128)
        text = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 128)
        user32.GetWindowTextW(hwnd, text, 256)
        if cls.value == "TComboBox":
            combos.append({"hwnd": hwnd, "text": text.value})
        elif cls.value == "TButton":
            buttons.append({"hwnd": hwnd, "text": text.value})
        return True

    Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
    user32.EnumChildWindows(dialog_hwnd, Proc(_cb), 0)

    # VERIFY ON WINDOWS: the Mode combo is the first TComboBox in tab order.
    # FL's combos don't carry meaningful text from GetWindowTextW; they
    # expose items only via CB_GETLBTEXT. Rely on index 0 being Mode.
    if combos:
        mode_index = 2 if batch else 0  # 2 = Tracks (separate), 0 = Full song
        user32.SendMessageW(combos[0]["hwnd"], CB_SETCURSEL, mode_index, 0)
        # CBN_SELCHANGE parent notification isn't sent by CB_SETCURSEL.
        # Send WM_COMMAND with HIWORD=CBN_SELCHANGE to trigger FL's handler.
        # Otherwise FL may ignore the change.
        # TODO verify on Windows whether this is needed.

    # Start button: text is "Start" or "&Start" depending on VCL locale.
    for btn in buttons:
        t = btn["text"].lower().replace("&", "")
        if t == "start":
            BM_CLICK = 0x00F5
            user32.SendMessageW(btn["hwnd"], BM_CLICK, 0, 0)
            return True

    return False
```

## Integration

Replace the current blind-Enter at the end of `_try_auto_render`:

```python
# ── STAGE 2 (blind Enter — current) ──
send_keys("{ENTER}")
time.sleep(0.3)
send_keys("{ENTER}")

# ── STAGE 2 (targeted — proposed) ──
settings_hwnd = _wait_for_fl_render_settings(timeout_s=4.0)
if settings_hwnd is None:
    # FL skipped the settings dialog (previous settings cached and valid).
    # The export is probably already running. No-op.
    return True, "Export triggered"

if not _configure_fl_render_settings(settings_hwnd, batch=batch, stop_event=stop_event):
    # Fall back to the blind Enter if we couldn't find the controls.
    send_keys("{ENTER}")
    send_keys("{ENTER}")
```

Add a `_wait_for_fl_render_settings` helper modeled on `_wait_for_save_dialog`
but looking for a top-level window whose class is a `T*` Delphi form and
that has a `TButton` child with text "Start" or "&Start".

## Unknowns to resolve on Windows

1. **Exact class name** of the render settings dialog. Probably `TForm1` or
   similar VCL default. Confirm via `GetClassNameW`.
2. **Mode combo index mapping**. Index-based rather than text-based because
   VCL combos don't surface item text through `GetWindowTextW`. Confirm the
   indexes by running CB_GETLBTEXT for each index on a live dialog.
3. **CBN_SELCHANGE requirement**. Some Delphi forms re-read combo state on
   button-click even without the notification. Others require the
   parent-notified message. Test by setting Mode via CB_SETCURSEL and then
   clicking Start — does FL honor the new Mode or fall back to the old one?
4. **Whether the dialog always appears**. If FL has valid cached settings,
   it may skip the dialog entirely and start exporting immediately. The
   `_wait_for_fl_render_settings` helper must tolerate a timeout cleanly.

## Don't start this until

- Vertical-slice test passes (docs/vertical-slice-test.md).
- User is actually seeing Stage-2 failures in real sessions. Today it works
  because FL remembers the setup. If we never see it fail in practice, this
  whole dialog-walking exercise is polish with no payoff.
