import 'package:flutter/services.dart';

import 'audio_route.dart';

/// The routing over the Kotlin channel `app.basalt/audio` (AudioRouteChannel.kt). Where there is no such channel (not Android) every call
/// answers as a phone that cannot route, and the app keeps playing on the default output.
class ChannelAudioRouter implements AudioRouter {
  ChannelAudioRouter({MethodChannel? channel}) : _channel = channel ?? const MethodChannel('app.basalt/audio') {
    _channel.setMethodCallHandler((call) async {
      if (call.method == 'devicesChanged') onDevicesChanged?.call();
      return null;
    });
  }

  final MethodChannel _channel;

  /// Whether the last route asked for was the loudspeaker (the WebRTC audio follows it).
  bool speakerWanted = true;

  @override
  VoidCallback? onDevicesChanged;

  Future<T?> _call<T>(String method, [Object? args]) async {
    try {
      return await _channel.invokeMethod<T>(method, args);
    } on MissingPluginException {
      return null;
    }
  }

  RouteOutcome _outcome(Map<Object?, Object?>? m) {
    if (m == null) return const RouteOutcome();
    return RouteOutcome(reason: '${m['reason'] ?? ''}', device: '${m['device'] ?? ''}');
  }

  @override
  Future<bool> hasBluetoothPermission() async => await _call<bool>('hasBluetoothPermission') ?? true;

  @override
  Future<bool> requestBluetoothPermission() async => await _call<bool>('requestBluetoothPermission') ?? false;

  @override
  Future<RouteOutcome> startSession(AudioRoute route) async {
    speakerWanted = route == AudioRoute.speaker;
    return _outcome(await _call<Map<Object?, Object?>>('startSession', {'route': route.name}));
  }

  @override
  Future<RouteOutcome> setRoute(AudioRoute route) async {
    speakerWanted = route == AudioRoute.speaker;
    return _outcome(await _call<Map<Object?, Object?>>('setRoute', {'route': route.name}));
  }

  @override
  Future<void> stopSession() async {
    await _call<void>('stopSession');
  }

  @override
  Future<AudioReport> probe() async {
    final m = await _call<Map<Object?, Object?>>('probe');
    return m == null ? const AudioReport(supported: false) : AudioReport.fromMap(m);
  }
}
