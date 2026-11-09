import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/avatar_controller.dart';
import 'package:basalt_mobile/src/avatar_hub.dart';
import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';
import 'render_fakes.dart';
import 'secret_fakes.dart';

void main() {
  late Directory dir;
  late AppSettings settings;
  late FakeAssetSource source;
  late AvatarAssetStore store;
  late FakeWav2LipBackend backend;
  late EngineHub hub;
  late FakeDevices devices;
  late FakeConnector connector;
  late AvatarController c;
  late List<FakeRenderer> built;
  late List<String> asked;
  String? failBuild;

  /// `cached`: the avatar's files are already on the phone when the app starts.
  Future<void> make({Map<String, Object> prefs = const {}, bool cached = false, bool useFactory = true}) async {
    Texts.use('en');
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone', ...prefs});
    settings = await AppSettings.load(secrets: FakeSecretStore());
    dir = Directory.systemTemp.createTempSync('basalt_render');
    source = FakeAssetSource()..modelBytes = Uint8List.fromList(utf8.encode('the model'));
    store = AvatarAssetStore(root: dir, source: source, modelSha: sha(utf8.encode('the model')), modelBytes: 9);
    backend = FakeWav2LipBackend();
    hub = EngineHub(settings: settings, store: store, backend: backend);
    source.serve(FakeAvatarFiles('ada'));
    if (cached) {
      await hub.fetchAssets('ada');
      source.asked.clear();
    }
    devices = FakeDevices();
    connector = FakeConnector();
    built = [];
    asked = [];
    c = AvatarController(
      api: FakeGatewayApi(settings),
      settings: settings,
      devices: devices,
      client: GatewayClient(connector: connector.call, wait: (d) async {}),
      hub: hub,
      rendererFactory: useFactory
          ? (mode, name, why) async {
              asked.add('$mode $name');
              if (failBuild != null) {
                why(failBuild!);
                return null;
              }
              final r = FakeRenderer(mode);
              built.add(r);
              return r;
            }
          : null,
    );
  }

  setUp(() => failBuild = null);

  tearDown(() {
    c.dispose();
    hub.dispose();
    store.dispose();
    dir.deleteSync(recursive: true);
  });

  /// Waits for what real file and zip work finishes outside the test's microtasks.
  Future<void> until(bool Function() done) async {
    for (var i = 0; i < 100 && !done(); i++) {
      await Future<void>.delayed(const Duration(milliseconds: 10));
    }
    await pumpEventQueue();
  }

  Future<FakeChannel> up(Map<String, dynamic> ready) async {
    await c.activate();
    await pumpEventQueue();
    connector.channel.say(ready);
    await pumpEventQueue();
    return connector.channel;
  }

  Map<String, dynamic> ready(String mode, {List<String>? modes, bool avatar = false}) =>
      {...readyMessage, 'avatar': avatar, 'render_mode': mode, 'render_modes': modes ?? [mode, 'video'], 'avatar_name': 'ada', 'engine': mode, 'resolution': 240};

  Map<String, dynamic> authOf(FakeChannel ch) => ch.sent.first;

  group('what is declared and when it is declared again', () {
    test('the first time nothing is on the phone: only the video is declared; the files are fetched in the background and the PC is told again', () async {
      await make();
      final ch = await up(ready('none', modes: []));
      expect(authOf(ch), {'type': 'auth', 'device': 'android', 'render_modes': ['video']});
      await until(() => connector.channels.length == 2);
      expect(source.asked, ['/api/voice/avatar/ada/manifest.json', '/api/voice/avatar/ada/assets.zip']);
      expect(settings.avatarName, 'ada');
      expect(connector.channels.length, 2, reason: 'the PC can only be told again by opening the connection again');
      expect(connector.channels.first.closed, isTrue);
      expect(authOf(connector.channel), {'type': 'auth', 'device': 'android', 'render_modes': ['visemes', 'video'], 'engine': 'visemes', 'resolution': 240});
    });

    test('with the files already on the phone the visemes are declared at once, and nothing is fetched again', () async {
      await make(cached: true);
      final ch = await up(ready('visemes'));
      expect(authOf(ch)['render_modes'], ['visemes', 'video']);
      await pumpEventQueue();
      expect(source.asked, isEmpty);
      expect(connector.channels.length, 1);
    });

    test('the files are not fetched when the person chose the PC\'s video, or switched the avatar off', () async {
      await make(prefs: {'flutter.avatarMode': 'video'});
      await up(ready('none', modes: []));
      await pumpEventQueue();
      expect(source.asked, isEmpty);
      c.dispose();
      hub.dispose();
      store.dispose();
      dir.deleteSync(recursive: true);
      await make(prefs: {'flutter.avatarEnabled': false});
      await up(ready('none', modes: []));
      await pumpEventQueue();
      expect(source.asked, isEmpty);
    });

    test('the person\'s engine and height go in the first message', () async {
      await make(cached: true, prefs: {'flutter.avatarResolution': 144});
      final ch = await up(ready('visemes'));
      expect(authOf(ch)['engine'], 'visemes');
      expect(authOf(ch)['resolution'], 144);
    });

    test('a change of the choice that changes what is declared opens the connection again with the new declaration', () async {
      await make(cached: true);
      await hub.downloadModel();
      await hub.refresh();
      await hub.runTest();
      await up(ready('visemes'));
      expect(connector.channels.length, 1);
      await settings.setAvatarMode('wav2lip');
      await pumpEventQueue();
      expect(connector.channels.length, 2);
      expect(authOf(connector.channel)['render_modes'], ['wav2lip', 'visemes', 'video']);
      expect(authOf(connector.channel)['engine'], 'wav2lip_gan96');
    });

    test('a setting that does not change what is declared leaves the connection alone', () async {
      await make(cached: true);
      await up(ready('visemes'));
      await settings.setAvatarQuality(settings.avatarQuality);
      await settings.setAvatarBrain('harness');
      await pumpEventQueue();
      expect(connector.channels.length, 1);
    });

    test('a model that passes its test while the tab is open makes Wav2Lip declarable at once', () async {
      await make(cached: true, prefs: {'flutter.avatarMode': 'wav2lip'});
      await up(ready('visemes'));
      expect(authOf(connector.channel)['render_modes'], ['visemes', 'video']);
      await hub.downloadModel();
      await hub.runTest();
      await pumpEventQueue();
      expect(connector.channels.length, 2);
      expect(authOf(connector.channel)['render_modes'], ['wav2lip', 'visemes', 'video']);
    });
  });

  group('the avatar drawn on the phone', () {
    test('the renderer of the mode the PC chose is built for the avatar it named, and shown', () async {
      await make(cached: true);
      await up(ready('visemes'));
      await until(() => built.isNotEmpty);
      expect(asked, ['visemes ada']);
      expect(c.renderer, same(built.single));
      expect(c.devices.videoSupported, isTrue);
      expect(devices.sessions, isEmpty, reason: 'no video while the phone draws the avatar');
      expect(connector.channel.kinds, isNot(contains('video_start')));
    });

    test('the timeline, the voice and its end are handed to it in order, and an interrupt or a replaced answer flushes it', () async {
      await make(cached: true);
      final ch = await up(ready('visemes'));
      await until(() => built.isNotEmpty);
      final r = built.single;
      ch.say({'type': 'transcript', 'text': 'ciao'});
      ch.say({'type': 'audio_start', 'sample_rate': 24000, 'segment': 1});
      ch.say({'type': 'visemes', 'segment': 1, 'duration_ms': 500, 'pcm_bytes': 24000, 'visemes': []});
      ch.bytes(List.filled(960, 1));
      ch.say({'type': 'audio_end'});
      await pumpEventQueue();
      expect(r.calls, ['start 24000', 'audio 960', 'end']);
      expect(r.timelines.single['segment'], 1);
      expect(devices.fakePlayer.events, contains('add'));
      ch.say({'type': 'reply_reset', 'text': 'x'});
      await pumpEventQueue();
      expect(r.calls.last, 'flush');
      r.calls.clear();
      ch.say({'type': 'interrupt'});
      await pumpEventQueue();
      expect(r.calls, ['flush']);
      expect(devices.fakePlayer.events, contains('cancel'));
    });

    test('a lost connection drops it, and the next ready builds it again', () async {
      await make(cached: true);
      final ch = await up(ready('visemes'));
      await until(() => built.isNotEmpty);
      ch.drop();
      await pumpEventQueue();
      expect(c.renderer, isNull);
      expect(built.single.disposed, isTrue);
      connector.channel.say(ready('visemes'));
      await until(() => built.length == 2);
      expect(c.renderer, same(built.last));
    });

    test('leaving the tab drops it', () async {
      await make(cached: true);
      await up(ready('visemes'));
      await until(() => built.isNotEmpty);
      await c.deactivate();
      expect(c.renderer, isNull);
      expect(built.single.disposed, isTrue);
    });

    test('a renderer that cannot go on tells the PC which mode and why, is left out, and the mode the PC names next is built', () async {
      await make(cached: true, prefs: {'flutter.avatarMode': 'wav2lip'});
      await hub.downloadModel();
      await hub.refresh();
      await hub.runTest();
      final ch = await up(ready('wav2lip', modes: ['wav2lip', 'visemes', 'video']));
      await until(() => built.isNotEmpty);
      expect(asked, ['wav2lip ada']);
      built.single.fail('too_slow');
      await pumpEventQueue();
      expect(ch.last('render_failed'), {'type': 'render_failed', 'mode': 'wav2lip', 'why': 'too_slow'});
      expect(connector.channels.length, 1, reason: 'the PC already leaves the mode out: no new connection');
      expect(c.renderer, isNull);
      expect(built.single.disposed, isTrue);
      expect(c.rendererNote, contains('wav2lip'));
      expect(c.rendererNote, contains('too_slow'));
      expect(hub.wav2lipReady, isFalse, reason: 'it is not declared again until it passes the test again');
      ch.say({'type': 'render_mode', 'mode': 'visemes', 'previous': 'wav2lip', 'why': 'too_slow', 'render_modes': ['visemes', 'video'], 'avatar_name': 'ada'});
      await until(() => built.length == 2);
      expect(asked.last, 'visemes ada');
      expect(c.renderer, same(built.last));
      expect(c.rendererNote, isEmpty);
    });

    test('a renderer that cannot be built is a failure of its mode with its reason', () async {
      await make(cached: true);
      failBuild = 'assets_invalid';
      final ch = await up(ready('visemes'));
      await until(() => ch.last('render_failed') != null);
      expect(ch.last('render_failed'), {'type': 'render_failed', 'mode': 'visemes', 'why': 'assets_invalid'});
      expect(c.renderer, isNull);
      expect(hub.visemesReady, isFalse);
    });

    test('the built-in renderers fail with a reason when what they need is not there', () async {
      await make(cached: true, useFactory: false);
      final ch = await up(ready('visemes'));
      await until(() => ch.last('render_failed') != null);
      expect(ch.last('render_failed')!['why'], 'assets_invalid', reason: 'the files are not pictures');
      c.dispose();
      hub.dispose();
      store.dispose();
      dir.deleteSync(recursive: true);
      await make(cached: true, useFactory: false, prefs: {'flutter.avatarMode': 'wav2lip'});
      final ch2 = await up(ready('wav2lip'));
      await until(() => ch2.last('render_failed') != null);
      expect(ch2.last('render_failed'), {'type': 'render_failed', 'mode': 'wav2lip', 'why': 'not_ready'});
    });

    test('when the PC says the video is the mode, the phone draws nothing itself and asks for the emergency video', () async {
      await make(cached: true);
      final ch = await up(ready('video', modes: ['video'], avatar: true));
      await pumpEventQueue();
      expect(c.renderer, isNull);
      expect(asked, isEmpty);
      expect(ch.last('video_start')!['reason'], 'emergency');
    });

    test('a mode the phone does not know is not drawn', () async {
      await make(cached: true);
      await up(ready('hologram'));
      await pumpEventQueue();
      expect(c.renderer, isNull);
      expect(asked, isEmpty);
    });

    test('turning the avatar off drops the renderer and tells the PC; turning it on builds it again', () async {
      await make(cached: true);
      await up(ready('visemes'));
      await until(() => built.isNotEmpty);
      await c.setAvatarEnabled(false);
      await pumpEventQueue();
      expect(c.renderer, isNull);
      expect(built.single.disposed, isTrue);
      expect(connector.channels.first.kinds, contains('video_stop'));
      expect(connector.channels.length, 2, reason: 'voice only is declared by declaring the video alone');
      expect(authOf(connector.channel)['render_modes'], ['video']);
    });

    test('a change of avatar (a new personality) builds the renderer for its folder', () async {
      await make(cached: true);
      final ch = await up(ready('visemes'));
      await until(() => built.isNotEmpty);
      final bob = FakeAvatarFiles('bob');
      source.serve(bob);
      ch.say({'type': 'render_mode', 'mode': 'visemes', 'render_modes': ['visemes', 'video'], 'avatar_name': 'bob'});
      await until(() => built.length == 2);
      expect(asked, ['visemes ada', 'visemes bob']);
      expect(settings.avatarName, 'bob');
      expect(built.first.disposed, isTrue);
      await until(() => store.stageOf('bob') == AssetStage.ready);
    });
  });
}
