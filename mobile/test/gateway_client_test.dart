import 'dart:typed_data';

import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:basalt_mobile/src/sdp.dart';
import 'package:flutter_test/flutter_test.dart';

import 'avatar_fakes.dart';

void main() {
  late FakeConnector connector;
  late GatewayClient client;
  late List<Duration> waits;
  final audio = <int>[];
  final events = <String>[];

  setUp(() {
    connector = FakeConnector();
    waits = [];
    audio.clear();
    events.clear();
    client = GatewayClient(connector: connector.call, wait: (d) async => waits.add(d))
      ..onAudioStart = ((rate) => events.add('start $rate'))
      ..onAudio = ((b) => audio.addAll(b))
      ..onAudioEnd = (() => events.add('end'))
      ..onInterrupt = (() => events.add('interrupt'))
      ..onAvatar = ((on) => events.add('avatar $on'));
  });

  tearDown(() => client.close());

  Future<FakeChannel> connect({Map<String, dynamic> ready = readyMessage}) async {
    client.open(Uri.parse('ws://pc:7232/api/voice/gateway/ws'), {'Authorization': 'Bearer bsk_key'});
    await pumpEventQueue();
    connector.channel.say(ready);
    await pumpEventQueue();
    return connector.channel;
  }

  test('it asks for the gateway with the phone\'s key, says who it is, and is ready when the PC says ready', () async {
    final ch = await connect();
    expect(connector.urls.single.toString(), 'ws://pc:7232/api/voice/gateway/ws');
    expect(connector.headers.single, {'Authorization': 'Bearer bsk_key'});
    expect(ch.sent.first, {'type': 'auth', 'render_modes': ['video'], 'device': 'android'});
    expect(client.status, GatewayStatus.ready);
    expect(client.canTalk, isTrue);
    expect(client.brain, 'direct');
    expect((client.renderMode, client.avatarName), ('video', 'ada'));
    expect(client.renderModes, ['video']);
  });

  test('render_mode messages and unknown fields are read tolerantly; a refused render_mode is not a problem', () async {
    final ch = await connect();
    ch.say({'type': 'render_mode', 'mode': 'none', 'render_modes': [], 'avatar_name': '', 'engine': 'x', 'resolution': 256, 'future_field': {'a': 1}});
    ch.say({'type': 'something_new', 'x': 1});
    ch.say({'type': 'error', 'code': 'render_mode_unavailable', 'message': 'no'});
    await pumpEventQueue();
    expect((client.renderMode, client.engine, client.resolution), ('none', 'x', '256'));
    expect(client.renderModes, isEmpty);
    expect(client.problem, isNull);
    ch.say({'type': 'render_mode', 'mode': 'video'});
    await pumpEventQueue();
    expect(client.renderMode, 'video');
    expect(client.renderModes, isEmpty);
  });

  test('a hold-to-talk turn: start, audio only while the button is held, end, then the words, the voice and done', () async {
    final ch = await connect();
    expect(client.holdStart('it'), isTrue);
    expect(ch.last('start'), {'type': 'start', 'mode': 'hold', 'lang': 'it', 'sample_rate': 16000});
    client.sendAudio(Uint8List.fromList([1, 2, 3, 4]));
    client.holdEnd();
    client.sendAudio(Uint8List.fromList([9, 9]));
    expect(ch.sentBytes.map((b) => b.length), [4]);
    expect(ch.kinds, ['auth', 'start', 'end']);
    expect(client.phase, Phase.thinking);

    ch.say({'type': 'transcript', 'text': 'che ore sono'});
    ch.say({'type': 'reply', 'chunk': 'Sono le '});
    ch.say({'type': 'reply', 'chunk': 'dieci.'});
    ch.say({'type': 'audio_start', 'sample_rate': 24000});
    ch.bytes([5, 6, 7, 8]);
    ch.say({'type': 'audio_end'});
    await pumpEventQueue();
    expect(client.phase, Phase.speaking);
    expect(client.lines.map((l) => '${l.mine ? 'me' : 'pc'}: ${l.text}'), ['me: che ore sono', 'pc: Sono le dieci.']);
    expect(events, ['start 24000', 'end']);
    expect(audio, [5, 6, 7, 8]);

    ch.say({'type': 'done'});
    await pumpEventQueue();
    expect(client.phase, Phase.idle);
  });

  test('the next reply opens a new line, and a silent turn adds none', () async {
    final ch = await connect();
    client.holdStart('en');
    client.holdEnd();
    ch.say({'type': 'transcript', 'text': ''});
    ch.say({'type': 'done'});
    await pumpEventQueue();
    expect(client.lines, isEmpty);
    expect(client.phase, Phase.idle);
    client.sendText('hello', 'en');
    ch.say({'type': 'reply', 'chunk': 'Hi.'});
    ch.say({'type': 'done'});
    await pumpEventQueue();
    client.sendText('again', 'en');
    ch.say({'type': 'reply', 'chunk': 'Yes.'});
    await pumpEventQueue();
    expect(client.lines.map((l) => l.text), ['hello', 'Hi.', 'again', 'Yes.']);
    expect(ch.last('text'), {'type': 'text', 'text': 'again', 'lang': 'en'});
  });

  test('the button during the reply stops it at once, the old turn\'s sound is dropped, and its done does not end the new turn', () async {
    final ch = await connect();
    client.sendText('tell me a story', 'en');
    ch.say({'type': 'audio_start', 'sample_rate': 24000});
    ch.bytes([1, 1]);
    await pumpEventQueue();
    expect(client.phase, Phase.speaking);

    expect(client.holdStart('en'), isTrue);
    expect(events.last, 'interrupt');
    expect(client.phase, Phase.listening);
    ch.bytes([2, 2]);
    ch.say({'type': 'audio_start', 'sample_rate': 24000});
    ch.say({'type': 'interrupt'});
    ch.say({'type': 'done'});
    await pumpEventQueue();
    expect(audio, [1, 1]);
    expect(client.phase, Phase.listening);

    client.holdEnd();
    ch.say({'type': 'transcript', 'text': 'something else'});
    ch.say({'type': 'done'});
    await pumpEventQueue();
    expect(client.phase, Phase.idle);
  });

  test('interrupt asks the PC to stop and stops the local sound', () async {
    final ch = await connect();
    client.sendText('long one', 'en');
    client.interrupt();
    expect(ch.last('interrupt'), isNotNull);
    expect(events.last, 'interrupt');
  });

  test('without the voice loaded on the PC the button does nothing and the reason is kept; it works once the PC says it is loaded', () async {
    final ch = await connect(ready: {...readyMessage, 'voice': false, 'missing': ['stt', 'tts']});
    expect(client.canTalk, isFalse);
    expect(client.missing, ['stt', 'tts']);
    expect(client.holdStart('en'), isFalse);
    expect(ch.kinds, ['auth']);
    ch.say({'type': 'error', 'code': 'not_loaded', 'message': 'the PC has not loaded: stt'});
    await pumpEventQueue();
    expect(client.problemCode, 'not_loaded');
    ch.say({'type': 'state', 'voice': true, 'missing': [], 'avatar': false});
    await pumpEventQueue();
    expect(client.canTalk, isTrue);
    expect(client.problem, isNull);
  });

  test('the PC loading the avatar is reported once, and an unloaded one the same', () async {
    final ch = await connect();
    ch.say({'type': 'state', 'voice': true, 'missing': [], 'avatar': true});
    ch.say({'type': 'state', 'voice': true, 'missing': [], 'avatar': true});
    ch.say({'type': 'state', 'voice': true, 'missing': [], 'avatar': false});
    await pumpEventQueue();
    expect(events, ['avatar true', 'avatar false']);
  });

  test('the choices go to the PC as messages and come back confirmed', () async {
    final ch = await connect();
    client.setBrain('harness');
    client.setPersona('bella');
    client.setRenderMode('video');
    expect(ch.sent.skip(1), [
      {'type': 'brain', 'brain': 'harness'},
      {'type': 'persona', 'persona': 'bella'},
      {'type': 'render_mode', 'mode': 'video'}
    ]);
    ch.say({'type': 'brain', 'brain': 'harness'});
    ch.say({'type': 'persona', 'persona': 'bella'});
    await pumpEventQueue();
    expect((client.brain, client.persona), ('harness', 'bella'));
  });

  test('the video: the offer goes out, the answer is handed over, the states are followed, and no_avatar leaves it in voice', () async {
    String? answer;
    String? reason;
    client
      ..onVideoAnswer = ((s) => answer = s)
      ..onVideoUnavailable = ((r) => reason = r);
    final ch = await connect();
    client.sendVideoStart('offer-sdp');
    expect(ch.last('video_start'), {'type': 'video_start', 'reason': 'emergency', 'sdp': 'offer-sdp', 'sdp_type': 'offer'});
    expect(client.video, VideoStage.negotiating);
    ch.say({'type': 'video_answer', 'sdp': 'answer-sdp'});
    ch.say({'type': 'video_state', 'state': 'preparing', 'detail': 'preparing the avatar - 40%'});
    await pumpEventQueue();
    expect((answer, client.video, client.videoDetail), ('answer-sdp', VideoStage.preparing, 'preparing the avatar - 40%'));
    ch.say({'type': 'video_state', 'state': 'live'});
    await pumpEventQueue();
    expect(client.video, VideoStage.live);
    client.sendVideoStop();
    expect(ch.last('video_stop'), isNotNull);
    expect(client.video, VideoStage.off);
    ch.say({'type': 'video_unavailable', 'reason': 'no_avatar'});
    await pumpEventQueue();
    expect((reason, client.videoReason, client.video), ('no_avatar', 'no_avatar', VideoStage.off));
  });

  test('a dropped connection is asked for again by itself after 1, 2, 4, 8 and then every 15 seconds; the wait starts over once it is ready', () async {
    var dropped = 0;
    client.onDropped = () => dropped++;
    await connect();
    for (var i = 0; i < 6; i++) {
      connector.channel.drop();
      await pumpEventQueue();
    }
    expect(waits.map((d) => d.inSeconds), [1, 2, 4, 8, 15, 15]);
    expect(dropped, 1);                                       // only a connection that had been ready counts as lost
    expect(connector.channels.length, 7);
    expect(client.status, GatewayStatus.connecting);

    connector.channel.say(readyMessage);
    await pumpEventQueue();
    expect(client.status, GatewayStatus.ready);
    connector.channel.drop();
    await pumpEventQueue();
    expect(waits.last, const Duration(seconds: 1));
    expect(dropped, 2);
  });

  test('a PC that cannot be reached is tried again as well', () async {
    connector.fail = 2;
    client.open(Uri.parse('ws://pc:7232/x'), {});
    await pumpEventQueue();
    expect(connector.urls.length, 3);
    expect(waits.map((d) => d.inSeconds), [1, 2]);
    expect(client.problem, isNotNull);
    connector.channel.say(readyMessage);
    await pumpEventQueue();
    expect(client.status, GatewayStatus.ready);
  });

  test('a turn that was running when the connection dropped is forgotten', () async {
    final ch = await connect();
    client.holdStart('en');
    expect(client.phase, Phase.listening);
    ch.drop();
    await pumpEventQueue();
    expect(client.phase, Phase.idle);
    expect(client.canTalk, isFalse);
    expect(client.avatar, isFalse);
  });

  test('close stops it for good: no new attempt', () async {
    final ch = await connect();
    client.close();
    await pumpEventQueue();
    expect(ch.closed, isTrue);
    expect(client.status, GatewayStatus.offline);
    expect(connector.channels.length, 1);
  });

  test('H.264 goes first in the video line of an offer, whatever its order was', () {
    final out = preferH264(sampleOffer);
    expect(out, contains('m=video 9 UDP/TLS/RTP/SAVPF 102 96 97 103\r\n'));
    expect(out, contains('m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'));
    expect(preferH264('v=0\nm=video 9 UDP/TLS/RTP/SAVPF 96\na=rtpmap:96 VP8/90000\n'), 'v=0\nm=video 9 UDP/TLS/RTP/SAVPF 96\na=rtpmap:96 VP8/90000\n');
  });
}
