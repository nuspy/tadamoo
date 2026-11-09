import 'dart:typed_data';

import 'package:basalt_mobile/src/avatar_controller.dart';
import 'package:basalt_mobile/src/barge_in.dart';
import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';

/// 30 ms of 16 kHz mono 16-bit audio at the given amplitude.
Uint8List chunk(int amplitude) {
  final data = ByteData(960);
  for (var i = 0; i < 480; i++) {
    data.setInt16(i * 2, i.isEven ? amplitude : -amplitude, Endian.little);
  }
  return data.buffer.asUint8List();
}

final loud = chunk(8000);
final quiet = chunk(100);

void main() {
  group('the voice detector', () {
    late List<String> events;
    late List<List<Uint8List>> prerolls;
    late BargeIn vad;

    setUp(() {
      events = [];
      prerolls = [];
      vad = BargeIn(onStart: (level, pre) {
        events.add('start');
        prerolls.add(pre);
      }, onEnd: (level, ms) => events.add('end $ms'));
    });

    test('loudness is the RMS of the samples, 0 to 1', () {
      expect(BargeIn.level(chunk(0)), 0);
      expect(BargeIn.level(loud), closeTo(8000 / 32768, 0.001));
      expect(BargeIn.level(Uint8List(0)), 0);
    });

    test('speech starts after 150 ms of loud audio, not before, and hands over the audio before it', () {
      vad.feed(quiet);
      vad.feed(quiet);
      for (var i = 0; i < 4; i++) {
        vad.feed(loud);
      }
      expect(events, isEmpty);
      vad.feed(loud);
      expect(events, ['start']);
      expect(prerolls.single.length, 7);
      expect(vad.speaking, isTrue);
    });

    test('a short noise does not start speech', () {
      for (var i = 0; i < 3; i++) {
        vad.feed(loud);
      }
      vad.feed(quiet);
      for (var i = 0; i < 3; i++) {
        vad.feed(loud);
      }
      expect(events, isEmpty);
    });

    test('speech ends after 700 ms of quiet, with how long it lasted; a pause inside it does not end it', () {
      for (var i = 0; i < 5; i++) {
        vad.feed(loud);
      }
      for (var i = 0; i < 10; i++) {
        vad.feed(quiet);
      }
      vad.feed(loud);
      expect(events, ['start']);
      for (var i = 0; i < 24; i++) {
        vad.feed(quiet);
      }
      expect(events.last, startsWith('end '));
      expect(vad.speaking, isFalse);
      final ms = int.parse(events.last.substring(4));
      expect(ms, inInclusiveRange(450, 500));
    });

    test('finish ends a speech that is still running', () {
      for (var i = 0; i < 5; i++) {
        vad.feed(loud);
      }
      vad.finish();
      expect(events.last, startsWith('end '));
      vad.finish();
      expect(events.length, 2);
    });
  });

  group('speaking over the avatar', () {
    late AppSettings settings;
    late FakeGatewayApi api;
    late FakeDevices devices;
    late FakeConnector connector;
    late AvatarController c;

    Future<FakeChannel> up({Map<String, Object> prefs = const {'flutter.audioRoute': 'bluetooth'}, Duration duckHold = const Duration(seconds: 6)}) async {
      Texts.use('en');
      SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone', ...prefs});
      settings = await AppSettings.load();
      api = FakeGatewayApi(settings);
      devices = FakeDevices();
      connector = FakeConnector();
      c = AvatarController(api: api, settings: settings, devices: devices, client: GatewayClient(connector: connector.call, wait: (d) async {}), duckHold: duckHold);
      await c.activate();
      await pumpEventQueue();
      connector.channel.say(readyMessage);
      await pumpEventQueue();
      return connector.channel;
    }

    tearDown(() => c.dispose());

    Future<void> avatarSpeaks(FakeChannel ch) async {
      ch.say({'type': 'audio_start', 'sample_rate': 24000});
      await pumpEventQueue();
    }

    Future<void> feed(List<Uint8List> chunks) async {
      for (final x in chunks) {
        devices.fakeMic.speak(x);
      }
      await pumpEventQueue();
    }

    test('the session runs in the chosen route while the tab is shown, and ends with it', () async {
      await up();
      expect(devices.fakeRouter.calls, ['start bluetooth']);
      await c.deactivate();
      expect(devices.fakeRouter.calls.last, 'stop');
    });

    test('full duplex (Bluetooth): the microphone opens while the avatar speaks, and closes when it is done', () async {
      final ch = await up();
      expect(devices.fakeMic.starts, 0);
      await avatarSpeaks(ch);
      expect(devices.fakeMic.running, isTrue);
      expect(devices.fakeMic.starts, 1);
      ch.say({'type': 'done'});
      await pumpEventQueue();
      expect(devices.fakeMic.running, isFalse);
    });

    test('half duplex (speakerphone without a verified echo canceller): the microphone stays closed while the avatar speaks', () async {
      final ch = await up(prefs: {'flutter.audioRoute': 'speaker', 'flutter.fullDuplexSpeaker': true});
      await avatarSpeaks(ch);
      expect(devices.fakeMic.starts, 0);
      expect(c.route.fullDuplex, isFalse);
    });

    test('the speakerphone is full duplex only when the audio test found an echo canceller and it was allowed', () async {
      final ch = await up(prefs: {'flutter.audioRoute': 'speaker', 'flutter.fullDuplexSpeaker': true, 'flutter.audioEchoCancel': true});
      await avatarSpeaks(ch);
      expect(devices.fakeMic.running, isTrue);
    });

    test('speech over the avatar lowers its voice to 30 percent at once, tells the PC, and sends the utterance without stopping the reply', () async {
      final ch = await up();
      await avatarSpeaks(ch);
      await feed([quiet, quiet, loud, loud, loud, loud, loud]);
      expect(devices.fakePlayer.gain, duckLevel);
      expect(duckLevel, 0.3);
      expect(ch.kinds.sublist(ch.kinds.length - 2), ['user_speaking', 'start']);
      final speaking = ch.last('user_speaking')!;
      expect(speaking['state'], 'start');
      expect(speaking['level'], isA<num>());
      expect(speaking['ms'], 0);
      expect(ch.last('start')!['barge_in'], isTrue);
      expect(ch.sentBytes.length, 7, reason: 'the audio before the start goes first');
      expect(devices.fakePlayer.events, isNot(contains('cancel')), reason: 'nothing is stopped by the phone');
      expect(c.client.phase, Phase.speaking);
      await feed([loud, loud]);
      expect(ch.sentBytes.length, 9);
    });

    test('when the person stops, the PC gets the end of the speech with its length and then the end of the turn', () async {
      final ch = await up();
      await avatarSpeaks(ch);
      await feed([loud, loud, loud, loud, loud]);
      await feed(List.filled(24, quiet));
      final n = ch.kinds.length;
      expect(ch.kinds.sublist(n - 2), ['user_speaking', 'end']);
      final end = ch.sent.where((m) => m['type'] == 'user_speaking').last;
      expect(end['state'], 'end');
      expect(end['ms'], greaterThan(100));
      expect(c.client.barging, isFalse);
    });

    test('the PC says duck: the voice goes to the level it asks for; one means back to normal', () async {
      final ch = await up();
      await avatarSpeaks(ch);
      await feed([loud, loud, loud, loud, loud]);
      expect(devices.fakePlayer.gain, 0.3);
      ch.say({'type': 'duck', 'level': 0.15});
      await pumpEventQueue();
      expect(devices.fakePlayer.gain, 0.15);
      ch.say({'type': 'duck', 'level': 1});
      await pumpEventQueue();
      expect(devices.fakePlayer.gain, 1.0);
      ch.say({'type': 'duck', 'level': 7});
      await pumpEventQueue();
      expect(devices.fakePlayer.gain, 1.0, reason: 'a level above 1 is taken as 1');
    });

    test('with no word from the PC the voice comes back by itself after the hold time', () async {
      final ch = await up(duckHold: const Duration(milliseconds: 30));
      await avatarSpeaks(ch);
      await feed([loud, loud, loud, loud, loud]);
      await feed(List.filled(24, quiet));
      expect(devices.fakePlayer.gain, 0.3);
      await Future<void>.delayed(const Duration(milliseconds: 80));
      expect(devices.fakePlayer.gain, 1.0);
    });

    test('a new reply starts at the normal level', () async {
      final ch = await up();
      await avatarSpeaks(ch);
      await feed([loud, loud, loud, loud, loud]);
      await feed(List.filled(24, quiet));
      ch.say({'type': 'audio_start', 'sample_rate': 24000});
      await pumpEventQueue();
      expect(devices.fakePlayer.gain, 1.0);
    });

    test('the PC says interrupt: the playback is dropped', () async {
      final ch = await up();
      await avatarSpeaks(ch);
      await feed([loud, loud, loud, loud, loud]);
      ch.say({'type': 'interrupt'});
      await pumpEventQueue();
      expect(devices.fakePlayer.events, contains('cancel'));
    });

    test('resume_offer shows the continuation; taking it or turning it down tells the PC once', () async {
      final ch = await up();
      ch.say({'type': 'resume_offer', 'text': '  the rest of the recipe '});
      await pumpEventQueue();
      expect(c.client.resumeOffer, 'the rest of the recipe');
      c.answerResume(true);
      expect(ch.last('resume'), {'type': 'resume', 'accept': true});
      expect(c.client.resumeOffer, isEmpty);
      final n = ch.sent.length;
      c.answerResume(true);
      expect(ch.sent.length, n, reason: 'nothing to answer');
      ch.say({'type': 'resume_offer', 'text': 'again'});
      await pumpEventQueue();
      c.answerResume(false);
      expect(ch.last('resume'), {'type': 'resume', 'accept': false});
    });

    test('a new turn by the person drops the offer', () async {
      final ch = await up();
      ch.say({'type': 'resume_offer', 'text': 'the rest'});
      await pumpEventQueue();
      expect(c.sendText('another thing'), isTrue);
      expect(c.client.resumeOffer, isEmpty);
    });

    test('unknown messages and a duck without a level are ignored', () async {
      final ch = await up();
      await avatarSpeaks(ch);
      ch.say({'type': 'something_new', 'x': 1});
      ch.say({'type': 'duck'});
      ch.say({'type': 'duck', 'level': 'loud'});
      ch.say({'type': 'resume_offer'});
      await pumpEventQueue();
      expect(devices.fakePlayer.gain, 1.0);
      expect(c.client.status, GatewayStatus.ready);
      expect(c.client.phase, Phase.speaking);
      expect(c.client.resumeOffer, isEmpty);
    });

    test('the hold button during the avatar\'s voice takes the microphone over and stops the voice as before', () async {
      final ch = await up();
      await avatarSpeaks(ch);
      expect(devices.fakeMic.starts, 1);
      c.stopSpeaking();
      await c.holdStart();
      await pumpEventQueue();
      expect(devices.fakeMic.starts, 2);
      expect(ch.last('start')!.containsKey('barge_in'), isFalse);
      expect(c.client.phase, Phase.listening);
      await c.holdEnd();
    });

    test('a lost connection drops the microphone and puts the voice back to normal', () async {
      final ch = await up();
      await avatarSpeaks(ch);
      await feed([loud, loud, loud, loud, loud]);
      expect(devices.fakePlayer.gain, 0.3);
      ch.drop();
      await pumpEventQueue();
      expect(devices.fakeMic.running, isFalse);
      expect(devices.fakePlayer.gain, 1.0);
    });
  });
}
