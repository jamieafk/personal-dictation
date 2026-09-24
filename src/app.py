"""Menubar dictation app. Entry point: python -m src.app"""

import fcntl
import functools
import logging
import logging.handlers
import os
import sys
import threading
import time
import rumps

from src import config

# DICTATION_DEBUG=1 makes `python -m src` usable from a terminal: it skips the
# single-instance lock (so a debug copy can run alongside the installed launchd
# one) and mirrors logs to the console.
_DEBUG = bool(os.environ.get("DICTATION_DEBUG"))

# Set up logging
_log_dir = config.CONFIG_DIR
os.makedirs(_log_dir, exist_ok=True)
_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_handler = logging.handlers.RotatingFileHandler(
    os.path.join(_log_dir, "dictation.log"),
    maxBytes=1_000_000, backupCount=3,
    encoding="utf-8",  # launchd has no UTF-8 locale; without this, non-ASCII log
                       # lines (em-dashes etc.) raise on emit and are silently dropped
)
_handler.setFormatter(_formatter)
log = logging.getLogger("dictation")
log.addHandler(_handler)
log.setLevel(logging.INFO)

# Mirror logs to the console when run from a terminal (or in debug mode), so the
# documented debug command isn't a blank screen. The launchd process has no TTY,
# so it never attaches this handler and never double-logs.
if _DEBUG or sys.stderr.isatty():
    _console = logging.StreamHandler()
    _console.setFormatter(_formatter)
    log.addHandler(_console)

# Single-instance guard — only one copy can run at a time. Skipped in debug mode
# so a terminal session can run alongside the installed launchd copy.
if not _DEBUG:
    _lock_path = os.path.join(_log_dir, "dictation.lock")
    _lock_file = open(_lock_path, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Personal Dictation is already running.")
        sys.exit(0)

from AppKit import NSWorkspace
from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
from Foundation import NSURL
from src import audio, hotkey, paste, transcribe, sounds, history, postprocess
from src.overlay import RecordingOverlay
from src.segmenter import StreamingSegmenter

# State machine
WARMING_UP = "warming_up"
IDLE = "idle"
RECORDING = "recording"
PROCESSING = "processing"

# Menubar titles for each state
TITLES = {
    WARMING_UP: "\u2699",    # Gear — loading
    IDLE: "\U0001F399",       # Studio microphone — ready
    RECORDING: "\U0001F534",  # Red circle — recording
    PROCESSING: "\u2026",    # Ellipsis — processing
}


# Shown in place of the "ready" mic when the hotkey can't run — so the menubar
# never claims everything's fine while hold-to-talk is silently dead.
WARNING = "⚠️"  # warning sign

# Stable menu keys (used for placement / removal).
LAST_TEXT_PLACEHOLDER = "No transcriptions yet"
PERM_ITEM_TITLE = "⚠ Hotkey disabled — grant Accessibility"

# System Settings deep link for the Accessibility pane.
_ACCESSIBILITY_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)


def _logged(method):
    """Wrap a rumps menu/timer callback so exceptions are logged instead of being
    silently swallowed (rumps catches and discards callback exceptions)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            log.exception("Callback %s failed", getattr(method, "__name__", "?"))
    return wrapper


class DictationApp(rumps.App):
    def __init__(self):
        super().__init__(TITLES[WARMING_UP], quit_button=None)
        self._state = WARMING_UP
        self._state_lock = threading.Lock()
        self._model_ready = threading.Event()
        self._focused_app = ""
        self._overlay = RecordingOverlay()
        # Streaming: transcribes speech segments during the hold so on release
        # only the final tail remains. Each segment self-normalizes + runs its own
        # VAD guard via transcribe.transcribe.
        self._segmenter = StreamingSegmenter(audio, transcribe.transcribe)

        # Recovery state
        self._last_text = ""        # full text of the last successful dictation
        self._failed_buf = None     # raw audio of the last failed dictation (for retry)
        self._failed_app = ""

        # Hotkey / permission state
        self._hotkey_ok = True
        self._perm_item = None      # menu item shown only when the hotkey is disabled
        self._recheck_timer = None  # polls for Accessibility grant to self-heal
        self._tap_watch_timer = None  # warns if the tap never sees a key event
        self._tap_silent_warned = False
        self._tap_watch_ticks = 0

        # Menu items
        self._last_text_item = rumps.MenuItem(LAST_TEXT_PLACEHOLDER)
        self._last_text_item.set_callback(None)
        self._word_count_item = rumps.MenuItem(self._word_count_label())
        self._word_count_item.set_callback(None)
        self._retry_item = rumps.MenuItem("Retry last dictation")
        self._retry_item.set_callback(None)  # greyed until a dictation fails
        self.menu = [
            self._last_text_item,
            self._word_count_item,
            self._retry_item,
            None,
            rumps.MenuItem("Transcription History...", callback=self._show_history),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        # Start model warmup in background
        threading.Thread(target=self._warmup, daemon=True).start()

    def _warmup(self):
        while True:
            try:
                log.info("Model warmup starting")
                transcribe.warmup()
                self._model_ready.set()
                log.info("Model warmup complete — ready")
                audio.prepare()  # Pre-create stream for instant first recording
                # Pre-build overlay panel so first dictation doesn't pay construction cost
                from PyObjCTools import AppHelper
                AppHelper.callAfter(self._overlay._build_panel)
                self._set_state(IDLE)
                return
            except Exception as e:
                log.exception("Model warmup failed")
                rumps.notification(
                    "Personal Dictation",
                    "Model failed to load",
                    f"{e} — retrying in 30 seconds",
                )
                time.sleep(30)

    def _set_state(self, new_state: str):
        with self._state_lock:
            self._state = new_state
        # Setting self.title mutates the NSStatusItem (AppKit), and _set_state is
        # called from background threads (_process finally, _warmup), so dispatch to
        # the main thread. callAfter just enqueues when already on main, so the
        # main-thread callers stay correct too.
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._apply_title, new_state)

    def _apply_title(self, state: str):
        """Main thread: apply the menubar glyph for a state."""
        self.title = self._title_for(state)

    def _title_for(self, state: str) -> str:
        # When the hotkey can't run, never show the "ready" mic — surface the warning
        # so the menubar doesn't claim everything's fine while hold-to-talk is dead.
        if state == IDLE and not self._hotkey_ok:
            return WARNING
        return TITLES[state]

    @staticmethod
    def _word_count_label():
        count = history.lifetime_word_count()
        return f"{count:,} words transcribed"

    @_logged
    def _show_history(self, _):
        from src.history_window import HistoryWindowController
        HistoryWindowController.show()

    # --- Last-transcription re-paste + failure recovery ---

    @_logged
    def _repaste_last(self, _):
        """Re-paste the last successful dictation. Recovery path for when the
        original blind Cmd+V landed nowhere (non-text field, focus moved, Electron
        ate the keystroke)."""
        text = self._last_text
        if not text:
            return
        # Don't re-paste while a dictation is mid-flight — avoids contending with
        # _process for the clipboard and the focused target.
        with self._state_lock:
            if self._state != IDLE:
                sounds.play_busy()
                return

        def _do():
            time.sleep(config.REPASTE_FOCUS_DELAY_S)  # let focus return after the menu closes
            # No pre-save: insert_text saves+restores the clipboard atomically under
            # its lock, so concurrent re-paste clicks can't clobber the saved value.
            paste.insert_text(text)

        threading.Thread(target=_do, daemon=True).start()

    def _refresh_menu_after_dictation(self, display: str):
        """Main thread: update menubar items after a successful dictation."""
        self._last_text_item.title = display
        self._last_text_item.set_callback(self._repaste_last)  # now clickable to re-paste
        self._word_count_item.title = self._word_count_label()

    def _enable_retry(self):
        """Main thread: make the 'Retry last dictation' item clickable."""
        self._retry_item.set_callback(self._retry_last)

    def _disable_retry(self):
        """Main thread: grey out the 'Retry last dictation' item."""
        self._retry_item.set_callback(None)

    @_logged
    def _retry_last(self, _):
        """Re-run the pipeline on the last failed dictation's retained audio, so a
        transient transcription error doesn't force the user to re-speak."""
        buf = self._failed_buf
        app_name = self._failed_app
        if buf is None:
            return
        with self._state_lock:
            if self._state != IDLE:
                sounds.play_busy()  # busy now — keep the buffer so they can retry later
                return
            self._state = PROCESSING
        self._failed_buf = None  # consume so a double-click can't double-run
        self._disable_retry()
        self.title = self._title_for(PROCESSING)
        # Give the retry the same near-cursor "working" cue as the normal release path.
        self._overlay.show_processing()
        # Save the clipboard fresh — the value captured at failure time is stale now.
        saved = paste.save_clipboard()
        threading.Thread(
            target=self._process,
            args=(buf, app_name, saved, 0.0, True),
            daemon=True,
        ).start()

    def _on_press(self):
        with self._state_lock:
            current = self._state
            if current == IDLE:
                self._state = RECORDING
        if current != IDLE:
            # Can't start a recording right now — model still warming up, or the
            # previous dictation is still processing. Give an audible "not ready"
            # cue so the user waits instead of speaking a sentence into the void.
            if current in (WARMING_UP, PROCESSING):
                sounds.play_busy()
            return
        self.title = TITLES[RECORDING]
        # Start recording FIRST for lowest latency
        if not audio.start_recording():
            log.error("Microphone unavailable")
            rumps.notification("Personal Dictation", "Microphone Error",
                               "Could not access the microphone. Check your audio device.")
            self._set_state(IDLE)
            return
        self._segmenter.start()  # begin transcribing speech segments during the hold
        # Capture focused app + feedback after recording is already running
        ws = NSWorkspace.sharedWorkspace()
        active = ws.frontmostApplication()
        self._focused_app = active.localizedName() if active else "Unknown"
        sounds.play_start()
        self._overlay.show()

    def _on_release(self):
        with self._state_lock:
            if self._state != RECORDING:
                return
            self._state = PROCESSING
        self.title = TITLES[PROCESSING]
        sounds.play_stop()

        # Min-duration discard runs on the main thread so a clipped quick-tap flashes
        # immediately (no processing pulse), matching the pre-streaming behavior.
        min_samples = int(max(1, audio.native_rate()) * config.MIN_DURATION_S)
        if audio.samples_captured() < min_samples:
            self._segmenter.cancel()
            audio.stop_stream()
            self._overlay.flash_discard()
            self._set_state(IDLE)
            return

        # Keep the overlay as a "processing" pulse until the paste lands. Most of the
        # audio was already transcribed during the hold, so this window is brief.
        self._overlay.show_processing()
        saved_clipboard = paste.save_clipboard()

        # Joining the poll thread, finalizing the tail, and pasting all run OFF the
        # main run loop so the CGEvent tap callback never blocks.
        threading.Thread(
            target=self._finalize_streaming,
            args=(self._focused_app, saved_clipboard),
            daemon=True,
        ).start()

    def _on_cancel(self):
        with self._state_lock:
            if self._state != RECORDING:
                return
            self._state = IDLE
        self.title = self._title_for(IDLE)
        self._overlay.hide()
        self._segmenter.cancel()  # discard pending segments + stop the worker
        audio.stop_stream()       # stop the stream, discard the buffer

    def _finalize_streaming(self, app_name: str, saved_clipboard=None):
        """Background: stop segmenting, transcribe the final tail, assemble the full
        text, post-process, and paste. Runs off the main thread. Most segments are
        already transcribed by release, so this mostly waits on the short tail."""
        from PyObjCTools import AppHelper
        try:
            self._segmenter.stop_polling()          # no more auto-closes (joins poll thread)
            end_native = audio.samples_captured()   # everything captured up to release
            self._segmenter.close_tail(end_native)  # enqueue the final tail segment
            audio.stop_stream()                     # tail already extracted; safe to stop+clear

            t0 = time.monotonic()
            text = self._segmenter.finalize()       # waits for all queued segments to transcribe
            elapsed_ms = (time.monotonic() - t0) * 1000
            full = self._segmenter.full_audio()     # reconstructed audio, retained for retry

            vocab = postprocess.load_vocab()
            text = postprocess.clean(text, vocab)
            if text:
                paste.insert_text(text, saved_clipboard=saved_clipboard)
                history.append(app_name, text)
                word_count = len(text.split())
                log.info("Dictation: %d words in %.0fms from %s (streaming)",
                         word_count, elapsed_ms, app_name)
                self._last_text = text
                self._failed_buf = None
                self._failed_app = ""
                display = text if len(text) <= 60 else text[:57] + "..."
                AppHelper.callAfter(self._refresh_menu_after_dictation, display)
                AppHelper.callAfter(self._disable_retry)
                from src.history_window import HistoryWindowController
                AppHelper.callAfter(HistoryWindowController.refresh_if_visible)
                AppHelper.callAfter(self._overlay.hide)
            else:
                log.info("Dictation discarded (no content) in %.0fms from %s (streaming)",
                         elapsed_ms, app_name)
                # Retain the reconstructed audio so a transient miss can be retried.
                if len(full) > 0:
                    self._failed_buf = full
                    self._failed_app = app_name
                    AppHelper.callAfter(self._enable_retry)
                AppHelper.callAfter(self._overlay.flash_discard)
        except Exception as e:
            log.exception("Streaming transcription error")
            full = self._segmenter.full_audio()
            if len(full) > 0:
                self._failed_buf = full
                self._failed_app = app_name
                AppHelper.callAfter(self._enable_retry)
            AppHelper.callAfter(self._overlay.hide)
            rumps.notification(
                "Dictation Error",
                "Transcription failed",
                f"{str(e)[:150]} — use 'Retry last dictation' in the menu",
            )
        finally:
            self._set_state(IDLE)
            audio.prepare()           # no-op if stream exists; recreates if it died
            self._segmenter.reset()

    def _process(self, buf, app_name: str, saved_clipboard=None, peak: float = 0.0,
                 was_retry: bool = False):
        from PyObjCTools import AppHelper
        try:
            t0 = time.monotonic()
            text = transcribe.transcribe(buf, precomputed_peak=peak)
            elapsed_ms = (time.monotonic() - t0) * 1000
            # Post-process: vocab correction, filler removal, stutter collapse
            vocab = postprocess.load_vocab()
            text = postprocess.clean(text, vocab)
            if text:
                paste.insert_text(text, saved_clipboard=saved_clipboard)
                history.append(app_name, text)
                word_count = len(text.split())
                log.info("Dictation: %d words in %.0fms from %s", word_count, elapsed_ms, app_name)
                # Remember the full text so the menubar item can re-paste it (recovery
                # if the blind Cmd+V landed nowhere); clear any stale failed buffer.
                self._last_text = text
                self._failed_buf = None
                self._failed_app = ""
                display = text if len(text) <= 60 else text[:57] + "..."
                # All UI updates must run on the main thread.
                AppHelper.callAfter(self._refresh_menu_after_dictation, display)
                AppHelper.callAfter(self._disable_retry)
                from src.history_window import HistoryWindowController
                AppHelper.callAfter(HistoryWindowController.refresh_if_visible)
                AppHelper.callAfter(self._overlay.hide)
            else:
                log.info("Dictation discarded (no content) in %.0fms from %s", elapsed_ms, app_name)
                # A retry that comes back empty must not silently destroy the recovery
                # buffer — keep it so the user can try again (mirrors the except branch).
                if was_retry:
                    self._failed_buf = buf
                    self._failed_app = app_name
                    AppHelper.callAfter(self._enable_retry)
                # Must dispatch to main thread — UI operations can't run on background threads
                AppHelper.callAfter(self._overlay.flash_discard)
        except Exception as e:
            log.exception("Transcription error")
            # Retain the raw audio so the user can retry instead of re-speaking.
            self._failed_buf = buf
            self._failed_app = app_name
            AppHelper.callAfter(self._enable_retry)
            AppHelper.callAfter(self._overlay.hide)
            rumps.notification(
                "Dictation Error",
                "Transcription failed",
                f"{str(e)[:150]} — use 'Retry last dictation' in the menu",
            )
        finally:
            self._set_state(IDLE)
            audio.prepare()  # No-op if stream exists; recreates if it died

    def _on_tap_lost(self):
        log.error("Hotkey tap lost — too many timeouts")
        rumps.notification(
            "Personal Dictation",
            "Hotkey stopped working",
            "Check Accessibility permissions and restart the app.",
        )

    # --- Hotkey / Accessibility status ---

    def _show_hotkey_disabled(self):
        """Main thread: reflect a dead hotkey in the menubar and start polling so a
        later Accessibility grant self-heals the app without a restart."""
        self.title = WARNING
        if self._perm_item is None:
            self._perm_item = rumps.MenuItem(
                PERM_ITEM_TITLE, callback=self._open_accessibility_settings)
            self.menu.insert_before(LAST_TEXT_PLACEHOLDER, self._perm_item)
        if self._recheck_timer is None:
            self._recheck_timer = rumps.Timer(self._recheck_hotkey, config.HOTKEY_RECHECK_S)
            self._recheck_timer.start()

    @_logged
    def _open_accessibility_settings(self, _):
        url = NSURL.URLWithString_(_ACCESSIBILITY_SETTINGS_URL)
        NSWorkspace.sharedWorkspace().openURL_(url)

    @_logged
    def _recheck_hotkey(self, _):
        """Periodic: once Accessibility is granted, enable the hotkey and restore the
        ready icon + menu without requiring a restart."""
        if self._hotkey_ok:
            return
        if hotkey.setup_hotkey(self._on_press, self._on_release, self._on_cancel, self._on_tap_lost):
            self._hotkey_ok = True
            log.info("Accessibility granted — hotkey enabled")
            self._start_tap_watch()
            self.title = self._title_for(self._state)
            if self._perm_item is not None:
                try:
                    del self.menu[PERM_ITEM_TITLE]
                except KeyError:
                    pass
                self._perm_item = None
            if self._recheck_timer is not None:
                self._recheck_timer.stop()
                self._recheck_timer = None

    def _start_tap_watch(self):
        """Main thread: a tap can be created yet receive nothing when the Input
        Monitoring grant is stale, so watch for the first key event."""
        if self._tap_watch_timer is None:
            self._tap_watch_ticks = 0
            self._tap_watch_timer = rumps.Timer(self._check_tap_alive, config.HOTKEY_SILENT_WARN_S)
            self._tap_watch_timer.start()

    @_logged
    def _check_tap_alive(self, _):
        # rumps.Timer fires once immediately on start; ignore that tick.
        self._tap_watch_ticks += 1
        if self._tap_watch_timer is None or self._tap_watch_ticks == 1:
            return
        if hotkey.events_seen() > 0:
            if self._tap_silent_warned:
                log.info("Hotkey tap now receiving key events")
            self._tap_watch_timer.stop()
            self._tap_watch_timer = None
        elif not self._tap_silent_warned:
            self._tap_silent_warned = True
            log.warning(
                "Hotkey tap has received no key events for %.0fs — Input Monitoring grant is "
                "likely stale (e.g. after a rebuild). Fix: tccutil reset ListenEvent/PostEvent/"
                "Accessibility com.personal.dictation, relaunch, re-grant.",
                config.HOTKEY_SILENT_WARN_S)

    @_logged
    def _quit(self, _):
        audio.shutdown()
        rumps.quit_application()

    def run(self, **kwargs):
        log.info("Personal Dictation starting")

        # Check microphone permission
        mic_status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        if mic_status == 2:  # Denied
            log.warning("Microphone permission denied")
            rumps.notification(
                "Personal Dictation",
                "Microphone Permission Required",
                "Go to System Settings > Privacy & Security > Microphone and enable it.",
            )
        elif mic_status == 0:  # Not yet determined — trigger the prompt
            AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AVMediaTypeAudio, lambda granted: None
            )

        # Set up the global hotkey — if it fails, still run (show menubar) but make
        # the broken state visible and recoverable instead of silently showing "ready".
        if hotkey.setup_hotkey(self._on_press, self._on_release, self._on_cancel, self._on_tap_lost):
            self._hotkey_ok = True
            self._start_tap_watch()
        else:
            self._hotkey_ok = False
            log.warning("Accessibility permission not granted — hotkey disabled")
            rumps.notification(
                "Personal Dictation",
                "Accessibility Permission Required",
                "Open System Settings > Privacy & Security > Accessibility and enable "
                "Personal Dictation. It will start working automatically — no restart needed.",
            )
            self._show_hotkey_disabled()
        super().run(**kwargs)


if __name__ == "__main__":
    DictationApp().run()
