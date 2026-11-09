import 'dart:async';

import 'package:flutter/services.dart';

import 'avatar_devices.dart';
import 'playback_tracker.dart';

/// The PC's voice played by the Kotlin side as communication audio (CommPlayer.kt), so that it follows the route chosen in AudioManager
/// and a lowered level applies to what is already queued. When the channel is missing or the track cannot start, `fallback` plays instead.
/// Calls are chained so that the samples keep their order.
class ChannelPcmPlayer implements PcmPlayer {
  ChannelPcmPlayer(this._fallback, {MethodChannel? channel, int Function()? now, this.pollEvery = const Duration(milliseconds: 40)})
      : _channel = channel ?? const MethodChannel('app.basalt/audio'),
        _tracker = PlaybackTracker(now ?? _clock);

  static final Stopwatch _wall = Stopwatch()..start();
  static int _clock() => _wall.elapsedMilliseconds;

  final PcmPlayer _fallback;
  final MethodChannel _channel;
  final PlaybackTracker _tracker;

  /// How often the Kotlin player is asked how far it has played.
  final Duration pollEvery;
  Timer? _poll;
  int _rate = 0;
  Future<void> _chain = Future.value();
  bool _native = true;
  double _gain = 1;

  /// Whether the last reply went to the channel (false: the fallback has it).
  bool get native => _native;

  @override
  int get queuedMs => _tracker.queuedMs;

  @override
  int get playedMs => _tracker.playedMs;

  void _startPolling() {
    _poll ??= Timer.periodic(pollEvery, (_) async {
      if (!_native || _tracker.drained) {
        _poll?.cancel();
        _poll = null;
        return;
      }
      try {
        final ms = await _channel.invokeMethod<int>('playPosition');
        if (ms != null && ms >= 0) _tracker.report(ms);
      } on PlatformException {
        // the next poll tries again
      } on MissingPluginException {
        _native = false;
      }
    });
  }

  void _then(Future<void> Function() step) {
    _chain = _chain.then((_) => step()).catchError((Object _) {});
  }

  @override
  Future<void> begin(int sampleRate) {
    if (_rate != sampleRate) _tracker.reset();
    _rate = sampleRate;
    _then(() async {
      try {
        await _channel.invokeMethod<void>('playBegin', {'rate': sampleRate});
        await _channel.invokeMethod<void>('playGain', {'gain': _gain});
        _native = true;
      } on MissingPluginException {
        _native = false;
      } on PlatformException {
        _native = false;
      }
      if (!_native) {
        _fallback.setGain(_gain);
        await _fallback.begin(sampleRate);
      }
    });
    return _chain;
  }

  @override
  void add(Uint8List pcm16) {
    _tracker.added(pcm16.length, _rate);
    _startPolling();
    _then(() async {
      if (_native) {
        await _channel.invokeMethod<void>('playAdd', pcm16);
      } else {
        _fallback.add(pcm16);
      }
    });
  }

  @override
  Future<void> finish() {
    _then(() async {
      if (!_native) await _fallback.finish();
    });
    return _chain;
  }

  @override
  Future<void> cancel() {
    _tracker.reset();
    _poll?.cancel();
    _poll = null;
    _then(() async {
      if (_native) {
        try {
          await _channel.invokeMethod<void>('playCancel');
        } on MissingPluginException {
          // nothing was playing
        }
      } else {
        await _fallback.cancel();
      }
    });
    return _chain;
  }

  @override
  void setGain(double gain) {
    _gain = gain.clamp(0.0, 1.0);
    if (_native) {
      _channel.invokeMethod<void>('playGain', {'gain': _gain}).catchError((Object _) {});
    } else {
      _fallback.setGain(_gain);
    }
  }
}
