import 'package:basalt_mobile/src/avatar_controller.dart';
import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';

void main() {
  late AppSettings settings;
  late FakeGatewayApi api;
  late FakeDevices devices;
  late FakeConnector connector;
  late AvatarController c;

  Future<void> make({Map<String, Object> prefs = const {}, bool video = true, Duration videoWait = const Duration(seconds: 40)}) async {
    Texts.use('en');
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone', ...prefs});
    settings = await AppSettings.load();
    api = FakeGatewayApi(settings);
    devices = FakeDevices(video: video);
    connector = FakeConnector();
    final client = GatewayClient(connector: connector.call, wait: (d) async {});
    c = AvatarController(api: api, settings: settings, devices: devices, client: client, retry: const Duration(milliseconds: 20), videoWait: videoWait);
  }

  tearDown(() => c.dispose());

  Future<FakeChannel> up({Map<String, dynamic> ready = readyMessage}) async {
    await c.activate();
    await pumpEventQueue();
    connector.channel.say(ready);
    await pumpEventQueue();
    return connector.channel;
  }

  test('it finds the gateway through the server, connects to the one address of the PC with the phone\'s key, and lists the personalities', () async {
    await make();
    final ch = await up();
    expect(connector.urls.single.toString(), 'ws://100.64.1.2:7232/api/voice/gateway/ws');
    expect(connector.headers.single, {'Authorization': 'Bearer bsk_phone'});
    expect(ch.kinds, ['auth']);
    expect(ch.sent.first, {'type': 'auth', 'render_modes': ['video'], 'device': 'android'});
    expect(c.personas, [{'slug': 'bella', 'name': 'Bella'}, {'slug': 'otto', 'name': 'Otto'}]);
    expect(c.client.canTalk, isTrue);
  });

  test('the video is chosen with render_mode only when the PC offers it and is not already using it', () async {
    await make(prefs: {'flutter.avatarMode': 'video'});
    final ch = await up(ready: {...readyMessage, 'render_mode': 'none', 'render_modes': ['video']});
    expect(ch.last('render_mode'), {'type': 'render_mode', 'mode': 'video'});
  });

  test('a gateway that is not there yet is asked for again until it is, and nothing is connected meanwhile', () async {
    await make();
    api.info = {'available': false, 'reason': 'the voice service has not started', 'personas': []};
    await c.activate();
    expect(c.unavailable, 'the voice service has not started');
    await Future<void>.delayed(const Duration(milliseconds: 70));
    expect(api.calls, greaterThan(1));
    expect(connector.channels, isEmpty);
    api.info = {'available': true, 'path': '/api/voice/gateway/ws', 'personas': []};
    await Future<void>.delayed(const Duration(milliseconds: 70));
    await pumpEventQueue();
    expect(c.unavailable, isNull);
    expect(connector.channels.length, 1);
  });

  test('a server that does not answer is said as such, and the phone is not connected until it does', () async {
    await make();
    api.down = true;
    await c.activate();
    expect(c.unavailable, 'no answer');
    expect(connector.channels, isEmpty);
  });

  test('an unconfigured app says to set it up first', () async {
    await make(prefs: {'flutter.address': '', 'flutter.key': ''});
    await c.activate();
    expect(c.unavailable, Texts.t('avatar.setup'));
    expect(api.calls, 0);
  });

  test('with no avatar loaded it works by voice only; when the avatar starts it asks for the video by itself and shows it once it is live', () async {
    await make();
    final ch = await up();
    expect(devices.sessions, isEmpty);
    expect(ch.kinds, isNot(contains('video_start')));
    expect(c.client.canTalk, isTrue);                          // the voice works without the avatar

    ch.say({'type': 'state', 'voice': true, 'missing': [], 'avatar': true});
    await pumpEventQueue();
    expect(devices.sessions.length, 1);
    final offer = ch.last('video_start')!['sdp'] as String;
    expect(offer, contains('m=video 9 UDP/TLS/RTP/SAVPF 102 96 97 103'));          // H.264 first
    expect(c.liveVideo, isNull);

    ch.say({'type': 'video_answer', 'sdp': 'answer-sdp'});
    await pumpEventQueue();
    expect(devices.sessions.single.answer, 'answer-sdp');
    ch.say({'type': 'video_state', 'state': 'preparing', 'detail': 'preparing the avatar - 20%'});
    await pumpEventQueue();
    expect(c.liveVideo, isNull);
    ch.say({'type': 'video_state', 'state': 'live'});
    await pumpEventQueue();
    expect(c.liveVideo, same(devices.sessions.single));
  });

  test('with the avatar already loaded when the phone connects the video starts at once', () async {
    await make();
    final ch = await up(ready: {...readyMessage, 'avatar': true});
    expect(ch.kinds, contains('video_start'));
    expect(devices.sessions.length, 1);
  });

  test('video_unavailable closes the session and stays in voice; the video is tried again only when the avatar state changes', () async {
    await make();
    final ch = await up(ready: {...readyMessage, 'avatar': true});
    ch.say({'type': 'video_unavailable', 'reason': 'no_avatar'});
    await pumpEventQueue();
    expect(devices.sessions.single.closed, isTrue);
    expect(c.videoRequested, isFalse);
    expect(c.client.canTalk, isTrue);
    ch.say({'type': 'state', 'voice': true, 'missing': [], 'avatar': false});
    ch.say({'type': 'state', 'voice': true, 'missing': [], 'avatar': true});
    await pumpEventQueue();
    expect(devices.sessions.length, 2);
  });

  test('emergency_only leaves the tab in voice with one plain line and does not ask again', () async {
    await make();
    final ch = await up(ready: {...readyMessage, 'avatar': true});
    expect(ch.last('video_start')!['reason'], 'emergency');
    ch.say({'type': 'video_unavailable', 'reason': 'emergency_only'});
    await pumpEventQueue();
    expect(c.videoRequested, isFalse);
    expect(c.client.videoReason, 'emergency_only');
    expect(c.client.canTalk, isTrue);
    expect(devices.sessions.length, 1);
  });

  test('a video that never answers is given up after the wait and the tab stays in voice', () async {
    await make(videoWait: const Duration(milliseconds: 30));
    final ch = await up(ready: {...readyMessage, 'avatar': true});
    await Future<void>.delayed(const Duration(milliseconds: 80));
    await pumpEventQueue();
    expect(devices.sessions.single.closed, isTrue);
    expect(ch.kinds, contains('video_stop'));
    expect(c.client.canTalk, isTrue);
  });

  test('when the connection drops the video is closed, and when it is back with the avatar the video starts again', () async {
    await make();
    final ch = await up(ready: {...readyMessage, 'avatar': true});
    ch.say({'type': 'video_state', 'state': 'live'});
    await pumpEventQueue();
    expect(c.liveVideo, isNotNull);
    ch.drop();
    await pumpEventQueue();
    expect(devices.sessions.first.closed, isTrue);
    expect(c.liveVideo, isNull);
    expect(devices.fakePlayer.events, contains('cancel'));
    connector.channel.say({...readyMessage, 'avatar': true});
    await pumpEventQueue();
    expect(devices.sessions.length, 2);
    expect(connector.channel.kinds, contains('video_start'));
  });

  test('Wav2Lip chosen but not drawable on this phone yet: only the video is declared, a PC that says none keeps the voice, and the choice of the mode moves the video', () async {
    await make(prefs: {'flutter.avatarMode': 'wav2lip'});
    expect(c.mode, 'wav2lip');
    expect(c.modeAvailable('wav2lip'), isFalse);
    expect(c.modeAvailable('visemes'), isFalse);
    expect(c.modeAvailable('video'), isTrue);
    final ch = await up(ready: {...readyMessage, 'avatar': true, 'render_mode': 'none', 'render_modes': []});
    expect(ch.sent.first, {'type': 'auth', 'device': 'android', 'render_modes': ['video']});
    expect(devices.sessions, isEmpty);
    expect(ch.kinds, isNot(contains('video_start')));
    expect(ch.kinds, isNot(contains('render_mode')));
    expect(c.client.canTalk, isTrue);

    await c.setMode('visemes');
    expect(settings.avatarMode, 'visemes');
    await c.setMode('video');                                  // back to the video: it starts now, the avatar being loaded
    await pumpEventQueue();
    expect(settings.avatarMode, 'video');
    expect(devices.sessions.length, 1);
    await c.setMode('wav2lip');                                // and away from it: the PC is told to stop
    await pumpEventQueue();
    expect(devices.sessions.single.closed, isTrue);
    expect(ch.kinds, contains('video_stop'));
  });

  test('a build that cannot show the video stays in voice and says so', () async {
    await make(video: false);
    final ch = await up(ready: {...readyMessage, 'avatar': true});
    expect(c.modeAvailable('video'), isFalse);
    expect(devices.sessions, isEmpty);
    expect(ch.kinds, isNot(contains('video_start')));
  });

  test('hold to talk: the microphone starts, the turn starts, the voice goes up while held and the PC is told when it is released', () async {
    await make();
    final ch = await up();
    await c.holdStart();
    expect(devices.fakeMic.running, isTrue);
    expect(ch.last('start'), {'type': 'start', 'mode': 'hold', 'lang': 'en', 'sample_rate': 16000});
    devices.fakeMic.speak([1, 2, 3, 4]);
    await pumpEventQueue();
    expect(ch.sentBytes.single, [1, 2, 3, 4]);
    await c.holdEnd();
    expect(devices.fakeMic.running, isFalse);
    expect(ch.last('end'), isNotNull);
    ch.say({'type': 'transcript', 'text': 'hello'});
    ch.say({'type': 'reply', 'chunk': 'Hi there.'});
    ch.say({'type': 'audio_start', 'sample_rate': 24000});
    ch.bytes([7, 8]);
    ch.say({'type': 'audio_end'});
    ch.say({'type': 'done'});
    await pumpEventQueue();
    expect(devices.fakePlayer.events, ['begin 24000', 'add', 'finish']);
    expect(devices.fakePlayer.received, [7, 8]);
    expect(c.client.lines.map((l) => l.text), ['hello', 'Hi there.']);
  });

  test('the language of the turn is the app\'s, or the phone\'s speech locale when one is set', () async {
    await make(prefs: {'flutter.speechLocale': 'it-IT'});
    final ch = await up();
    await c.holdStart();
    expect(ch.last('start')!['lang'], 'it');
    await c.holdEnd();
  });

  test('a microphone the phone refuses is said and no turn is started', () async {
    await make();
    final ch = await up();
    devices.fakeMic.allowed = false;
    await c.holdStart();
    expect(c.micDenied, isTrue);
    expect(ch.kinds, ['auth']);
    expect(c.holding, isFalse);
  });

  test('the button released before the microphone was ready sends nothing', () async {
    await make();
    final ch = await up();
    final started = c.holdStart();
    await c.holdEnd();
    await started;
    expect(ch.kinds, isNot(contains('start')));
    expect(devices.fakeMic.running, isFalse);
  });

  test('toggling does the same as holding for a person who cannot hold', () async {
    await make();
    final ch = await up();
    await c.toggleTalk();
    expect(ch.last('start'), isNotNull);
    await c.toggleTalk();
    expect(ch.last('end'), isNotNull);
  });

  test('the brain and the personality chosen are kept, sent, and sent again to a PC that comes back', () async {
    await make();
    var ch = await up();
    await c.setBrain('harness');
    await c.setPersona('bella');
    expect((settings.avatarBrain, settings.avatarPersona), ('harness', 'bella'));
    expect(ch.last('brain'), {'type': 'brain', 'brain': 'harness'});
    expect(ch.last('persona'), {'type': 'persona', 'persona': 'bella'});
    ch.drop();
    await pumpEventQueue();
    ch = connector.channel;
    ch.say(readyMessage);
    await pumpEventQueue();
    expect(ch.last('brain'), {'type': 'brain', 'brain': 'harness'});
    expect(ch.last('persona'), {'type': 'persona', 'persona': 'bella'});
  });

  test('leaving the tab lets go of the connection, the microphone, the voice and the video', () async {
    await make();
    final ch = await up(ready: {...readyMessage, 'avatar': true});
    await c.holdStart();
    await c.deactivate();
    expect(ch.closed, isTrue);
    expect(devices.fakeMic.running, isFalse);
    expect(devices.sessions.single.closed, isTrue);
    expect(devices.fakePlayer.events, contains('cancel'));
    expect(c.active, isFalse);
    await c.activate();                                        // and coming back connects again
    await pumpEventQueue();
    expect(connector.channels.length, 2);
  });
}
