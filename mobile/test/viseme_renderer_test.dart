import 'dart:io';
import 'dart:ui' as ui;

import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/avatar_renderer.dart';
import 'package:basalt_mobile/src/viseme_timeline.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'avatar_fakes.dart';
import 'render_fakes.dart';

void main() {
  late FakePlayer player;
  late ui.Image poster;
  late ui.Image sil;
  late ui.Image aa;
  late ui.Image oo;

  Future<void> images(WidgetTester tester) async {
    await tester.runAsync(() async {
      poster = await tinyImage(40, 30, const Color(0xFFCCAA99));
      sil = await tinyImage(10, 8, const Color(0xFF111111));
      aa = await tinyImage(10, 8, const Color(0xFF222222));
      oo = await tinyImage(10, 8, const Color(0xFF333333));
    });
  }

  VisemePack pack({Map<String, ui.Image>? sprites}) => VisemePack(
        poster: poster,
        posterSize: const Size(40, 30),
        sprites: sprites ?? {'sil': sil, 'aa': aa, 'O': oo},
        inv: const [1, 0, 15, 0, 1, 18],
        frameSize: const Size(10, 8),
      );

  VisemeRenderer renderer(VisemePack p, {VisemeTimeline? timeline}) => VisemeRenderer(pack: p, player: player, timeline: timeline ?? VisemeTimeline(leadMs: 0, fadeMs: 0), now: () => 0);

  Map<String, dynamic> message(int segment, int duration, List<List<Object>> events) => {
        'type': 'visemes',
        'segment': segment,
        'duration_ms': duration,
        'pcm_bytes': duration * 48,
        'visemes': [for (final e in events) {'t': e[0], 'dur': e[1], 'v': e[2]}],
      };

  setUp(() => player = FakePlayer());

  testWidgets('the frame at a time of the voice shows the sprite of that mouth shape', (tester) async {
    await images(tester);
    final r = renderer(pack());
    r.audioStart(24000);
    r.visemes(message(1, 1000, [[0, 200, 'sil'], [200, 300, 'aa'], [500, 500, 'O']]));
    player.queuedMs = 1000;

    player.playedMs = 100;
    r.tick();
    expect(r.frame.v, 'sil');
    expect(r.scene!.layers.single.image, same(sil));
    player.playedMs = 300;
    r.tick();
    expect(r.frame.v, 'aa');
    expect(r.scene!.layers.single.image, same(aa));
    player.playedMs = 800;
    r.tick();
    expect(r.frame.v, 'O');
    expect(r.scene!.layers.single.image, same(oo));
    player.playedMs = 1200;
    r.tick();
    expect(r.frame.v, 'sil', reason: 'the sentence is over: the mouth rests');
  });

  testWidgets('a sentence is placed where the audio handed over so far ends, so two sentences follow each other on the clock', (tester) async {
    await images(tester);
    final r = renderer(pack());
    r.audioStart(24000);
    player.queuedMs = 0;
    r.visemes(message(1, 1000, [[0, 1000, 'aa']]));
    player.queuedMs = 1000;
    r.visemes(message(2, 1000, [[0, 1000, 'O']]));
    player.queuedMs = 2000;
    player.playedMs = 900;
    r.tick();
    expect(r.frame.v, 'aa');
    player.playedMs = 1100;
    r.tick();
    expect(r.frame.v, 'O');
  });

  testWidgets('an interrupt drops every sentence at once: the mouth rests and nothing that was queued is shown later', (tester) async {
    await images(tester);
    final r = renderer(pack());
    r.visemes(message(1, 1000, [[0, 1000, 'aa']]));
    r.visemes(message(2, 1000, [[0, 1000, 'O']]));
    player.queuedMs = 2000;
    player.playedMs = 300;
    r.tick();
    expect(r.frame.v, 'aa');
    r.flush();
    expect(r.frame.v, 'sil');
    player.playedMs = 400;
    r.tick();
    expect(r.frame.v, 'sil');
    player.playedMs = 1500;
    r.tick();
    expect(r.frame.v, 'sil');
  });

  testWidgets('a mouth shape with no sprite falls back to the resting mouth, and with none at all the photo is shown alone', (tester) async {
    await images(tester);
    final r = renderer(pack());
    r.visemes(message(1, 1000, [[0, 1000, 'E']]));
    player.queuedMs = 1000;
    player.playedMs = 100;
    r.tick();
    expect(r.frame.v, 'E');
    expect(r.scene!.layers.single.image, same(sil));
    final bare = renderer(pack(sprites: {}));
    bare.visemes(message(1, 1000, [[0, 1000, 'aa']]));
    bare.tick();
    expect(bare.scene!.layers, isEmpty);
    expect(bare.scene!.poster, same(poster));
  });

  testWidgets('a change of shape draws the old sprite under the new one while it fades', (tester) async {
    await images(tester);
    final r = renderer(pack(), timeline: VisemeTimeline(leadMs: 0, fadeMs: 100));
    r.visemes(message(1, 1000, [[0, 200, 'sil'], [200, 800, 'aa']]));
    player.queuedMs = 1000;
    player.playedMs = 100;
    r.tick();
    player.playedMs = 250;
    r.tick();
    final layers = r.scene!.layers;
    expect(layers.length, 2);
    expect(layers.first.image, same(sil));
    expect(layers.last.image, same(aa));
    expect(layers.last.opacity, closeTo(0.5, 1e-9));
    player.playedMs = 400;
    r.tick();
    expect(r.scene!.layers.single.image, same(aa));
  });

  testWidgets('a timeline that cannot be read changes nothing', (tester) async {
    await images(tester);
    final r = renderer(pack());
    r.visemes({'type': 'visemes', 'visemes': 'junk'});
    r.visemes({'type': 'visemes'});
    player.playedMs = 10;
    r.tick();
    expect(r.frame.v, 'sil');
  });

  testWidgets('the picture is drawn and kept moving by the screen\'s clock, without errors, and a viseme change repaints it', (tester) async {
    await images(tester);
    final r = renderer(pack());
    r.visemes(message(1, 4000, [[0, 1000, 'sil'], [1000, 1000, 'aa'], [2000, 2000, 'O']]));
    player.queuedMs = 4000;
    await tester.pumpWidget(MaterialApp(home: SizedBox(width: 200, height: 150, child: r.view())));
    for (var i = 1; i <= 8; i++) {
      player.playedMs = i * 400;
      await tester.pump(const Duration(milliseconds: 400));
    }
    expect(tester.takeException(), isNull);
    expect(r.frame.v, 'O');
    expect(find.byType(CustomPaint), findsWidgets);
  });

  testWidgets('with reduced motion the idle movement is off, the mouth still follows the voice', (tester) async {
    await images(tester);
    final r = renderer(pack());
    r.visemes(message(1, 4000, [[0, 4000, 'aa']]));
    player.queuedMs = 4000;
    await tester.pumpWidget(MaterialApp(home: MediaQuery(data: const MediaQueryData(disableAnimations: true), child: SizedBox(width: 200, height: 150, child: r.view()))));
    player.playedMs = 500;
    for (var i = 0; i < 5; i++) {
      await tester.pump(const Duration(milliseconds: 100));
    }
    expect(r.reducedMotion, isTrue);
    expect(r.motion, (0.0, 0.0, 0.0, 1.0));
    expect(r.frame.v, 'aa');
  });

  testWidgets('without reduced motion the picture moves a little while nothing is said', (tester) async {
    await images(tester);
    var ms = 0;
    final r = VisemeRenderer(pack: pack(), player: player, now: () => ms);
    await tester.pumpWidget(MaterialApp(home: SizedBox(width: 200, height: 150, child: r.view())));
    ms = 1500;
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pump(const Duration(milliseconds: 100));
    expect(r.reducedMotion, isFalse);
    expect(r.motion, isNot((0.0, 0.0, 0.0, 1.0)));
  });

  group('the files become a pack', () {
    late Directory dir;
    late AvatarAssetStore store;
    late FakeAssetSource source;

    setUp(() {
      dir = Directory.systemTemp.createTempSync('basalt_pack');
      source = FakeAssetSource();
      store = AvatarAssetStore(root: dir, source: source);
    });

    tearDown(() {
      store.dispose();
      dir.deleteSync(recursive: true);
    });

    testWidgets('every sprite, the photo and the frame are read; the size of the photo is the PC\'s', (tester) async {
      await images(tester);
      await tester.runAsync(() async {
        source.serve(FakeAvatarFiles('ada'));
        await store.sync('ada');
        final p = await VisemePack.load(store, 'ada', decoder: (bytes) async => aa);
        expect(p, isNotNull);
        expect(p!.sprites.length, 15);
        expect(p.posterSize, const Size(40, 30));
        expect(p.inv, [1, 0, 0, 0, 1, 0]);
        expect(p.frameSize, const Size(10, 8));
      });
    });

    testWidgets('a sprite that does not decode is left out and the resting one stands in; without the resting one there is no pack', (tester) async {
      await images(tester);
      await tester.runAsync(() async {
        source.serve(FakeAvatarFiles('ada'));
        await store.sync('ada');
        final p = await VisemePack.load(store, 'ada', decoder: (bytes) async {
          if (String.fromCharCodes(bytes) == 'png-aa') throw const FormatException('broken');
          return sil;
        });
        expect(p!.sprites.containsKey('aa'), isFalse);
        expect(p.spriteFor('aa'), same(sil));
        final none = await VisemePack.load(store, 'ada', decoder: (bytes) async {
          if (String.fromCharCodes(bytes) == 'png-sil') throw const FormatException('broken');
          return sil;
        });
        expect(none, isNull);
      });
    });

    testWidgets('a frame that is not usable, or files that are not there, give no pack', (tester) async {
      await images(tester);
      await tester.runAsync(() async {
        expect(await VisemePack.load(store, 'nobody', decoder: (bytes) async => aa), isNull);
        source.serve(FakeAvatarFiles('ada'));
        await store.sync('ada');
        File(store.pathOf('ada', 'visemes/frame.json')).writeAsStringSync('{"size": [1], "inv": []}');
        expect(await VisemePack.load(store, 'ada', decoder: (bytes) async => aa), isNull);
      });
    });
  });
}
