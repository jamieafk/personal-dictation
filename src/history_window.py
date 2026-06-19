"""Transcription history window. Search, pagination, export."""

import datetime
import os
import objc
from AppKit import (
    NSWindow, NSScrollView, NSView, NSTextField, NSButton, NSFont, NSColor,
    NSPasteboard, NSBezelStyleAccessoryBarAction, NSApplication, NSSearchField,
    NSSavePanel, NSAlert, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
    NSBackingStoreBuffered, NSLineBreakByTruncatingTail,
    NSTextAlignmentLeft, NSTextAlignmentRight,
)
from Foundation import NSMakeRect, NSObject, NSTimer
from src import history

try:
    from AppKit import NSPasteboardTypeString
except ImportError:
    NSPasteboardTypeString = "public.utf8-plain-text"

WINDOW_WIDTH = 480
WINDOW_HEIGHT = 600
ROW_HEIGHT = 80
TOOLBAR_HEIGHT = 90  # Header + search bar
PADDING = 12
PAGE_SIZE = 50


def _create_label(text, frame, font=None, color=None, alignment=NSTextAlignmentLeft,
                  selectable=False):
    label = NSTextField.alloc().initWithFrame_(frame)
    label.setStringValue_(text)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(selectable)
    label.setAlignment_(alignment)
    if font:
        label.setFont_(font)
    if color:
        label.setTextColor_(color)
    return label


class CopyButtonTarget(NSObject):
    def initWithText_(self, text):
        self = objc.super(CopyButtonTarget, self).init()
        if self is None:
            return None
        self._text = text
        self._revert_btn = None
        return self

    def copyText_(self, sender):
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(self._text, NSPasteboardTypeString)
        sender.setTitle_("Copied!")
        sender.setEnabled_(False)
        self._revert_btn = sender
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.5, self, "revertButton:", None, False,
        )

    def revertButton_(self, timer):
        if self._revert_btn:
            self._revert_btn.setTitle_("Copy")
            self._revert_btn.setEnabled_(True)


class DeleteButtonTarget(NSObject):
    def initWithEntry_controller_(self, entry, controller):
        self = objc.super(DeleteButtonTarget, self).init()
        if self is None:
            return None
        self._entry = entry
        self._controller = controller
        self._timer = None
        self._btn = None
        return self

    def deleteEntry_(self, sender):
        if self._timer is not None:
            # User clicked "Undo" — cancel the pending delete
            self._timer.invalidate()
            self._timer = None
            sender.setTitle_("Delete")
            return
        # Enter undo window — 3 seconds to cancel
        sender.setTitle_("Undo")
        self._btn = sender
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.0, self, "commitDelete:", None, False,
        )

    def commitDelete_(self, timer):
        self._timer = None
        history.delete_entry(self._entry)
        self._controller._reload_and_show()


class DeleteAllTarget(NSObject):
    def initWithController_(self, controller):
        self = objc.super(DeleteAllTarget, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def deleteAll_(self, sender):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Delete All Transcriptions?")
        alert.setInformativeText_(
            "This will permanently delete your entire transcription history. "
            "This cannot be undone."
        )
        alert.addButtonWithTitle_("Delete All")
        alert.addButtonWithTitle_("Cancel")
        alert.setAlertStyle_(2)  # NSAlertStyleCritical
        if alert.runModal() == 1000:  # NSAlertFirstButtonReturn
            history.delete_all()
            self._controller._reload_and_show()


class SearchHandler(NSObject):
    """Handles search field changes."""
    def initWithController_(self, controller):
        self = objc.super(SearchHandler, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def searchChanged_(self, sender):
        query = sender.stringValue()
        self._controller._apply_search(str(query))


class LoadMoreTarget(NSObject):
    def initWithController_(self, controller):
        self = objc.super(LoadMoreTarget, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def loadMore_(self, sender):
        self._controller._load_more()


class ExportTarget(NSObject):
    def initWithController_(self, controller):
        self = objc.super(ExportTarget, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def exportHistory_(self, sender):
        self._controller._export()


class HistoryWindowController:
    _instance = None
    _window = None
    _button_targets = []
    _all_entries = []
    _filtered_entries = []
    _visible_count = PAGE_SIZE
    _search_query = ""
    _scroll_view = None
    _search_handler = None
    _load_more_target = None
    _export_target = None
    _delete_all_target = None

    @classmethod
    def show(cls):
        if cls._instance is None:
            cls._instance = cls()
        cls._instance._reload_and_show()

    @classmethod
    def refresh_if_visible(cls):
        if cls._instance and cls._window and cls._window.isVisible():
            cls._instance._reload_and_show()

    def _reload_and_show(self):
        self._all_entries = history.load_all()
        self._all_entries.reverse()
        self._visible_count = PAGE_SIZE

        if self.__class__._window is None:
            self._build_window()

        self._apply_search(self._search_query)

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.__class__._window.makeKeyAndOrderFront_(None)

    def _apply_search(self, query):
        self._search_query = query
        if query:
            q = query.lower()
            self._filtered_entries = [
                e for e in self._all_entries
                if q in e.text.lower() or q in e.app_name.lower()
            ]
        else:
            self._filtered_entries = self._all_entries
        self._visible_count = min(self._visible_count, max(PAGE_SIZE, len(self._filtered_entries)))
        self._populate()

    def _load_more(self):
        self._visible_count += PAGE_SIZE
        self._populate()

    def _build_window(self):
        style = (
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(200, 200, WINDOW_WIDTH, WINDOW_HEIGHT),
            style, NSBackingStoreBuffered, False,
        )
        window.setTitle_("Transcription History")
        window.setFrameAutosaveName_("DictationHistory")
        window.setMinSize_((360, 300))
        window.setReleasedWhenClosed_(False)
        self.__class__._window = window

    def _populate(self):
        window = self.__class__._window
        content_frame = window.contentView().frame()
        width = content_frame.size.width
        self.__class__._button_targets = []

        container = NSView.alloc().initWithFrame_(content_frame)

        # --- Toolbar: title, word count, search, export ---
        toolbar_y = content_frame.size.height - TOOLBAR_HEIGHT

        # Title
        title_label = _create_label(
            "Transcription History",
            NSMakeRect(PADDING, toolbar_y + 52, width / 2, 24),
            font=NSFont.boldSystemFontOfSize_(16),
        )
        container.addSubview_(title_label)

        # Word count
        total_words = sum(e.word_count for e in self._all_entries)
        count_label = _create_label(
            f"{total_words:,} words",
            NSMakeRect(width / 2, toolbar_y + 52, width / 2 - PADDING - 140, 24),
            font=NSFont.systemFontOfSize_(13),
            color=NSColor.secondaryLabelColor(),
            alignment=NSTextAlignmentRight,
        )
        container.addSubview_(count_label)

        # Delete All button
        if self._delete_all_target is None:
            self._delete_all_target = DeleteAllTarget.alloc().initWithController_(self)
        delete_all_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - PADDING - 55 - 4 - 70, toolbar_y + 52, 70, 24)
        )
        delete_all_btn.setTitle_("Delete All")
        delete_all_btn.setBezelStyle_(NSBezelStyleAccessoryBarAction)
        delete_all_btn.setFont_(NSFont.systemFontOfSize_(11))
        delete_all_btn.setTarget_(self._delete_all_target)
        delete_all_btn.setAction_("deleteAll:")
        container.addSubview_(delete_all_btn)

        # Export button
        if self._export_target is None:
            self._export_target = ExportTarget.alloc().initWithController_(self)
        export_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - PADDING - 55, toolbar_y + 52, 55, 24)
        )
        export_btn.setTitle_("Export")
        export_btn.setBezelStyle_(NSBezelStyleAccessoryBarAction)
        export_btn.setFont_(NSFont.systemFontOfSize_(11))
        export_btn.setTarget_(self._export_target)
        export_btn.setAction_("exportHistory:")
        container.addSubview_(export_btn)

        # Search field
        if self._search_handler is None:
            self._search_handler = SearchHandler.alloc().initWithController_(self)
        search_field = NSSearchField.alloc().initWithFrame_(
            NSMakeRect(PADDING, toolbar_y + 14, width - 2 * PADDING, 28)
        )
        search_field.setPlaceholderString_("Search transcriptions...")
        search_field.setFont_(NSFont.systemFontOfSize_(13))
        search_field.setStringValue_(self._search_query or "")
        search_field.setTarget_(self._search_handler)
        search_field.setAction_("searchChanged:")
        container.addSubview_(search_field)

        # --- Results info ---
        showing = min(self._visible_count, len(self._filtered_entries))
        total = len(self._filtered_entries)
        if self._search_query:
            info_text = f"{total} results for \"{self._search_query}\""
        else:
            info_text = f"Showing {showing} of {total}"
        info_label = _create_label(
            info_text,
            NSMakeRect(PADDING, toolbar_y - 4, width - 2 * PADDING, 16),
            font=NSFont.systemFontOfSize_(11),
            color=NSColor.tertiaryLabelColor(),
        )
        container.addSubview_(info_label)

        # --- Scroll view ---
        scroll_top = toolbar_y - 10
        scroll_frame = NSMakeRect(0, 0, width, scroll_top)
        scroll_view = NSScrollView.alloc().initWithFrame_(scroll_frame)
        scroll_view.setHasVerticalScroller_(True)
        scroll_view.setAutohidesScrollers_(True)

        entries_to_show = self._filtered_entries[:self._visible_count]
        has_more = len(self._filtered_entries) > self._visible_count
        extra_row = ROW_HEIGHT if has_more else 0
        total_height = max(len(entries_to_show) * ROW_HEIGHT + extra_row, scroll_frame.size.height)
        doc_width = width - 20
        doc_view = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, doc_width, total_height)
        )

        now = datetime.datetime.now()
        for i, entry in enumerate(entries_to_show):
            row_y = total_height - (i + 1) * ROW_HEIGHT
            row = self._make_row(entry, NSMakeRect(0, row_y, doc_width, ROW_HEIGHT), now)
            doc_view.addSubview_(row)

        # "Load More" button
        if has_more:
            remaining = len(self._filtered_entries) - self._visible_count
            if self._load_more_target is None:
                self._load_more_target = LoadMoreTarget.alloc().initWithController_(self)
            btn_y = total_height - len(entries_to_show) * ROW_HEIGHT - ROW_HEIGHT
            load_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(doc_width / 2 - 60, btn_y + 25, 120, 30)
            )
            load_btn.setTitle_(f"Load {min(remaining, PAGE_SIZE)} more")
            load_btn.setBezelStyle_(NSBezelStyleAccessoryBarAction)
            load_btn.setFont_(NSFont.systemFontOfSize_(12))
            load_btn.setTarget_(self._load_more_target)
            load_btn.setAction_("loadMore:")
            doc_view.addSubview_(load_btn)

        scroll_view.setDocumentView_(doc_view)
        container.addSubview_(scroll_view)

        window.setContentView_(container)

        # Scroll to top (most recent entries)
        clip = scroll_view.contentView()
        clip.scrollToPoint_((0, total_height - scroll_frame.size.height))
        scroll_view.reflectScrolledClipView_(clip)

        self._scroll_view = scroll_view

    def _make_row(self, entry, frame, now):
        row = NSView.alloc().initWithFrame_(frame)
        w = frame.size.width

        if entry.timestamp.date() == now.date():
            ts_str = entry.timestamp.strftime("%H:%M")
        else:
            ts_str = entry.timestamp.strftime("%b %d, %H:%M")
        meta_text = f"{ts_str}  \u00b7  {entry.app_name}"

        meta_label = _create_label(
            meta_text,
            NSMakeRect(PADDING, frame.size.height - 24, w - 130, 18),
            font=NSFont.systemFontOfSize_(11),
            color=NSColor.secondaryLabelColor(),
        )
        row.addSubview_(meta_label)

        display_text = entry.text
        if len(display_text) > 200:
            display_text = display_text[:197] + "..."
        text_label = _create_label(
            display_text,
            NSMakeRect(PADDING, 8, w - 130, frame.size.height - 32),
            font=NSFont.systemFontOfSize_(13),
            selectable=True,
        )
        text_label.setLineBreakMode_(NSLineBreakByTruncatingTail)
        row.addSubview_(text_label)

        # Delete button (with 3-second undo)
        del_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(w - 114, (frame.size.height - 24) / 2, 50, 24)
        )
        del_btn.setTitle_("Delete")
        del_btn.setBezelStyle_(NSBezelStyleAccessoryBarAction)
        del_btn.setFont_(NSFont.systemFontOfSize_(11))
        del_target = DeleteButtonTarget.alloc().initWithEntry_controller_(entry, self)
        self.__class__._button_targets.append(del_target)
        del_btn.setTarget_(del_target)
        del_btn.setAction_("deleteEntry:")
        row.addSubview_(del_btn)

        # Copy button
        btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(w - 60, (frame.size.height - 24) / 2, 48, 24)
        )
        btn.setTitle_("Copy")
        btn.setBezelStyle_(NSBezelStyleAccessoryBarAction)
        btn.setFont_(NSFont.systemFontOfSize_(11))
        target = CopyButtonTarget.alloc().initWithText_(entry.text)
        self.__class__._button_targets.append(target)
        btn.setTarget_(target)
        btn.setAction_("copyText:")
        row.addSubview_(btn)

        sep = NSView.alloc().initWithFrame_(NSMakeRect(PADDING, 0, w - 2 * PADDING, 1))
        sep.setWantsLayer_(True)
        sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
        row.addSubview_(sep)

        return row

    def _export(self):
        """Export all history (unfiltered) to a text file via Save dialog."""
        panel = NSSavePanel.savePanel()
        panel.setTitle_("Export Transcription History")
        panel.setNameFieldStringValue_("dictation-history.txt")
        panel.setAllowedContentTypes_([])

        if panel.runModal() == 1:  # OK
            path = str(panel.URL().path())
            entries = history.load_all()
            with open(path, "w") as f:
                for e in entries:
                    ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"{ts} | {e.app_name} | {e.text}\n")
