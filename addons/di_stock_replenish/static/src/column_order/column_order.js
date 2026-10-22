import { useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";

/**
 * What the replenishment grids share about their columns: an order the worker
 * drags into place, remembered in this browser, and Enter moving down a
 * column of quantity cells. Used by the stock comparison here and by
 * di_pos_replenish's sales grid, which depends on this module - one copy, so
 * the two screens can never start behaving differently.
 */

function readOrder(storageKey) {
    try {
        const stored = JSON.parse(browser.localStorage.getItem(storageKey) || "[]");
        return Array.isArray(stored) ? stored.filter((key) => typeof key === "string") : [];
    } catch {
        // Private window, blocked storage, or a value that is not JSON: the
        // grid simply opens in its default order.
        return [];
    }
}

function writeOrder(storageKey, order) {
    try {
        if (order.length) {
            browser.localStorage.setItem(storageKey, JSON.stringify(order));
        } else {
            browser.localStorage.removeItem(storageKey);
        }
    } catch {
        // Not remembered, but the order still holds for this session.
    }
}

/**
 * ``saved`` laid over ``keys`` (the default order): every saved key that still
 * exists keeps its place, and a column the saved order has never seen - a
 * shop or a warehouse added since, say - goes in right after the column it
 * follows by default, rather than at the far end.
 */
export function mergeOrder(saved, keys) {
    const result = saved.filter((key) => keys.includes(key));
    keys.forEach((key, index) => {
        if (result.includes(key)) {
            return;
        }
        let position = 0;
        for (let i = index - 1; i >= 0; i--) {
            const found = result.indexOf(keys[i]);
            if (found !== -1) {
                position = found + 1;
                break;
            }
        }
        result.splice(position, 0, key);
    });
    return result;
}

const DRAGGING = "di_col_dragging";
const DROP_BEFORE = "di_col_drop_before";
const DROP_AFTER = "di_col_drop_after";

/**
 * Draggable column order for one grid, kept under ``storageKey``.
 *
 * One key per screen, not per document: it is how this person likes to read
 * the grid, and warehouse ids are stable, so a column keeps its place from one
 * run to the next. The headings bind the drag handlers below and the drop
 * marker is a class set straight on the heading cell: a drag fires dragover
 * dozens of times a second, and re-rendering hundreds of rows for each one
 * would make the header stutter. Only the drop itself touches state.
 */
export function useColumnOrder(storageKey) {
    const state = useState({ order: readOrder(storageKey) });
    let dragKey = null;

    const setOrder = (order) => {
        state.order = order;
        writeOrder(storageKey, order);
    };
    const clearMarker = (cell) => cell.classList.remove(DROP_BEFORE, DROP_AFTER);

    return {
        get hasCustomOrder() {
            return state.order.length > 0;
        },
        /** The full order over ``defaultKeys``, hidden columns included, so
         * a column switched back on returns to where it was dragged. */
        keys(defaultKeys) {
            return mergeOrder(state.order, defaultKeys);
        },
        reset() {
            setOrder([]);
        },
        onDragStart(key, ev) {
            dragKey = key;
            ev.dataTransfer.effectAllowed = "move";
            // Firefox will not start a drag without some data on it.
            ev.dataTransfer.setData("text/plain", key);
            ev.currentTarget.classList.add(DRAGGING);
        },
        /** Which half of the heading the pointer is over decides the side the
         * column lands on, and the marker shows it before the drop. */
        onDragOver(key, ev) {
            if (!dragKey || dragKey === key) {
                return;
            }
            ev.preventDefault();
            ev.dataTransfer.dropEffect = "move";
            const cell = ev.currentTarget;
            const rect = cell.getBoundingClientRect();
            const after = ev.clientX > rect.left + rect.width / 2;
            cell.classList.toggle(DROP_BEFORE, !after);
            cell.classList.toggle(DROP_AFTER, after);
        },
        onDragLeave(ev) {
            clearMarker(ev.currentTarget);
        },
        onDrop(key, defaultKeys, ev) {
            ev.preventDefault();
            const dragged = dragKey;
            const after = ev.currentTarget.classList.contains(DROP_AFTER);
            clearMarker(ev.currentTarget);
            if (!dragged || dragged === key) {
                return;
            }
            const order = mergeOrder(state.order, defaultKeys).filter((k) => k !== dragged);
            const target = order.indexOf(key);
            order.splice(after ? target + 1 : target, 0, dragged);
            setOrder(order);
        },
        onDragEnd(ev) {
            dragKey = null;
            ev.currentTarget.classList.remove(DRAGGING);
        },
    };
}

/**
 * Enter goes to the same column's input on the next row, Shift+Enter to the
 * one above, the way a spreadsheet column is filled: type 1, Enter, type 2,
 * Enter. Moving focus is what commits the value - the input left behind
 * fires its change on blur, as it would for a click elsewhere.
 *
 * The inputs carry ``data-column``, and the next one is read off the DOM
 * rather than a row list, so it follows exactly what is on screen: a filter
 * box, group headings and sub-rows without an input are all stepped over.
 * Bound as a keydown handler; anything but Enter is left alone.
 */
export function moveDownColumn(ev, inputSelector) {
    if (ev.key !== "Enter") {
        return;
    }
    ev.preventDefault();
    const input = ev.target;
    const column = input.dataset.column;
    const inputs = [...input.closest("table").querySelectorAll(inputSelector)].filter(
        (el) => el.dataset.column === column
    );
    const next = inputs[inputs.indexOf(input) + (ev.shiftKey ? -1 : 1)];
    if (next) {
        next.focus();
        next.select();
    } else {
        // The last row: nowhere to go, but the value still has to land.
        input.blur();
    }
}
