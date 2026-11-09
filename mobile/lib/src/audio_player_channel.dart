import 'package:flutter/services.dart';

import 'avatar_devices.dart';

/// The PC's voice played by the Kotlin side as communication audio (CommPlayer.kt), so that it follows the route chosen in AudioManager
/// and a lowered level applies to what is already queued. When the channel is missing or the track cannot start, `fallback` plays instead.
/// Calls are chained so that the samples keep their order.
class ChannelPcmPlayer implements PcmPlayer {
  ChannelPcmPlayer(this._fallback, {MethodChannel? channel}) : _channel = channel ?? const MethodChannel('app.basalt/audio');

  final PcmPlayer _fallback;
  final MethodChannel _channel;
  Future<void> _chain = Future.value();
  bool _native = true;
  double _gain = 1;

  /// Whether the last reply went to the channel (false: the fallback has it).
  bool get native => _native;

  void _then(Future<void> Function() step) {
    _chain = _chain.then((_) => step()).catchError((Object _) {});
  }

  @override
  Future<void> begin(int sampleRate) {
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
