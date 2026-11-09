import 'package:basalt_mobile/src/audio_route.dart';
import 'package:basalt_mobile/src/audio_route_channel.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';
import 'secret_fakes.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('app.basalt/audio');

  group('the platform channel', () {
    final calls = <MethodCall>[];
    var permission = true;
    var devicePresent = true;

    setUp(() {
      calls.clear();
      permission = true;
      devicePresent = true;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
        calls.add(call);
        switch (call.method) {
          case 'hasBluetoothPermission':
            return permission;
          case 'requestBluetoothPermission':
            return permission;
          case 'startSession':
          case 'setRoute':
            final bt = (call.arguments as Map)['route'] == 'bluetooth';
            return bt && !devicePresent ? {'reason': 'no_device'} : {'reason': '', 'device': bt ? 'Buds' : ''};
          case 'stopSession':
            return null;
          case 'probe':
            return {
              'android': '14 (API 34)',
              'model': 'Test phone',
              'aec': true,
              'ns': true,
              'agc': false,
              'aecOnCapture': 'yes',
              'btPermission': 'granted',
              'btOn': 'on',
              'mode': 'in communication',
              'current': 'builtin speaker',
              'devices': [
                {'type': 'builtin speaker', 'name': ''},
                {'type': 'bluetooth sco', 'name': 'Buds'}
              ],
            };
        }
        return null;
      });
    });

    tearDown(() => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));

    test('the calls carry the route by name and the answers become outcomes', () async {
      final r = ChannelAudioRouter();
      final started = await r.startSession(AudioRoute.speaker);
      expect(started.ok, isTrue);
      expect(calls.single.method, 'startSession');
      expect(calls.single.arguments, {'route': 'speaker'});
      expect((await r.setRoute(AudioRoute.bluetooth)).device, 'Buds');
      devicePresent = false;
      final lost = await r.setRoute(AudioRoute.bluetooth);
      expect(lost.ok, isFalse);
      expect(lost.reason, 'no_device');
      await r.stopSession();
      expect(calls.last.method, 'stopSession');
    });

    test('the Bluetooth permission is asked for only when the app asks', () async {
      final r = ChannelAudioRouter();
      permission = false;
      expect(await r.hasBluetoothPermission(), isFalse);
      expect(calls.map((c) => c.method), ['hasBluetoothPermission']);
      expect(await r.requestBluetoothPermission(), isFalse);
    });

    test('the probe becomes a report that can be pasted', () async {
      final report = await ChannelAudioRouter().probe();
      expect(report.echoCancel, isTrue);
      expect(report.autoGain, isFalse);
      final text = report.toText();
      expect(text, contains('AcousticEchoCanceler available: yes'));
      expect(text, contains('NoiseSuppressor available: yes'));
      expect(text, contains('AutomaticGainControl available: no'));
      expect(text, contains('Android 14 (API 34), Test phone'));
      expect(text, contains('bluetooth sco "Buds"'));
      expect(text, contains('depends on the phone\'s echo canceller'));
      expect(text, contains('media usage'));
    });

    test('a headset coming or going is passed on', () async {
      final r = ChannelAudioRouter();
      var n = 0;
      r.onDevicesChanged = () => n++;
      await TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.handlePlatformMessage(channel.name, const StandardMethodCodec().encodeMethodCall(const MethodCall('devicesChanged')), (_) {});
      expect(n, 1);
    });

    test('the loudspeaker flag follows the last route asked for', () async {
      final r = ChannelAudioRouter();
      await r.startSession(AudioRoute.speaker);
      expect(r.speakerWanted, isTrue);
      await r.setRoute(AudioRoute.earpiece);
      expect(r.speakerWanted, isFalse);
    });
  });

  test('without the channel (not Android) the phone cannot route and the report says so', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null);
    final r = ChannelAudioRouter();
    expect((await r.startSession(AudioRoute.speaker)).ok, isTrue);
    final report = await r.probe();
    expect(report.supported, isFalse);
    expect(report.toText(), contains('not available'));
  });

  group('the state machine', () {
    late AppSettings settings;
    late FakeRouter router;
    late AudioRouteController c;

    Future<void> make([Map<String, Object> prefs = const {}]) async {
      SharedPreferences.setMockInitialValues(prefs);
      settings = await AppSettings.load(secrets: FakeSecretStore());
      router = FakeRouter();
      c = AudioRouteController(router: router, settings: settings);
    }

    test('it starts as the speakerphone, half duplex, and a session starts in that route', () async {
      await make();
      expect(c.selected, AudioRoute.speaker);
      expect(c.fullDuplex, isFalse);
      await c.start();
      expect(router.calls, ['start speaker']);
      expect(c.active, AudioRoute.speaker);
      await c.stop();
      expect(router.calls.last, 'stop');
      expect(c.sessionOn, isFalse);
    });

    test('duplex: Bluetooth yes, earpiece never, speakerphone only when the test found an echo canceller and the person allowed it', () async {
      await make({'flutter.audioRoute': 'bluetooth'});
      expect(c.fullDuplex, isTrue);
      await settings.setAudioRoute('earpiece');
      await settings.setFullDuplexSpeaker(true);
      await settings.setAudioEchoCancel(true);
      expect(c.fullDuplex, isFalse);
      await settings.setAudioRoute('speaker');
      expect(c.fullDuplex, isTrue);
      await settings.setAudioEchoCancel(false);
      expect(c.fullDuplex, isFalse, reason: 'no echo canceller: the switch alone is not enough');
      await settings.setAudioEchoCancel(true);
      await settings.setFullDuplexSpeaker(false);
      expect(c.fullDuplex, isFalse, reason: 'the person did not allow it');
    });

    test('choosing a route during a session applies it and saves it; before a session it only saves', () async {
      await make();
      expect(await c.select(AudioRoute.earpiece), isTrue);
      expect(router.calls, isEmpty);
      expect(settings.audioRoute, 'earpiece');
      await c.start();
      expect(router.calls, ['start earpiece']);
      expect(await c.select(AudioRoute.speaker), isTrue);
      expect(router.calls.last, 'route speaker');
      expect(settings.audioRoute, 'speaker');
      expect(c.active, AudioRoute.speaker);
    });

    test('Bluetooth asks for the permission when it is missing; a refusal changes nothing', () async {
      await make();
      router.permission = false;
      router.grantWhenAsked = false;
      expect(await c.select(AudioRoute.bluetooth), isFalse);
      expect(router.calls, ['request']);
      expect(settings.audioRoute, 'speaker');
      expect(c.problem, 'route.err.permission');
      router.grantWhenAsked = true;
      expect(await c.select(AudioRoute.bluetooth), isTrue);
      expect(settings.audioRoute, 'bluetooth');
      expect(c.problem, isNull);
    });

    test('with the permission already given it is not asked again', () async {
      await make();
      await c.select(AudioRoute.bluetooth);
      expect(router.calls, isNot(contains('request')));
    });

    test('Bluetooth with no headset connected during a session keeps the old route and says why', () async {
      await make();
      await c.start();
      router.bluetoothPresent = false;
      expect(await c.select(AudioRoute.bluetooth), isFalse);
      expect(settings.audioRoute, 'speaker');
      expect(c.active, AudioRoute.speaker);
      expect(c.problem, 'route.err.nodevice');
    });

    test('a session that starts on Bluetooth with no headset falls back to the speakerphone, and takes Bluetooth when it appears', () async {
      await make({'flutter.audioRoute': 'bluetooth'});
      router.bluetoothPresent = false;
      await c.start();
      expect(c.selected, AudioRoute.bluetooth, reason: 'the choice stays');
      expect(c.active, AudioRoute.speaker);
      expect(router.calls, ['start bluetooth', 'route speaker']);
      expect(c.fullDuplex, isFalse);
      router.bluetoothPresent = true;
      router.onDevicesChanged!();
      await pumpEventQueue();
      expect(c.active, AudioRoute.bluetooth);
      expect(c.fullDuplex, isTrue);
      expect(c.problem, isNull);
    });

    test('the headset taken away during a session drops to the speakerphone', () async {
      await make({'flutter.audioRoute': 'bluetooth'});
      await c.start();
      expect(c.active, AudioRoute.bluetooth);
      router.bluetoothPresent = false;
      router.onDevicesChanged!();
      await pumpEventQueue();
      expect(c.active, AudioRoute.speaker);
      expect(c.problem, 'route.err.lost');
      expect(settings.audioRoute, 'bluetooth');
    });

    test('a headset event outside a session does nothing', () async {
      await make({'flutter.audioRoute': 'bluetooth'});
      router.onDevicesChanged!();
      await pumpEventQueue();
      expect(router.calls, isEmpty);
    });

    test('the audio test keeps its report and remembers whether the phone has an echo canceller', () async {
      await make();
      expect(settings.audioEchoCancel, isNull);
      await c.runTest();
      expect(c.report!.echoCancel, isTrue);
      expect(settings.audioEchoCancel, isTrue);
      expect(c.testing, isFalse);
      router.report = const AudioReport(echoCancel: false);
      await c.runTest();
      expect(settings.audioEchoCancel, isFalse);
    });
  });
}
