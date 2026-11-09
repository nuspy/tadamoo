import 'package:basalt_mobile/src/audio_player_channel.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'avatar_fakes.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('app.basalt/audio');
  final calls = <MethodCall>[];

  void handler(Future<Object?> Function(MethodCall call)? h) => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, h);

  setUp(calls.clear);
  tearDown(() => handler(null));

  test('the voice goes to the Kotlin player in order, and the level is sent as it changes', () async {
    handler((call) async {
      calls.add(call);
      return null;
    });
    final fallback = FakePlayer();
    final p = ChannelPcmPlayer(fallback);
    final started = p.begin(24000);
    p.add(Uint8List.fromList([1, 2]));
    p.add(Uint8List.fromList([3, 4]));
    await started;
    await p.finish();
    p.setGain(0.3);
    await p.cancel();
    expect(calls.map((c) => c.method), ['playBegin', 'playGain', 'playAdd', 'playAdd', 'playGain', 'playCancel']);
    expect(calls.first.arguments, {'rate': 24000});
    expect(calls[2].arguments, Uint8List.fromList([1, 2]));
    expect(calls[4].arguments, {'gain': 0.3});
    expect(fallback.events, isEmpty);
  });

  test('without the channel the plugin player plays, with the same level', () async {
    handler(null);
    final fallback = FakePlayer();
    final p = ChannelPcmPlayer(fallback);
    p.setGain(0.5);
    await p.begin(16000);
    p.add(Uint8List.fromList([5, 6]));
    await p.finish();
    await p.cancel();
    expect(p.native, isFalse);
    expect(fallback.events, ['begin 16000', 'add', 'finish', 'cancel']);
    expect(fallback.gains, [0.5]);
    p.setGain(0.2);
    expect(fallback.gain, 0.2);
  });

  test('a track that cannot start falls back to the plugin player', () async {
    handler((call) async {
      calls.add(call);
      if (call.method == 'playBegin') throw PlatformException(code: 'audio', message: 'no track');
      return null;
    });
    final fallback = FakePlayer();
    final p = ChannelPcmPlayer(fallback);
    await p.begin(24000);
    p.add(Uint8List.fromList([7, 8]));
    await p.finish();
    expect(p.native, isFalse);
    expect(fallback.events, ['begin 24000', 'add', 'finish']);
    expect(calls.map((c) => c.method), ['playBegin']);
  });
}
