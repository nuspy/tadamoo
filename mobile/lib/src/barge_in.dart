import 'dart:math';
import 'dart:typed_data';

/// How far the avatar's voice is lowered while the person may be speaking over it (a fraction of the normal level). It is never muted at once:
/// the PC decides whether this was an interruption.
const duckLevel = 0.3;

/// Voice activity on the microphone while the avatar speaks. It sees only the loudness of 16 kHz mono 16-bit samples: speech starts after
/// `startMs` of loud audio and ends after `endMs` of quiet. The last `prerollMs` of audio before the start are handed over with it, so the
/// first word is not cut.
class BargeIn {
  BargeIn({this.threshold = 0.03, this.startMs = 150, this.endMs = 700, this.prerollMs = 300, this.sampleRate = 16000, required this.onStart, required this.onEnd});

  /// The RMS level (0 to 1 of full scale) above which a chunk counts as speech.
  final double threshold;
  final int startMs;
  final int endMs;
  final int prerollMs;
  final int sampleRate;

  /// Speech began: its level and the audio before it (to send first).
  final void Function(double level, List<Uint8List> preroll) onStart;

  /// Speech ended: its last level and how long it lasted, in milliseconds.
  final void Function(double level, int ms) onEnd;

  bool speaking = false;
  double _loudMs = 0;
  double _quietMs = 0;
  double _spokenMs = 0;
  double _peak = 0;
  final List<Uint8List> _recent = [];
  double _recentMs = 0;

  /// The loudness of a chunk, 0 to 1.
  static double level(Uint8List pcm16) {
    final n = pcm16.length ~/ 2;
    if (n == 0) return 0;
    final data = ByteData.sublistView(pcm16);
    var sum = 0.0;
    for (var i = 0; i < n; i++) {
      final s = data.getInt16(i * 2, Endian.little) / 32768.0;
      sum += s * s;
    }
    return sqrt(sum / n);
  }

  /// One chunk of the microphone.
  void feed(Uint8List pcm16) {
    final ms = _msOf(pcm16);
    final lv = level(pcm16);
    final loud = lv >= threshold;
    if (!speaking) {
      _recent.add(pcm16);
      _recentMs += ms;
      while (_recent.length > 1 && _recentMs - _msOf(_recent.first) >= prerollMs) {
        _recentMs -= _msOf(_recent.removeAt(0));
      }
      _loudMs = loud ? _loudMs + ms : 0;
      if (_loudMs >= startMs) {
        speaking = true;
        _quietMs = 0;
        _spokenMs = _loudMs;
        _peak = lv;
        final pre = List<Uint8List>.of(_recent);
        _recent.clear();
        _recentMs = 0;
        onStart(lv, pre);
      }
      return;
    }
    _spokenMs += ms;
    _peak = max(_peak, lv);
    _quietMs = loud ? 0 : _quietMs + ms;
    if (_quietMs >= endMs) {
      speaking = false;
      _loudMs = 0;
      onEnd(_peak, (_spokenMs - _quietMs).round());
    }
  }

  double _msOf(Uint8List c) => c.length * 1000 / (2 * sampleRate);

  /// Ends a speech that is still running (the avatar stopped speaking, or the person pressed the button).
  void finish() {
    if (speaking) {
      speaking = false;
      onEnd(_peak, _spokenMs.round());
    }
    _loudMs = 0;
    _recent.clear();
    _recentMs = 0;
  }
}
