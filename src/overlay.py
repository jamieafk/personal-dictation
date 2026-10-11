"""Floating Siri-style waveform during recording. Non-focus-stealing NSPanel.

Three tapered sine waves in a dark capsule. Speaking is binary: any sound swells
the waves, silence settles them to a faint shimmer (the indicator shows presence,
not volume). Processing: one line with a traveling bump. Discard: amber, a "no"
shake, fade out.
"""

import math
import time
import objc
from AppKit import (
    NSPanel, NSView, NSColor, NSBezierPath, NSScreen, NSGraphicsContext,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSBackingStoreBuffered, NSRoundLineCapStyle,
)
from Foundation import NSObject, NSTimer, NSMakeRect, NSMakePoint
import Quartz
from src import audio, config

# Panel geometry
PANEL_WIDTH = 100
PANEL_HEIGHT = 28
BOTTOM_GAP = 12        # pt above the visible bottom edge
WAVE_INSET = 10        # horizontal inset of the waves inside the capsule
WAVE_STEPS = 60        # points per curve

# Motion
FRAME_S = 1 / 60
IDLE_AMP = 1.0         # pt, silent shimmer
SPEAK_AMP = 8.0        # pt added while speaking
ATTACK_S = 0.05        # amplitude easing: fast rise...
RELEASE_S = 0.22       # ...softer fall, so word gaps dip instead of snapping flat
PROCESS_S = 0.15       # blend into the processing line
BUMP_PERIOD_S = 1.1    # processing bump sweep
DISCARD_S = 0.7        # discard animation length, then hide
DISCARD_FADE_S = 0.3   # fade-out at the end of it
SHAKE_S = 0.35
SHAKE_PT = 5

PURPLE = (0.72, 0.25, 1.0)
AMBER = (1.0, 0.58, 0.22)
# (rgb, alpha, line width, cycles across, phase speed rad/s, phase offset, amplitude share)
CURVES = (
    (PURPLE, 1.0, 1.7, 2.0, 9.0, 0.0, 1.0),
    ((0.95, 0.47, 1.0), 0.65, 1.2, 2.6, -7.0, 1.7, 0.75),
    ((0.45, 0.38, 1.0), 0.5, 1.0, 1.5, 6.0, 3.1, 0.6),
)
_U = [i / (WAVE_STEPS - 1) for i in range(WAVE_STEPS)]
_TAPER = [math.sin(math.pi * u) for u in _U]  # 0 at both ends
_ENV = [t * t for t in _TAPER]


def _ease(value, target, dt, tau):
    return value + (target - value) * (1 - math.exp(-dt / tau))


class _WaveState:
    """Animation state in pure Python (floats through ObjC become NSNumber)."""

    def __init__(self):
        self.mode = "recording"  # "recording" | "processing" | "discard"
        self.t = 0.0             # animation clock, s
        self.mode_t = 0.0        # self.t when the mode was entered
        self.amp = 0.0           # 0 silent .. 1 speaking, eased
        self.proc = 0.0          # 0..1 blend into the processing line


class WaveformView(NSView):
    """Draws the capsule and waves from a shared _WaveState."""

    def initWithFrame_(self, frame):
        self = objc.super(WaveformView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._state = _WaveState()
        self.setWantsLayer_(True)
        return self

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        s = self._state
        b = self.bounds()
        w, h = b.size.width, b.size.height

        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.25, 0.25, w - 0.5, h - 0.5), h / 2 - 0.25, h / 2 - 0.25
        )
        NSColor.colorWithWhite_alpha_(0.06, 0.92).setFill()
        pill.fill()
        # Hairline edge keeps the pill visible over dark apps
        NSColor.colorWithWhite_alpha_(1.0, 0.13).setStroke()
        pill.setLineWidth_(0.5)
        pill.stroke()

        discard = s.mode == "discard"
        flatten = 1.0 - min(1.0, (s.t - s.mode_t) / DISCARD_S) if discard else 1.0
        amp = (IDLE_AMP + SPEAK_AMP * s.amp) * (1.0 - s.proc) * flatten
        bump_h = 5.0 * s.proc * flatten
        bump_at = (s.t % BUMP_PERIOD_S) / BUMP_PERIOD_S * 1.4 - 0.2  # sweeps past both ends
        cy = h / 2
        span = w - 2 * WAVE_INSET
        context = NSGraphicsContext.currentContext().CGContext()

        for n, (rgb, alpha, width, cycles, speed, offset, share) in enumerate(CURVES):
            if n:  # secondary curves fade out, leaving one processing line
                alpha *= 1.0 - s.proc
                if alpha < 0.01:
                    continue
            k = cycles * 2 * math.pi
            phase = s.t * speed + offset
            a = amp * share
            path = NSBezierPath.bezierPath()
            for i, u in enumerate(_U):
                y = a * _ENV[i] * math.sin(u * k + phase)
                if n == 0 and bump_h:
                    y += bump_h * _TAPER[i] * math.exp(-((u - bump_at) ** 2) / 0.006)
                pt = NSMakePoint(WAVE_INSET + u * span, cy + y)
                if i:
                    path.lineToPoint_(pt)
                else:
                    path.moveToPoint_(pt)
            path.setLineWidth_(width)
            path.setLineCapStyle_(NSRoundLineCapStyle)
            r, g, bl = AMBER if discard else rgb
            color = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, bl, alpha)

            if n == 0:
                # Glow under the lead curve
                Quartz.CGContextSaveGState(context)
                glow = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, bl, 0.8)
                Quartz.CGContextSetShadowWithColor(context, (0, 0), 4.0, glow.CGColor())
                color.setStroke()
                path.stroke()
                Quartz.CGContextRestoreGState(context)
            color.setStroke()
            path.stroke()


class WaveformAnimator(NSObject):
    """NSTimer target; forwards to RecordingOverlay._tick (pure Python)."""

    def initWithOverlay_(self, overlay):
        self = objc.super(WaveformAnimator, self).init()
        if self is None:
            return None
        self._overlay = overlay
        return self

    def timerFired_(self, timer):
        self._overlay._tick()


class RecordingOverlay:
    """Manages the floating indicator lifecycle. All methods: main thread only."""

    def __init__(self):
        self._panel = None
        self._view = None
        self._origin = (0.0, 0.0)
        self._timer = None
        self._animator = None
        self._last_tick = 0.0

    def _build_panel(self):
        screen = NSScreen.mainScreen()
        screen_frame = screen.visibleFrame()
        x = (screen_frame.size.width - PANEL_WIDTH) / 2 + screen_frame.origin.x
        y = screen_frame.origin.y + BOTTOM_GAP
        self._origin = (x, y)
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
        self._view = view
        self._animator = WaveformAnimator.alloc().initWithOverlay_(self)

    def _enter(self, mode):
        """Switch mode, put the panel back in place at full opacity, and animate."""
        if self._panel is None:
            self._build_panel()
        s = self._view._state
        s.mode = mode
        s.mode_t = s.t
        self._panel.setFrameOrigin_(NSMakePoint(*self._origin))
        self._panel.setAlphaValue_(1.0)
        self._view.setNeedsDisplay_(True)
        self._panel.orderFront_(None)
        if self._timer is None:
            self._last_tick = time.monotonic()
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                FRAME_S, self._animator, "timerFired:", None, True,
            )

    def show(self):
        """Show the overlay in live-recording mode."""
        self._enter("recording")
        s = self._view._state
        s.amp = 0.0
        s.proc = 0.0

    def show_processing(self):
        """Morph the (already-visible) overlay into the processing line and keep it
        on screen until hide()/flash_discard().

        Called on hotkey release so the user has feedback near the cursor for the
        whole inference+paste window, not just the menubar ellipsis."""
        self._enter("processing")

    def flash_discard(self):
        """Amber + shake + fade over DISCARD_S, then hide."""
        self._enter("discard")

    def _tick(self):
        now = time.monotonic()
        dt = min(now - self._last_tick, 0.1)  # a stalled run loop shouldn't jump
        self._last_tick = now
        s = self._view._state
        s.t += dt

        speaking = (
            s.mode == "recording"
            and float(audio.get_rms_level()) > config.VAD_SPEAKING_FLOOR
        )
        s.amp = _ease(s.amp, 1.0 if speaking else 0.0, dt, ATTACK_S if speaking else RELEASE_S)
        s.proc = _ease(s.proc, 1.0 if s.mode == "processing" else 0.0, dt, PROCESS_S)

        if s.mode == "discard":
            elapsed = s.t - s.mode_t
            if elapsed >= DISCARD_S:
                self.hide()
                return
            shake = SHAKE_PT * math.sin(elapsed * 45) * max(0.0, 1 - elapsed / SHAKE_S)
            self._panel.setFrameOrigin_(NSMakePoint(self._origin[0] + shake, self._origin[1]))
            self._panel.setAlphaValue_(min(1.0, (DISCARD_S - elapsed) / DISCARD_FADE_S))

        self._view.setNeedsDisplay_(True)

    def hide(self):
        """Hide the overlay and stop animation. Idempotent — safe from any
        _process exit branch."""
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        if self._panel:
            self._panel.orderOut_(None)
