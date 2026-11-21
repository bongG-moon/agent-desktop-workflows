"""Read the current UIA Table and Grid interfaces without materializing offscreen rows."""
import ctypes as C
from contextlib import closing

import win_bridge as win


def _pattern(element, pattern, iid):
    pointer = C.c_void_p()
    interface = win.GUID.parse(iid)
    element.call(14, (C.c_int32, C.POINTER(win.GUID), C.POINTER(C.c_void_p)),
                 pattern, C.byref(interface), C.byref(pointer))
    if not pointer.value:
        raise win.BridgeError('result_provider_unavailable')
    return win._Com(pointer, element.uia)


def headers(element, *, cell=False):
    # Both Table.GetCurrentColumnHeaders and TableItem.GetCurrentColumnHeaderItems occupy slot 4.
    pattern_id, iid = ((10013, '0b964eb3-ef2e-4464-9c79-61d61737a27e') if cell else
                       (10012, '620e691c-ea96-4710-a850-754b24ce2417'))
    with closing(_pattern(element, pattern_id, iid)) as pattern:
        pointer = C.c_void_p()
        pattern.call(4, (C.POINTER(C.c_void_p),), C.byref(pointer))
        if not pointer.value:
            return []
        with closing(win._Com(pointer, element.uia)) as array:
            count = C.c_int32()
            array.call(3, (C.POINTER(C.c_int32),), C.byref(count))
            if not 0 <= count.value <= 256:
                raise win.BridgeError('result_grid_scan_limit')
            items = []
            for index in range(count.value):
                raw = C.c_void_p()
                array.call(4, (C.c_int32, C.POINTER(C.c_void_p)), index, C.byref(raw))
                if not raw.value:
                    raise win.BridgeError('result_grid_incomplete')
                items.append(win.Element(raw, element.uia))
            return items


def column_cells(table, column, rows):
    # GetItem is read-only. Do not scroll, realize an item or change selection.
    with closing(_pattern(table, 10006, '414c3cdc-856b-4f5b-8538-3131c6302550')) as grid:
        for row in range(rows):
            raw = C.c_void_p()
            grid.call(3, (C.c_int32, C.c_int32, C.POINTER(C.c_void_p)), row, column, C.byref(raw))
            if not raw.value:
                raise win.BridgeError('result_grid_incomplete')
            yield win.Element(raw, table.uia)
