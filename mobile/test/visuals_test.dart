import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/visuals_box.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

class Recorder extends Api {
  Recorder(super.settings);
  final List<String> calls = [];
  @override
  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body]) async {
    calls.add('POST $path $body');
    return {
      'session': {'id': 's1', 'images': body?['images'] ?? '', 'charts': body?['charts'] ?? ''}
    };
  }
}

Future<AppSettings> settings() async {
  SharedPreferences.setMockInitialValues({});
  return AppSettings.load(secrets: FakeSecretStore());
}

void main() {
  setUp(() => Texts.use('en'));

  test('pictures and charts go in a new conversation only when they are on', () {
    expect(NewChat().body().containsKey('images'), isFalse);
    expect(NewChat().body().containsKey('charts'), isFalse);
    final b = NewChat(images: 'high', charts: 'normal').body();
    expect(b['images'], 'high');
    expect(b['charts'], 'normal');
    expect(NewChat().copy(images: 'low').images, 'low');
  });

  test('the phone keeps its own choice, and a new conversation starts from it', () async {
    final s = await settings();
    expect([s.images, s.charts], ['', '']);
    await s.setVisuals('very_high', 'low');
    expect([s.images, s.charts], ['very_high', 'low']);
  });

  test('an open conversation changes them on the server and keeps what the server says', () async {
    final s = await settings();
    final api = Recorder(s);
    final c = ChatController(api, s)..sid = 's1';
    await c.setVisuals('high', '');
    expect(api.calls.single, 'POST /api/agent/sessions/s1/visuals {images: high, charts: }');
    expect(c.info!.images, 'high');
    expect(c.info!.charts, '');
  });

  group('the two switches', () {
    Widget host(Widget child) => MaterialApp(home: Scaffold(body: SingleChildScrollView(child: child)));

    testWidgets('a level can be chosen only while its switch is on, and turning it on starts at normal', (t) async {
      final seen = <String>[];
      await t.pumpWidget(host(VisualsBox(images: '', charts: 'high', onChanged: (i, c) async {
        seen.add('$i|$c');
        return null;
      })));
      expect(tester(t, 'vis-img').value, isFalse);
      expect(tester(t, 'vis-chart').value, isTrue);
      await t.tap(find.byKey(const ValueKey('vis-img')));
      await t.pump();
      expect(seen, ['normal|high']);
      await t.tap(find.byKey(const ValueKey('vis-chart')));
      await t.pump();
      expect(seen.last, 'normal|');
    });

    testWidgets('the server\'s complaint is shown and the box keeps what it had', (t) async {
      await t.pumpWidget(host(VisualsBox(images: '', charts: '', onChanged: (i, c) async => 'refused')));
      await t.tap(find.byKey(const ValueKey('vis-img')));
      await t.pumpAndSettle();
      expect(find.text('refused'), findsOneWidget);
      expect(tester(t, 'vis-img').value, isFalse);
    });

    testWidgets('fits a narrow phone with large text and every control is at least 48 high', (t) async {
      t.view.physicalSize = const Size(360 * 3, 640 * 3);
      t.view.devicePixelRatio = 3;
      addTearDown(t.view.reset);
      await t.pumpWidget(MediaQuery(
        data: const MediaQueryData(size: Size(360, 640), textScaler: TextScaler.linear(1.6)),
        child: host(VisualsBox(images: 'normal', charts: 'normal', onChanged: (i, c) async => null)),
      ));
      expect(t.takeException(), isNull);
      for (final key in const ['vis-img', 'vis-chart']) {
        expect(t.getSize(find.byKey(ValueKey(key))).height, greaterThanOrEqualTo(48));
      }
    });
  });
}

CheckboxListTile tester(WidgetTester t, String key) => t.widget<CheckboxListTile>(find.byKey(ValueKey(key)));
