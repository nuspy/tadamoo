# The plan page: diagram, zoom, deleting, layout

Files: `serve/web/plans.js` (the page), `serve/web/planzoom.js` (zoom and pan), `serve/web/plans.css`.

## Layout

The Plans page is one window-sized layout. The top bar, the plan's title and tabs and the bar of the Define tab never scroll away. Under them is a **fixed pane**: the diagram on
the left and the inspector (the properties of the chosen step) on the right, as high as the pane, with a scroll of its own. The pane is **a third of the window high** until the person
drags the **divider** under it (`role="separator"`, focusable, ArrowUp/ArrowDown change it by 2%, Shift by 10%, Home and End are the limits 15% and 75%, Enter puts it back to a third;
`aria-valuenow` is the percent of the window; the height is kept in `localStorage` `tadamoo.plans.pane`). On a touch screen the divider is 44 px high. Everything else (the proposed changes,
the tasks for the person, the log, the history, the kinds of step and the instruction line) is one **lower part** that scrolls. On a phone the inspector is at the top of the lower part,
because there is no room beside the diagram.

An empty note field is one line and grows as it is typed in. A block with nothing to show (no proposals, no tasks, no history) is one line. The history and the run log are one line for
each entry, left aligned, in the monospace font, the text cut with an ellipsis (the whole line is its tooltip). A mouse gets tighter rows than a finger (`@media (pointer: fine)`); a touch
screen keeps 44 px targets.

## Zoom and pan

The diagram is an SVG drawn at its layout size times the zoom (the `viewBox` stays), inside a frame that scrolls. Zooming is the size it is drawn at; panning is the frame's scroll.

- The **mouse wheel** zooms around the pointer, only while the pointer is over the diagram (the page does not scroll then); Ctrl + wheel and a trackpad pinch do the same. Limits 25% to 300%.
- Holding the **middle button** pans: the diagram follows the pointer. The cursor is an open hand over the empty background, a closed one while panning, and keeps its own shape over a step
  (move), a link (pointer) and a connector (crosshair).
- Touch: two fingers pinch-zoom around their middle, one finger on the empty background pans; a finger on a step still drags the step. The frame's `touch-action` is `none`.
- Keyboard (from a step or the frame): `+`, `=` and `-` zoom by a quarter, `0` goes back to 100%.
- A bar under the diagram has minus, the zoom in percent (it also resets to 100%), plus and **Fit** (all steps in view, never above 100%). A plan opens fitted, but not below 50%, so the text
  stays readable.

Node drag, links and the palette read the SVG's screen matrix, so they follow the zoom.

## The start step, deleting a step

A step may sit at a negative place (the start of a plan is at x = -230): the drawing's `viewBox` starts at the smallest place, so it is drawn.

**Delete** (or Backspace) on a chosen step, or the button under the diagram (44 px on a touch screen), takes the step and its links off the plan. **Undo** (the button, or Ctrl+Z in the diagram)
puts the step back with its links, for 20 seconds. The two-click "Remove" button in the inspector is still there.

Tests: `serve/tests/test_plan_zoom_page.py`, `serve/tests/test_plan_layout_page.py` (a real browser at 360x640, 390x844 and 1280x800, in English and Italian).
