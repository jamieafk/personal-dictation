"""Floating waveform indicator during recording. Non-focus-stealing NSPanel."""

import math
import random
import objc
from AppKit import (
    NSPanel, NSView, NSColor, NSBezierPath, NSScreen, NSGraphicsContext,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSBackingStoreBuffered,
)
from Foundation import NSObject, NSTimer, NSMakeRect
import Quartz
from src import audio, config

# Panel dimensions — minimal, sleek
PANEL_WIDTH = 64
PANEL_HEIGHT = 24
NUM_BARS = 5
BAR_WIDTH = 3
BAR_GAP = 4
BAR_RADIUS = 1.5
CORNER_RADIUS = 8


class WaveformView(NSView):
    """Custom view that draws animated audio level bars."""

    def initWithFrame_(self, frame):
        self = objc.super(WaveformView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._levels = [0.0] * NUM_BARS
        self._discard_mode = False
        self._anim_mode = "recording"  # "recording" (RMS-driven) or "processing" (pulse)
        self.setWantsLayer_(True)
        return self

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        # Dark semi-transparent background pill
        bounds = self.bounds()
        bg_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, CORNER_RADIUS, CORNER_RADIUS
        )
        NSColor.colorWithWhite_alpha_(0.1, 0.85).setFill()
        bg_path.fill()

        # Draw bars centered in the pill
        total_bars_width = NUM_BARS * BAR_WIDTH + (NUM_BARS - 1) * BAR_GAP
        start_x = (bounds.size.width - total_bars_width) / 2
        max_bar_height = bounds.size.height - 12
        min_bar_height = 4

        if self._discard_mode:
            # Dim orange for discard feedback
            bar_main = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 0.5, 0.2, 0.7
            )
            bar_glow = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 0.5, 0.2, 0.4
            )
        else:
            # Neon purple with glow
            bar_main = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.75, 0.2, 1.0, 1.0
            )
            bar_glow = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.75, 0.2, 1.0, 0.6
            )

        context = NSGraphicsContext.currentContext().CGContext()

        for i in range(NUM_BARS):
            level = self._levels[i]
            bar_height = max(min_bar_height, level * max_bar_height)
            x = start_x + i * (BAR_WIDTH + BAR_GAP)
            y = (bounds.size.height - bar_height) / 2

            bar_rect = NSMakeRect(x, y, BAR_WIDTH, bar_height)
            bar_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bar_rect, BAR_RADIUS, BAR_RADIUS
            )

            # Draw glow layer behind the bar
            Quartz.CGContextSaveGState(context)
            Quartz.CGContextSetShadowWithColor(
                context, (0, 0), 6.0, bar_glow.CGColor()
            )
            bar_main.setFill()
            bar_path.fill()
            Quartz.CGContextRestoreGState(context)

            # Draw the bar again on top for a bright core
            bar_main.setFill()
            bar_path.fill()


class WaveformAnimator(NSObject):
    """NSTimer target. Does level calculation in pure Python to avoid ObjC type issues."""

    def initWithView_(self, view):
        self = objc.super(WaveformAnimator, self).init()
        if self is None:
            return None
        self._view = view
        self._overlay = None  # Set by RecordingOverlay after init
        self._tick = 0        # Frame counter for the processing pulse
        return self

    def discardDone_(self, timer):
        if self._overlay:
            self._overlay._end_discard()

    def timerFired_(self, timer):
        view = self._view
        self._tick += 1

        if view._anim_mode == "processing":
            # Indeterminate "working" state: a gentle traveling wave across the
            # bars, independent of audio (the mic is already stopped).
            for i in range(NUM_BARS):
                phase = self._tick * 0.35 + i * 0.6
                view._levels[i] = 0.32 + 0.32 * (0.5 + 0.5 * math.sin(phase))
            view.setNeedsDisplay_(True)
            return

        rms = float(audio.get_rms_level())

        # Treat RMS as a binary: speaking (any sound at all) vs silence.
        # Even very quiet speech (~0.005 RMS) should trigger full animation.
        is_speaking = rms > config.VAD_SPEAKING_FLOOR

        for i in range(NUM_BARS):
            if is_speaking:
                # Lively random movement — bars dance independently
                target = random.uniform(0.3, 1.0)
            else:
                # Silence — bars settle to a low idle pulse
                target = random.uniform(0.05, 0.15)

            # Smooth transitions
            old = view._levels[i]
            if target > old:
                view._levels[i] = old * 0.3 + target * 0.7  # Fast rise
            else:
                view._levels[i] = old * 0.6 + target * 0.4  # Medium fall

        view.setNeedsDisplay_(True)


class RecordingOverlay:
    """Manages the floating indicator lifecycle."""

    def __init__(self):
        self._panel = None
        self._timer = None
        self._discard_timer = None
        self._waveform_view = None
        self._animator = None

    def _cancel_discard(self):
        """Invalidate any pending discard auto-hide timer. Prevents a stale 0.5s
        discard timer from hiding the pill during a freshly-started recording."""
        if self._discard_timer is not None:
            self._discard_timer.invalidate()
            self._discard_timer = None

    def _build_panel(self):
        screen = NSScreen.mainScreen()
        screen_frame = screen.visibleFrame()
        x = (screen_frame.size.width - PANEL_WIDTH) / 2 + screen_frame.origin.x
        y = screen_frame.origin.y + 12  # Small spacer from bottom
        rect = NSMakeRect(x, y, PANEL_WIDTH, PANEL_HEIGHT)

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(Quartz.kCGFloatingWindowLevel + 1)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)
        panel.setHasShadow_(True)
        panel.setCollectionBehavior_(1 << 0 | 1 << 4)

        view = WaveformView.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT)
        )
        panel.setContentView_(view)

        self._panel = panel
        self._waveform_view = view
        self._animator = WaveformAnimator.alloc().initWithView_(view)
        self._animator._overlay = self

    def _start_timer(self):
        """Start the ~20fps animation timer if not already running. Main thread only."""
        if self._timer is not None:
            return
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05,
            self._animator,
            "timerFired:",
            None,
            True,
        )

    def show(self):
        """Show the overlay in live-recording mode and start animation. Main thread only."""
        if self._panel is None:
            self._build_panel()

        self._cancel_discard()  # don't let a stale discard timer hide this recording
        self._waveform_view._anim_mode = "recording"
        self._waveform_view._discard_mode = False
        self._waveform_view._levels = [0.0] * NUM_BARS
        self._panel.orderFront_(None)
        self._start_timer()

    def show_processing(self):
        """Switch the (already-visible) overlay into an indeterminate 'processing'
        pulse and keep it on screen until hide()/flash_discard(). Main thread only.

        Called on hotkey release so the user has feedback near the cursor for the
        whole inference+paste window, not just the menubar ellipsis."""
        if self._panel is None:
            self._build_panel()

        self._cancel_discard()
        self._waveform_view._anim_mode = "processing"
        self._waveform_view._discard_mode = False
        self._panel.orderFront_(None)
        self._start_timer()  # No-op if the recording timer is already running

    def flash_discard(self):
        """Briefly show the overlay in discard mode (orange) for 0.5s, then hide.
        Stops any live/processing animation first. Main thread only."""
        if self._panel is None:
            self._build_panel()
        # Stop the live/processing animation so it doesn't fight the static flash
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        self._cancel_discard()  # replace any in-flight discard timer with this one
        self._waveform_view._discard_mode = True
        # Set bars to a static mid-level for the flash
        self._waveform_view._levels = [0.4] * NUM_BARS
        self._waveform_view.setNeedsDisplay_(True)
        self._panel.orderFront_(None)
        # Auto-hide after 0.5s
        self._discard_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self._animator, "discardDone:", None, False,
        )

    def _end_discard(self):
        """Hide after discard flash."""
        self._discard_timer = None  # one-shot has fired; drop the reference
        if self._waveform_view:
            self._waveform_view._discard_mode = False
            self._waveform_view._anim_mode = "recording"
        if self._panel:
            self._panel.orderOut_(None)

    def hide(self):
        """Hide the overlay and stop animation. Main thread only.
        Safe to call from any _process exit branch — idempotent."""
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        self._cancel_discard()
        if self._waveform_view:
            self._waveform_view._discard_mode = False
            self._waveform_view._anim_mode = "recording"
        if self._panel:
            self._panel.orderOut_(None)
