"""Briefly serve UTF-8 paste on the private X display; never retain text."""
import select
import sys
import time
from Xlib import X, Xatom, display, protocol


def main():
    text = sys.stdin.buffer.read(65537)
    if len(text) > 65536:
        return 1
    connection = display.Display()
    try:
        root = connection.screen().root
        owner = root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
        clipboard = connection.intern_atom('CLIPBOARD')
        targets = connection.intern_atom('TARGETS')
        utf8 = connection.intern_atom('UTF8_STRING')
        owner.set_selection_owner(clipboard, X.CurrentTime)
        connection.sync()
        if connection.get_selection_owner(clipboard).id != owner.id:
            return 1
        print('ready', flush=True)
        deadline = time.monotonic() + 8
        delivered = set()
        acknowledged = False
        while time.monotonic() < deadline:
            if not connection.pending_events():
                select.select([connection.fileno()], [], [], max(0, deadline-time.monotonic()))
                if not connection.pending_events():
                    continue
            event = connection.next_event()
            if event.type == X.SelectionClear:
                return 1
            if event.type == X.PropertyNotify and (event.window.id, event.atom) in delivered and event.state == X.PropertyDelete:
                # Chrome may prefetch clipboard data before processing Ctrl+V.
                # Keep ownership until the parent finishes that key sequence.
                if not acknowledged:
                    print('served', flush=True)
                    acknowledged = True
            if event.type != X.SelectionRequest:
                continue
            property_atom = event.property or event.target
            accepted = X.NONE
            if event.selection == clipboard and event.target == targets:
                event.requestor.change_property(property_atom, Xatom.ATOM, 32, [targets, utf8])
                accepted = property_atom
            elif event.selection == clipboard and event.target == utf8:
                event.requestor.change_attributes(event_mask=X.PropertyChangeMask)
                event.requestor.change_property(property_atom, utf8, 8, text)
                delivered.add((event.requestor.id, property_atom))
                accepted = property_atom
            event.requestor.send_event(protocol.event.SelectionNotify(time=event.time, requestor=event.requestor, selection=event.selection, target=event.target, property=accepted), propagate=False)
            connection.flush()
        return 1
    finally:
        # Closing the owning X connection clears the selection, including on
        # timeout/error. No clipboard manager runs on this private display.
        connection.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(1)
