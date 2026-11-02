import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_options.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/server_prefs.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// The server's switches, kept in memory; `refuse` answers every change as a phone's key is answered on a PC that keeps them for itself.
class FakeServer extends Api {
  FakeServer(super.settings);
  String intake = 'off';
  bool optimize = false;
  bool persistence = true;
  int side = 0;
  bool refuse = false;
  bool down = false;
  final List<String> calls = [];

  @override
  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query}) async {
    calls.add('GET $path');
    if (down) throw ApiError(0, 'no answer');
    switch (path) {
      case '/api/intake':
        return {'intake': intake, 'optimize_prompt': optimize, 'suggestions': true, 'side_call_tokens': 1000};
      case '/api/persistence':
        return {'on': persistence};
      case '/v1/status':
        return {'concurrency': {'side_slot': side > 0 ? {'context': side} : null}};
    }
    throw ApiError(404, 'not found');
  }

  @override
  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body]) async {
    calls.add('POST $path $body');
    if (refuse) throw ApiError(403, 'these settings can be changed only from the PC the server runs on');
    if (path == '/api/intake') {
      if (body!['optimize_prompt'] == true && side == 0) throw ApiError(400, 'Optimize prompt needs the technical session');
      if (body.containsKey('intake')) intake = body['intake'] as String;
      if (body.containsKey('optimize_prompt')) optimize = body['optimize_prompt'] as bool;
      return {'intake': intake, 'optimize_prompt': optimize};
    }
    if (path == '/api/persistence') {
      persistence = body!['on'] as bool;
      return {'on': persistence};
    }
    throw ApiError(404, 'not found');
  }
}

Future<FakeServer> server() async {
  SharedPreferences.setMockInitialValues({});
  return FakeServer(await AppSettings.load());
}

Widget host(Widget child) => MaterialApp(theme: ThemeData.dark(), home: Scaffold(body: SingleChildScrollView(padding: const EdgeInsets.all(16), child: child)));

void main() {
  setUp(() => Texts.use('en'));

  group('the switches the server keeps', () {
    test('they are read: prompt analysis is off by default, and the technical session is read from the engine status', () async {
      final s = await server();
      final p = ServerPrefs(s);
      expect(p.intake, 'off');
      await p.load();
      expect(p.intakeLoaded, isTrue);
      expect(p.persistenceLoaded, isTrue);
      expect(p.intake, 'off');
      expect(p.persistence, isTrue);
      expect(p.sideActive, isFalse);
      s.side = 4096;
      s.intake = 'full';
      s.persistence = false;
      await p.load();
      expect((p.intake, p.persistence, p.sideActive, p.sideTokens), ('full', false, true, 4096));
    });

    test('a change is sent as the page sends it and the value the server answers with is the one kept', () async {
      final s = await server();
      final p = ServerPrefs(s);
      await p.load();
      await p.setIntake('rules');
      await p.setPersistence(false);
      expect(s.calls, containsAll(['POST /api/intake {intake: rules}', 'POST /api/persistence {on: false}']));
      expect((s.intake, s.persistence), ('rules', false));
      expect((p.intake, p.persistence, p.noteBad), ('rules', false, false));
    });

    test('optimize cannot be switched on without the technical session, and is not even sent', () async {
      final s = await server();
      final p = ServerPrefs(s);
      await p.load();
      s.calls.clear();
      await p.setOptimize(true);
      expect(p.optimizePrompt, isFalse);
      expect(s.calls.where((c) => c.startsWith('POST')), isEmpty);
    });

    test('with the session it is switched on and, if the session went away meanwhile, the refusal puts it back and says why', () async {
      final s = await server();
      s.side = 2048;
      final p = ServerPrefs(s);
      await p.load();
      await p.setOptimize(true);
      expect(p.optimizePrompt, isTrue);
      expect(s.optimize, isTrue);
      await p.setOptimize(false);
      s.side = 0;
      await p.setOptimize(true); // the page still thinks there is a session
      expect(p.optimizePrompt, isFalse);
    });

    test('a saved "on" without a session can still be switched off', () async {
      final s = await server();
      s.optimize = true;
      final p = ServerPrefs(s);
      await p.load();
      expect(p.optimizePrompt, isTrue);
      expect(p.sideActive, isFalse);
      await p.setOptimize(false);
      expect(s.optimize, isFalse);
      expect(p.optimizePrompt, isFalse);
    });

    test('a PC that keeps its settings for itself refuses the change: the old value stays and the app says so in its own words', () async {
      final s = await server();
      s.refuse = true;
      final p = ServerPrefs(s);
      await p.load();
      await p.setIntake('full');
      await p.setPersistence(false);
      expect((p.intake, p.persistence), ('off', true));
      expect(p.noteBad, isTrue);
      expect(p.note, Texts.t('opts.pconly'));
    });

    test('nothing is sent before the values were read', () async {
      final s = await server();
      final p = ServerPrefs(s);
      await p.setIntake('full');
      await p.setPersistence(false);
      expect(s.calls, isEmpty);
    });
  });

  group('the options on the screen', () {
    testWidgets('on a narrow phone with large text: three states, a tick box, two switches; nothing overflows; the technical session cannot be changed', (tester) async {
      tester.view.physicalSize = const Size(360 * 3, 640 * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);
      final s = await server();
      final p = ServerPrefs(s);
      await p.load();
      await tester.pumpWidget(MediaQuery(data: const MediaQueryData(size: Size(360, 640), textScaler: TextScaler.linear(1.8)), child: host(ServerOptionsBody(prefs: p))));
      await tester.pump();
      for (final m in ['off', 'rules', 'full']) {
        expect(find.byKey(ValueKey('ps-$m')), findsOneWidget);
      }
      expect(find.text('Prompt analysis'), findsOneWidget);
      expect(find.text('Optimize the prompt'), findsOneWidget);
      expect(find.text('Work until done'), findsOneWidget);
      expect(find.text('Technical session'), findsOneWidget);
      // the technical session switch is shown but disabled; so is the optimize box while there is no session
      expect(tester.widget<SwitchListTile>(find.byKey(const ValueKey('technical-session'))).onChanged, isNull);
      expect(tester.widget<CheckboxListTile>(find.byKey(const ValueKey('opt-prompt'))).onChanged, isNull);
      expect(find.textContaining('Needs the technical session'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('tapping a state sends it, tapping the switch sends it, and the optimize box works once the session is there', (tester) async {
      tester.view.physicalSize = const Size(390 * 3, 1200 * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);
      final s = await server();
      s.side = 3000;
      final p = ServerPrefs(s);
      await tester.runAsync(p.load);
      await tester.pumpWidget(host(ServerOptionsBody(prefs: p)));
      await tester.tap(find.byKey(const ValueKey('ps-rules')));
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
      await tester.pump();
      expect(s.intake, 'rules');
      await tester.tap(find.byKey(const ValueKey('work-until-done')));
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
      await tester.pump();
      expect(s.persistence, isFalse);
      await tester.tap(find.byKey(const ValueKey('opt-prompt')));
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
      await tester.pump();
      expect(s.optimize, isTrue);
      expect(find.text('Saved on the PC.'), findsOneWidget);
    });

    testWidgets('the options are in Italian when the app is', (tester) async {
      Texts.use('it');
      final s = await server();
      final p = ServerPrefs(s);
      await tester.runAsync(p.load);
      await tester.pumpWidget(host(ServerOptionsBody(prefs: p)));
      expect(find.text('Analisi del prompt'), findsOneWidget);
      expect(find.text('Ottimizza il prompt'), findsOneWidget);
      expect(find.text('Lavora fino in fondo'), findsOneWidget);
      expect(find.text('Sessione tecnica'), findsOneWidget);
    });

    testWidgets('the role menu lists the roles of the PC and reports a pick', (tester) async {
      String? picked;
      await tester.pumpWidget(host(RolePicker(role: 'build', onChanged: (r) => picked = r)));
      expect(find.text('Developer'), findsOneWidget);
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      for (final r in ['Plan', 'Ask', 'Debug', 'Orchestrate', 'General']) {
        expect(find.text(r), findsWidgets);
      }
      await tester.tap(find.text('Debug').last);
      await tester.pumpAndSettle();
      expect(picked, 'debug');
      expect(agentRoles, ['build', 'plan', 'ask', 'debug', 'orchestrate', 'general']);
    });
  });
}
