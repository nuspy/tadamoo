import 'dart:math' as math;

import 'avatar_assets.dart';

/// One mouth shape in a segment: from `t` for `dur` milliseconds after the segment starts.
class VisemeEvent {
  const VisemeEvent(this.t, this.dur, this.v);
  final int t;
  final int dur;
  final String v;
}

/// The timeline the PC sends before the voice of a sentence, placed on the clock of the audio: `startMs` is where the sentence begins in the
/// milliseconds of audio handed to the player since its track began.
class VisemeSegment {
  VisemeSegment(this.id, this.startMs, this.durationMs, this.events);

  final int id;
  final int startMs;
  final int durationMs;
  final List<VisemeEvent> events;

  int get endMs => startMs + durationMs;

  /// Reads the PC's `visemes` message (`gateway.md`, Render modes); null when it has no usable duration. An event that is not a whole
  /// time, or names a shape that is not one of the 15, is skipped.
  static VisemeSegment? fromMessage(Map<String, dynamic> m, int startMs, {int sampleRate = 0}) {
    final events = <VisemeEvent>[];
    final raw = m['visemes'];
    for (final e in (raw is List ? raw : const []).whereType<Map>()) {
      final t = e['t'];
      final dur = e['dur'];
      final v = e['v'];
      if (t is! num || dur is! num || v is! String || !visemeNames.contains(v)) continue;
      events.add(VisemeEvent(t.round(), dur.round(), v));
    }
    events.sort((a, b) => a.t.compareTo(b.t));
    final d = m['duration_ms'];
    final b = m['pcm_bytes'];
    final id = m['segment'];
    var ms = d is num ? d.round() : 0;
    final bytes = b is num ? b.round() : 0;
    if (ms <= 0 && bytes > 0 && sampleRate > 0) ms = bytes * 500 ~/ sampleRate;
    if (ms <= 0 && events.isNotEmpty) ms = events.last.t + events.last.dur;
    if (ms <= 0) return null;
    return VisemeSegment(id is num ? id.round() : 0, startMs, ms, events);
  }
}

/// What to draw at an instant: the shape, the one before it and how far the change has gone (0 to 1), so the picture can fade between them.
class VisemeFrame {
  const VisemeFrame(this.v, this.prev, this.mix);
  final String v;
  final String prev;
  final double mix;

  static const rest = VisemeFrame('sil', 'sil', 1);
}

/// The segments of the reply being spoken, and the shape at a given time of the audio clock. A shape is held until the next one starts, the
/// mouth rests (`sil`) where no segment is, and `flush` forgets everything (an interrupt or a new answer).
class VisemeTimeline {
  VisemeTimeline({this.leadMs = 40, this.fadeMs = 50});

  /// The picture is drawn this far ahead of the sound, for the time a frame takes to reach the screen.
  final int leadMs;

  /// How long a change of shape takes.
  final int fadeMs;

  final List<VisemeSegment> _segments = [];
  String _cur = 'sil';
  String _prev = 'sil';
  int _since = 0;

  int get segmentCount => _segments.length;

  void add(VisemeSegment s) {
    _segments.removeWhere((x) => x.id == s.id && s.id != 0 && x.startMs == s.startMs);
    _segments.add(s);
    _segments.sort((a, b) => a.startMs.compareTo(b.startMs));
  }

  void flush() {
    _segments.clear();
    _cur = 'sil';
    _prev = 'sil';
    _since = 0;
  }

  /// The shape at `t` and the time its event began (`t` itself when nothing is being said).
  (String, int) _target(int t) {
    for (final s in _segments) {
      if (t < s.startMs || t >= s.endMs) continue;
      final local = t - s.startMs;
      var v = 'sil';
      var from = s.startMs;
      for (final e in s.events) {
        if (e.t > local) break;
        v = e.v;
        from = s.startMs + e.t;
      }
      return (v, from);
    }
    return ('sil', t);
  }

  /// The frame for `playedMs`, the milliseconds of audio heard so far.
  VisemeFrame at(int playedMs) {
    final t = playedMs + leadMs;
    _segments.removeWhere((s) => s.endMs < t - 3000);
    final (want, began) = _target(t);
    if (t < _since) _since = t;
    if (want != _cur) {
      _prev = _cur;
      _cur = want;
      _since = began;
    }
    final mix = fadeMs <= 0 ? 1.0 : ((t - _since) / fadeMs).clamp(0.0, 1.0);
    return VisemeFrame(_cur, _prev, mix);
  }
}

/// Slow movement of the whole picture while nothing is said, so that it does not look frozen. The photo has no eyes of its own to blink,
/// so it is a breath: a slight turn, a lift and a swell. With reduced motion there is none.
class IdleMotion {
  const IdleMotion({this.reduced = false});
  final bool reduced;

  /// (dx, dy, angle in radians, scale) as fractions of the picture, at `ms`.
  (double, double, double, double) at(int ms) {
    if (reduced) return (0, 0, 0, 1);
    final s = ms / 1000.0;
    final breath = math.sin(s * 2 * math.pi / 4.2);
    final sway = math.sin(s * 2 * math.pi / 7.3 + 1.1);
    return (sway * 0.002, breath * 0.002, sway * 0.004, 1 + breath * 0.004);
  }
}
