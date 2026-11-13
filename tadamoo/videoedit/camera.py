"""The virtual camera: a keyframed, eased window over a still or a video that is bigger than the screen it is shown on.

A camera state says where the presenter's face is on the screen (`fx`, `fy`, 0 to 1), how far the window is zoomed in (`zoom`, 1 = the whole picture) and how far it is
rolled (`roll`, degrees). A move goes from one state to another over some seconds with a smooth S-curve (smootherstep: it starts and ends at rest, so nothing jerks). A
planner gives each stretch of presenter picture a move from its attitude and its length, and joins the stretches so that a cut falls on motion: the outgoing move ends
still moving in the direction the incoming one continues, the face is at the same place on the screen on both sides, and the scale on the two sides differs by a small step,
not a jump; a short cross-dissolve covers it.

Limits (all parameters of the settings): the zoom changes by at most `zoom_per_s` times in a second at the fastest point of the S-curve, the roll stays within `roll_max`
degrees, and the face moves sideways by at most `drift_per_s` of the screen's width in a second."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Tuple

FACE_WIDE = (0.47, 0.30)                            # where the face is in the presenter's own picture (x, y), measured on the reference photograph
LIMITS = {"zoom_per_s": 1.10, "roll_max": 4.0, "drift_per_s": 0.03, "plateau_s": 4.0, "move_min_s": 3.0, "dissolve_frames": 6}


@dataclass(frozen=True)
class State:
    zoom: float = 1.0
    fx: float = FACE_WIDE[0]
    fy: float = FACE_WIDE[1]
    roll: float = 0.0


def smooth(u: float) -> float:
    """Smootherstep: 0 to 1 with zero speed and zero acceleration at both ends."""
    u = min(1.0, max(0.0, u))
    return u * u * u * (u * (6 * u - 15) + 10)


def blend(a: State, b: State, u: float) -> State:
    """The state a fraction `u` of the way from a to b; the zoom changes by a constant ratio (it looks even), the rest in a straight line."""
    k = smooth(u)
    return State(a.zoom * (b.zoom / a.zoom) ** k, a.fx + (b.fx - a.fx) * k, a.fy + (b.fy - a.fy) * k, a.roll + (b.roll - a.roll) * k)


@dataclass(frozen=True)
class Move:
    name: str
    start: State
    end: State
    seconds: float

    def at(self, t: float) -> State:
        return blend(self.start, self.end, t / self.seconds if self.seconds > 0 else 1.0)

    def peak_zoom_per_s(self) -> float:
        """The fastest the zoom changes: the S-curve's steepest point is 1.875 times the average speed."""
        if self.seconds <= 0:
            return 1.0
        return (self.end.zoom / self.start.zoom) ** (1.875 / self.seconds) if self.end.zoom >= self.start.zoom else (self.start.zoom / self.end.zoom) ** (1.875 / self.seconds)

    def peak_drift_per_s(self) -> float:
        return abs(self.end.fx - self.start.fx) * 1.875 / max(self.seconds, 1e-6)


# ---- the library of moves: (start state, seconds) -> end state
def _push_in(s: State, seconds: float, amount: float) -> State:
    return replace(s, zoom=s.zoom * amount, fx=0.5 + (s.fx - 0.5) * 0.4, fy=min(0.46, s.fy + 0.05 * (amount - 1) / 0.3))


def _pull_out(s: State, seconds: float, amount: float) -> State:
    return replace(s, zoom=max(1.0, s.zoom / amount), fy=max(0.30, s.fy - 0.04))


def _drift(s: State, seconds: float, amount: float, sign: float) -> State:
    return replace(s, fx=min(0.62, max(0.38, s.fx + sign * 0.03 * min(seconds, 5.0))), zoom=s.zoom * (1 + (amount - 1) * 0.5))


def _roll(s: State, seconds: float, amount: float) -> State:
    return replace(s, roll=min(LIMITS["roll_max"], max(-LIMITS["roll_max"], -s.roll if abs(s.roll) > 1 else 1.6)), zoom=s.zoom * (1 + (amount - 1) * 0.6))


MOVES: Dict[str, Callable[..., State]] = {
    "push_in": lambda s, sec, amount: _push_in(s, sec, amount),
    "pull_out": lambda s, sec, amount: _pull_out(s, sec, amount),
    "drift_right": lambda s, sec, amount: _drift(s, sec, amount, 1.0),
    "drift_left": lambda s, sec, amount: _drift(s, sec, amount, -1.0),
    "roll": lambda s, sec, amount: _roll(s, sec, amount),
    "dolly_roll": lambda s, sec, amount: replace(_roll(_push_in(s, sec, amount), sec, amount), zoom=s.zoom * amount),
    "hold": lambda s, sec, amount: s,
}
START = {"warm": State(1.0, 0.47, 0.30, 0.0), "firm": State(1.22, 0.50, 0.40, 0.0)}      # where a block of each attitude begins: warm wide and open, firm tighter and steadier


def amount_for(seconds: float, per_second: float) -> float:
    """The zoom ratio a move of this length may reach: a gentle share of what the limit allows (the S-curve's peak is 1.875 times the average)."""
    return min(1.45, per_second ** (seconds / 1.875) * 0.97) if seconds > 0 else 1.0


def make_move(name: str, start: State, seconds: float, limits: Optional[Dict[str, float]] = None) -> Move:
    lim = {**LIMITS, **(limits or {})}
    amount = min(1.0 + 0.045 * seconds, amount_for(seconds, lim["zoom_per_s"]), 1.38)
    end = MOVES[name](start, seconds, amount)
    end = replace(end, roll=max(-lim["roll_max"], min(lim["roll_max"], end.roll)))
    move = Move(name, start, end, seconds)
    while (move.peak_zoom_per_s() > lim["zoom_per_s"] or move.peak_drift_per_s() > lim["drift_per_s"]) and move.end != move.start:
        end = blend(move.start, move.end, 0.8)                      # a move that is too fast is shortened in amplitude until it is not
        move = Move(name, start, end, seconds)
    return move


def keyframed(name: str, start: State, end: State, seconds: float, limits: Optional[Dict[str, float]] = None) -> Move:
    """A move between two states given by the caller, shortened in amplitude until it breaks no limit (the roll is held within its limit first)."""
    lim = {**LIMITS, **(limits or {})}
    start = replace(start, roll=max(-lim["roll_max"], min(lim["roll_max"], start.roll)))
    end = replace(end, roll=max(-lim["roll_max"], min(lim["roll_max"], end.roll)))
    move = Move(name, start, end, seconds)
    while (move.peak_zoom_per_s() > lim["zoom_per_s"] or move.peak_drift_per_s() > lim["drift_per_s"]) and move.end != move.start:
        move = Move(name, start, blend(move.start, move.end, 0.8), seconds)
    return move


def check(move: Move, limits: Optional[Dict[str, float]] = None) -> List[str]:
    """Why a move breaks the limits, in words; empty when it does not."""
    lim = {**LIMITS, **(limits or {})}
    out = []
    if move.peak_zoom_per_s() > lim["zoom_per_s"] + 1e-6:
        out.append(f"{move.name}: the zoom changes by {move.peak_zoom_per_s():.3f} times a second, at most {lim['zoom_per_s']}")
    if max(abs(move.start.roll), abs(move.end.roll)) > lim["roll_max"] + 1e-6:
        out.append(f"{move.name}: the roll is more than {lim['roll_max']} degrees")
    if move.peak_drift_per_s() > lim["drift_per_s"] + 1e-6:
        out.append(f"{move.name}: the face drifts by {move.peak_drift_per_s():.3f} of the width a second, at most {lim['drift_per_s']}")
    return out


# ---- the planner: moves for a run of presenter pictures, joined on motion
def sequence(stretches: List[Tuple[str, float]], limits: Optional[Dict[str, float]] = None, previous: Optional[State] = None, continuous: bool = False) -> List[Move]:
    """A move for each stretch of presenter picture, given as (attitude, seconds). The first stretch starts where its attitude begins, or, when `previous` (where the
    stretch before ended) is given, a small step back from it, at the same face position: a cut on motion; with `continuous` it starts exactly where the one before ended, so that the
    camera never jumps (the picture is one continuous take). Each next stretch takes up where the one before ended.
    Warm stretches push in towards the viewer and then drift or roll a little; firm stretches stay steadier, lower and tighter."""
    lim = {**LIMITS, **(limits or {})}
    out: List[Move] = []
    cycle = {"warm": ["push_in", "drift_right", "pull_out", "dolly_roll", "drift_left"], "firm": ["push_in", "hold", "drift_left", "push_in"]}
    count = 0
    for attitude, seconds in stretches:
        names = cycle.get(attitude, cycle["warm"])
        if out:
            start = out[-1].end
        elif previous is not None:
            start = previous if continuous else replace(previous, zoom=max(1.0, previous.zoom * 0.94))
        else:
            start = START.get(attitude, START["warm"])
        if attitude == "firm" and start.zoom < START["firm"].zoom and not (continuous and previous is not None):
            start = replace(start, zoom=START["firm"].zoom, fy=START["firm"].fy)
        name = names[count % len(names)]
        if seconds < lim["move_min_s"] and name in ("roll", "dolly_roll"):
            name = "push_in"
        if start.zoom > 1.45 and name in ("push_in", "dolly_roll"):
            name = "pull_out"                                       # never past a tight close-up: the source would show its softness
        move = make_move(name, start, seconds, lim)
        out.append(move)
        count += 1
    return out
