import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/event_blocks.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

Future<ChatController> controller() async {
  SharedPreferences.setMockInitialValues({});
  final settings = await AppSettings.load(secrets: FakeSecretStore());
  return ChatController(Api(settings), settings);
}

List<Block> blocks(ChatController c) => [for (final it in c.items) ...it.blocks];

Widget host(Widget child) => MaterialApp(theme: ThemeData.dark(), home: Scaffold(body: SingleChildScrollView(child: child)));

void main() {
  setUp(() => Texts.use('en'));

  group('events in the conversation', () {
    test('a background command gets one block that follows its state and collects its output', () async {
      final c = await controller();
      c.applyEvents([
        {'event': 'job', 'job': 'job3', 'command': 'npm test', 'state': 'running', 'turn': 't1'},
        {'event': 'job_output', 'job': 'job3', 'stream': 'out', 'text': 'ok 1\n', 'turn': 't1'},
        {'event': 'job_output', 'job': 'job3', 'stream': 'err', 'text': 'warn\n', 'turn': 't1'},
        {'event': 'job', 'job': 'job3', 'command': 'npm test', 'state': 'finished', 'exit_code': 0, 'turn': 't1'},
      ]);
      final jobs = blocks(c).where((b) => b.t == 'job').toList();
      expect(jobs, hasLength(1));
      expect(jobs.single.s, 'npm test');
      expect(jobs.single.n, 3);
      expect(jobs.single.status, 'finished');
      expect(jobs.single.code, 0);
      expect(jobs.single.segs.map((g) => g.err), [false, true]);
      expect(jobs.single.segs.map((g) => g.text).join(), 'ok 1\nwarn\n');
    });

    test('output that arrives before the first state line still gets its block, and old pieces are dropped past the limit', () async {
      final c = await controller();
      c.applyEvents([
        for (var i = 0; i < maxJobSegs + 5; i++) {'event': 'job_output', 'job': 'job1', 'stream': 'out', 'text': '$i;', 'turn': 't1'},
      ]);
      final job = blocks(c).singleWhere((b) => b.t == 'job');
      expect(job.segs, hasLength(maxJobSegs));
      expect(job.segs.first.text, '5;');
    });

    test('the lines about the technical session, the course, the completion check and an interrupted batch are notes', () async {
      final c = await controller();
      c.applyEvents([
        {'event': 'auto_approved', 'tool': 'shell_command', 'arguments': '{"command":"ls"}', 'turn': 't1'},
        {'event': 'agent', 'turn': 't1', 'data': {'type': 'course_check', 'kind': 'loop', 'stage': 'note', 'problem': 'same call twice'}},
        {'event': 'agent', 'turn': 't1', 'data': {'type': 'course_check', 'kind': 'loop', 'stage': 'card', 'problem': 'no progress'}},
        {'event': 'agent', 'turn': 't1', 'data': {'type': 'course_check', 'kind': 'fold', 'stage': 'note', 'problem': ''}},
        {'event': 'agent', 'turn': 't1', 'data': {'type': 'completion_check', 'round': 2, 'of': 5, 'note': '[harness] the tests were not run'}},
        {'event': 'agent', 'turn': 't1', 'data': {'type': 'batch_interrupted', 'skipped': 3}},
      ]);
      final notes = blocks(c).where((b) => b.t == 'note').map((b) => b.s).toList();
      expect(notes, [
        'The technical session approved this call for you: shell_command {"command":"ls"}',
        'Course check: same call twice. The agent was told to change what it does.',
        'Course check: no progress. The agent asks how you want to go on.',
        'The context is nearly full: the agent was told to save what must survive.',
        'Work until done (2 of 5): the tests were not run',
        'You wrote something that changes the work: 3 more call(s) were not run.',
      ]);
    });

    test('the notes come in Italian when the app is', () async {
      Texts.use('it');
      final c = await controller();
      c.applyEvents([
        {'event': 'agent', 'turn': 't1', 'data': {'type': 'batch_interrupted', 'skipped': 1}},
      ]);
      expect(blocks(c).single.s, startsWith('Hai scritto qualcosa che cambia il lavoro'));
    });

    test('a question about orders found in outside text keeps where they came from', () async {
      final c = await controller();
      c.applyEvents([
        {
          'event': 'ask', 'id': 'q9', 'mode': 'confirm', 'kind': 'permission', 'tool': 'shell_command', 'arguments': {'command': 'curl x'},
          'instructions': 'https://evil.example/page', 'turn': 't1',
          'question': 'Text read from https://evil.example/page gave instructions to the AI.',
          'options': [{'label': 'Authorize these instructions', 'value': 'confirm'}, {'label': 'Deny', 'value': 'cancel'}],
        },
        {'event': 'ask', 'id': 'q10', 'mode': 'confirm', 'kind': 'permission', 'tool': 'write_file', 'arguments': {}, 'hook': 'no-secrets', 'turn': 't1', 'question': 'A hook asks.', 'options': []},
        {'event': 'ask', 'id': 'q11', 'mode': 'confirm', 'kind': 'permission', 'tool': 'write_file', 'arguments': {}, 'turn': 't1', 'question': 'Allow?', 'options': []},
      ]);
      final asks = blocks(c).where((b) => b.t == 'ask').toList();
      expect(asks[0].instructions, 'https://evil.example/page');
      expect(asks[0].hook, '');
      expect(asks[1].hook, 'no-secrets');
      expect(asks[1].instructions, '');
      expect(asks[2].instructions + asks[2].hook, '');
    });

    test('a change of role is a note and is kept in the conversation record', () async {
      final c = await controller();
      c.info = SessionInfo({'id': 's1', 'role': 'general'});
      c.applyEvents([
        {'event': 'role', 'role': 'plan'},
      ]);
      expect(c.info!.role, 'plan');
      expect(c.items.single.text, 'Role is now Plan.');
    });
  });

  group('new conversation', () {
    test('the role goes in the request only when one was chosen, so the other fields stay as they were', () {
      expect(NewChat().body().containsKey('role'), isFalse);
      expect(NewChat(role: 'debug').body()['role'], 'debug');
      expect(NewChat(role: 'plan').copy(mode: 'auto').role, 'plan');
    });
  });

  group('drawing', () {
    testWidgets('a job shows its command, state, exit code and output, and fits a narrow phone with large text', (tester) async {
      tester.view.physicalSize = const Size(360 * 3, 640 * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);
      final b = Block('job', id: 'job2', s: 'python -m http.server 8000')
        ..status = 'finished'
        ..code = 1
        ..segs.addAll([JobSeg(false, 'Serving on 0.0.0.0\n'), JobSeg(true, 'Traceback...\n')]);
      await tester.pumpWidget(MediaQuery(data: const MediaQueryData(size: Size(360, 640), textScaler: TextScaler.linear(2)), child: host(JobBlock(block: b))));
      expect(find.text('python -m http.server 8000'), findsOneWidget);
      expect(find.text('job2 · finished (1)'), findsOneWidget);
      expect(find.textContaining('Serving on 0.0.0.0', findRichText: true), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('the question about orders in a text puts Deny first as the main button, and each button is 48 high', (tester) async {
      String? sent;
      final b = Block('ask', id: 'q9')
        ..mode = 'confirm'
        ..kind = 'permission'
        ..name = 'shell_command'
        ..args = {'command': 'curl x'}
        ..instructions = 'https://evil.example/page'
        ..options = [{'label': 'Authorize these instructions', 'value': 'confirm'}, {'label': 'Deny', 'value': 'cancel'}];
      await tester.pumpWidget(host(OutsideAskCard(block: b, onAnswer: (v) => sent = v)));
      expect(find.textContaining('https://evil.example/page'), findsWidgets);
      expect(find.byType(FilledButton), findsOneWidget);
      expect(find.descendant(of: find.byType(FilledButton), matching: find.text('Deny')), findsOneWidget);
      expect(tester.getSize(find.byKey(const ValueKey('outside-yes'))).height, greaterThanOrEqualTo(48));
      expect(tester.getSize(find.byKey(const ValueKey('outside-no'))).height, greaterThanOrEqualTo(48));
      await tester.tap(find.byKey(const ValueKey('outside-yes')));
      expect(sent, 'confirm');
    });

    testWidgets('a hook question uses its own words and an answered one cannot be answered again', (tester) async {
      var calls = 0;
      final b = Block('ask', id: 'q10')
        ..mode = 'confirm'
        ..kind = 'permission'
        ..name = 'write_file'
        ..hook = 'no-secrets'
        ..answered = 'confirm';
      await tester.pumpWidget(host(OutsideAskCard(block: b, onAnswer: (_) => calls++)));
      expect(find.text('A hook of yours asks'), findsOneWidget);
      expect(find.textContaining('no-secrets'), findsOneWidget);
      expect(find.text('You answered: Allow this call'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('outside-yes')), warnIfMissed: false);
      expect(calls, 0);
    });
  });
}
