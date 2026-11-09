/// Where the voice being played has got to, on one clock for the whole life of the track: milliseconds of audio handed over (`queuedMs`)
/// and milliseconds heard (`playedMs`). A renderer puts a segment at the `queuedMs` of the moment its timeline arrives and shows what
/// belongs at `playedMs`, so the picture follows the sound that is really coming out and not the time the bytes arrived.
///
/// With reports of the player's own position (`report`) the estimate between two reports advances with the clock; without any it is the
/// time since the audio was handed over, less `latencyMs`.
class PlaybackTracker {
  PlaybackTracker(this._now, {this.latencyMs = 120});

  final int Function() _now;

  /// The delay assumed between handing over audio and hearing it, when the player reports nothing.
  final int latencyMs;

  int _queuedUs = 0;
  int _anchorPlayedMs = 0;
  int _anchorAt = 0;
  bool _reported = false;
  bool _anchored = false;

  int get queuedMs => _queuedUs ~/ 1000;

  /// Heard so far, never beyond what was handed over.
  int get playedMs {
    if (!_anchored) return 0;
    final dt = _now() - _anchorAt;
    final v = _anchorPlayedMs + (dt < 0 ? 0 : dt);
    final q = queuedMs;
    return v > q ? q : v;
  }

  /// Whether everything handed over has been heard.
  bool get drained => playedMs >= queuedMs;

  /// `bytes` of 16-bit mono samples at `rate` were handed to the player.
  void added(int bytes, int rate) {
    if (rate <= 0) return;
    if (!_anchored || (!_reported && drained)) {
      _anchorPlayedMs = queuedMs;
      _anchorAt = _now() + latencyMs;
      _anchored = true;
    }
    _queuedUs += (bytes * 500000) ~/ rate;
  }

  /// The player says it has played `ms` milliseconds since the track began.
  void report(int ms) {
    _anchorPlayedMs = ms;
    _anchorAt = _now();
    _anchored = true;
    _reported = true;
  }

  /// The track was dropped: both clocks start again.
  void reset() {
    _queuedUs = 0;
    _anchorPlayedMs = 0;
    _anchorAt = 0;
    _anchored = false;
    _reported = false;
  }
}
