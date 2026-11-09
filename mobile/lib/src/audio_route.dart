import 'package:flutter/foundation.dart';

import 'i18n.dart';
import 'settings.dart';

/// Where the sound of a voice session goes: a Bluetooth headset (headphones and their microphone), the loudspeaker, or the earpiece
/// with the phone's own microphone.
enum AudioRoute {
  bluetooth,
  speaker,
  earpiece;

  static AudioRoute fromName(String name) => AudioRoute.values.firstWhere((r) => r.name == name, orElse: () => AudioRoute.speaker);
}

/// What the phone said about a change of route. `reason` is empty when it worked, else 'no_device', 'permission' or 'failed'.
class RouteOutcome {
  const RouteOutcome({this.reason = '', this.device = ''});
  final String reason;
  final String device;
  bool get ok => reason.isEmpty;
}

class AudioDeviceInfo {
  const AudioDeviceInfo(this.type, this.name);
  final String type;
  final String name;
}

/// What the phone's audio system offers, as read by the audio test.
class AudioReport {
  const AudioReport({
    this.supported = true,
    this.android = '',
    this.model = '',
    this.echoCancel = false,
    this.noiseSuppress = false,
    this.autoGain = false,
    this.echoCancelOnCapture = '',
    this.bluetoothPermission = '',
    this.bluetoothOn = '',
    this.devices = const [],
    this.current = '',
    this.mode = '',
  });

  /// False when the phone side of the test does not exist (not Android).
  final bool supported;
  final String android;
  final String model;
  final bool echoCancel;
  final bool noiseSuppress;
  final bool autoGain;

  /// 'yes', 'no' or 'unknown' (the microphone permission was not given).
  final String echoCancelOnCapture;
  final String bluetoothPermission;
  final String bluetoothOn;
  final List<AudioDeviceInfo> devices;
  final String current;
  final String mode;

  static String _yn(bool v) => v ? 'yes' : 'no';

  factory AudioReport.fromMap(Map<Object?, Object?> m) => AudioReport(
        android: '${m['android'] ?? ''}',
        model: '${m['model'] ?? ''}',
        echoCancel: m['aec'] == true,
        noiseSuppress: m['ns'] == true,
        autoGain: m['agc'] == true,
        echoCancelOnCapture: '${m['aecOnCapture'] ?? 'unknown'}',
        bluetoothPermission: '${m['btPermission'] ?? ''}',
        bluetoothOn: '${m['btOn'] ?? ''}',
        devices: [
          for (final d in (m['devices'] as List? ?? const []).whereType<Map>()) AudioDeviceInfo('${d['type']}', '${d['name'] ?? ''}'),
        ],
        current: '${m['current'] ?? ''}',
        mode: '${m['mode'] ?? ''}',
      );

  /// The report as plain English text, the same in every language, so that it can be pasted into a message.
  String toText() {
    if (!supported) return 'TaDaMoo audio test\nThe audio test is not available on this device.';
    final lines = <String>[
      'TaDaMoo audio test',
      'Android $android, $model',
      'AcousticEchoCanceler available: ${_yn(echoCancel)}',
      'NoiseSuppressor available: ${_yn(noiseSuppress)}',
      'AutomaticGainControl available: ${_yn(autoGain)}',
      'Echo canceller attaches to a VOICE_COMMUNICATION capture: $echoCancelOnCapture',
      'Bluetooth permission: $bluetoothPermission',
      'Bluetooth: $bluetoothOn',
      'Audio mode: $mode',
      'Current communication device: ${current.isEmpty ? 'none' : current}',
      'Communication devices:',
      if (devices.isEmpty) '  none' else for (final d in devices) '  ${d.type}${d.name.isEmpty ? '' : ' "${d.name}"'}',
      'Playback of the PC voice uses the media usage, not the communication usage: whether the route and the echo canceller apply to it is not proven.',
      echoCancel
          ? 'Full duplex on the speakerphone depends on the phone\'s echo canceller: it is present, listen for echo before relying on it.'
          : 'No echo canceller: keep the speakerphone half-duplex.',
    ];
    return lines.join('\n');
  }
}

/// The phone's side of the routing (a Kotlin platform channel in the real app, a stand-in in the tests).
abstract class AudioRouter {
  Future<bool> hasBluetoothPermission();

  /// Asks Android for the Bluetooth permission; true when it was given. Always true before Android 12.
  Future<bool> requestBluetoothPermission();

  /// Starts a voice session: communication mode and `route`.
  Future<RouteOutcome> startSession(AudioRoute route);

  /// Changes the route of the running session.
  Future<RouteOutcome> setRoute(AudioRoute route);

  /// Ends the session and puts the phone's audio back as it was.
  Future<void> stopSession();

  Future<AudioReport> probe();

  /// Called when a headset is connected or taken away during a session.
  VoidCallback? onDevicesChanged;
}

/// The state of the routing: the route the person chose, the one that is really active (a missing headset falls back to the speaker),
/// and whether the microphone may stay open while the avatar speaks.
class AudioRouteController extends ChangeNotifier {
  AudioRouteController({required this.router, required this.settings}) {
    router.onDevicesChanged = _devicesChanged;
  }

  final AudioRouter router;
  final AppSettings settings;

  bool sessionOn = false;
  bool testing = false;
  AudioRoute? _active;

  /// A key of the texts for the last thing that did not work, or null.
  String? problem;

  /// The audio test report, after it has run.
  AudioReport? report;

  AudioRoute get selected => AudioRoute.fromName(settings.audioRoute);

  /// The route in use; before a session starts it is the chosen one.
  AudioRoute get active => _active ?? selected;

  /// Bluetooth: full duplex. Speakerphone: only when the audio test found an echo canceller and the person allowed it. Earpiece: never.
  bool get fullDuplex {
    switch (active) {
      case AudioRoute.bluetooth:
        return true;
      case AudioRoute.speaker:
        return settings.fullDuplexSpeaker && settings.audioEchoCancel == true;
      case AudioRoute.earpiece:
        return false;
    }
  }

  Future<void> start() async {
    if (sessionOn) return;
    sessionOn = true;
    await _apply(selected, starting: true);
  }

  Future<void> stop() async {
    if (!sessionOn) return;
    sessionOn = false;
    _active = null;
    try {
      await router.stopSession();
    } catch (_) {}
    notifyListeners();
  }

  /// The person chose `route`. Bluetooth asks Android for its permission first. Returns whether the choice was taken.
  Future<bool> select(AudioRoute route) async {
    problem = null;
    if (route == AudioRoute.bluetooth && !await router.hasBluetoothPermission()) {
      if (!await router.requestBluetoothPermission()) {
        problem = 'route.err.permission';
        notifyListeners();
        return false;
      }
    }
    if (sessionOn) {
      final ok = await _apply(route, starting: false);
      if (!ok) return false;
    }
    await settings.setAudioRoute(route.name);
    _active = sessionOn ? route : null;
    notifyListeners();
    return true;
  }

  /// Applies `route` to the session. A route that cannot be had leaves the one in use, or on start falls back to the speaker.
  Future<bool> _apply(AudioRoute route, {required bool starting}) async {
    RouteOutcome out;
    try {
      out = starting ? await router.startSession(route) : await router.setRoute(route);
    } catch (_) {
      out = const RouteOutcome(reason: 'failed');
    }
    if (out.ok) {
      _active = route;
      problem = null;
      notifyListeners();
      return true;
    }
    problem = out.reason == 'no_device' ? 'route.err.nodevice' : (out.reason == 'permission' ? 'route.err.permission' : 'route.err.failed');
    if (starting) {
      _active = AudioRoute.speaker;
      try {
        await router.setRoute(AudioRoute.speaker);
      } catch (_) {}
    }
    notifyListeners();
    return false;
  }

  /// A headset came or went: take Bluetooth back when it was chosen and is there again, drop to the speaker when it is gone.
  Future<void> _devicesChanged() async {
    if (!sessionOn) return;
    if (selected == AudioRoute.bluetooth && _active != AudioRoute.bluetooth) {
      await _apply(AudioRoute.bluetooth, starting: false);
    } else if (_active == AudioRoute.bluetooth) {
      final out = await router.setRoute(AudioRoute.bluetooth).catchError((_) => const RouteOutcome(reason: 'failed'));
      if (!out.ok) {
        _active = AudioRoute.speaker;
        problem = 'route.err.lost';
        try {
          await router.setRoute(AudioRoute.speaker);
        } catch (_) {}
        notifyListeners();
      }
    }
  }

  /// The audio test: asks the phone what it has and remembers whether it has an echo canceller.
  Future<void> runTest() async {
    testing = true;
    notifyListeners();
    try {
      report = await router.probe();
      if (report!.supported) await settings.setAudioEchoCancel(report!.echoCancel);
    } catch (e) {
      report = const AudioReport(supported: false);
    }
    testing = false;
    notifyListeners();
  }

  /// The text of the last problem, in the app's language.
  String? get problemText => problem == null ? null : Texts.t(problem!);

  @override
  void dispose() {
    router.onDevicesChanged = null;
    super.dispose();
  }
}
