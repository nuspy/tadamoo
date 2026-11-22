import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/long_answer.dart';
import 'package:basalt_mobile/src/long_answer_sheet.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

Future<AppSettings> settings() async {
  SharedPreferences.setMockInitialValues({});
  return AppSettings.load(secrets: FakeSecretStore());
}

void main() {
  setUp(() => Texts.use('en'));

  group('the line the harness reads', () {
    test('a new text of a fixed length', () {
      expect(const LongAnswerOptions(words: 8000).line, '[long answer: mode=write; size=fixed; words=8000]');
    });

    test('a length the agent judges has no number, a base length keeps it', () {
      expect(const LongAnswerOptions(size: 'heuristic', words: 8000).line, '[long answer: mode=write; size=heuristic]');
      expect(const LongAnswerOptions(size: 'hybrid', words: 5000).line, '[long answer: mode=write; size=hybrid; words=5000]');
    });

    test('a translation carries the language, a rewrite its task, and neither has a size or a length', () {
      expect(const LongAnswerOptions(mode: 'translate', lang: 'English').line, '[long answer: mode=translate; language=English]');
      expect(const LongAnswerOptions(mode: 'rewrite', lang: 'Italian', instr: 'shorter; formal', style: 'dry').line, '[long answer: mode=rewrite; language=Italian; task=shorter  formal; style=dry]');
    });

    test('a value cannot end the line or the field, and is cut at 80 characters', () {
      final l = const LongAnswerOptions(mode: 'translate', lang: 'It]alian\nx;y').line;
      expect(l, '[long answer: mode=translate; language=It alian x y]');
      expect(LongAnswerOptions(style: 'a' * 200).line, contains('style=${'a' * 80}]'));
    });

    test('the message goes first, the line after a blank line', () {
      expect(const LongAnswerOptions(words: 3000).decorate('Write about bees'), 'Write about bees\n\n[long answer: mode=write; size=fixed; words=3000]');
    });
  });

  group('what is missing', () {
    test('the length of a new text is 50 to 200000 words, unless the agent judges it', () {
      expect(const LongAnswerOptions(words: 49).problem, isNotEmpty);
      expect(const LongAnswerOptions(words: 200001).problem, isNotEmpty);
      expect(const LongAnswerOptions(words: 50).problem, isEmpty);
      expect(const LongAnswerOptions(size: 'heuristic', words: 0).problem, isEmpty);
    });

    test('a translation needs a language and a rewrite needs its task', () {
      expect(const LongAnswerOptions(mode: 'translate').problem, 'Say which language to translate into.');
      expect(const LongAnswerOptions(mode: 'translate', lang: 'English').problem, isEmpty);
      expect(const LongAnswerOptions(mode: 'rewrite').problem, 'Say what to change.');
      expect(const LongAnswerOptions(mode: 'rewrite', instr: 'shorter').problem, isEmpty);
    });
  });

  group('kept on the phone', () {
    test('the options come back as they were chosen, and an unreadable store gives the defaults', () async {
      final s = await settings();
      expect(s.longAnswer.mode, 'write');
      await s.setLongAnswer(const LongAnswerOptions(mode: 'translate', lang: 'English', style: 'plain'));
      expect(s.longAnswer.mode, 'translate');
      expect(s.longAnswer.lang, 'English');
      expect(LongAnswerOptions.decode('not json').mode, 'write');
      expect(LongAnswerOptions.fromJson({'mode': 'nonsense', 'size': 'huh'}).mode, 'write');
    });

    test('"stays on" is kept per conversation, and the oldest are forgotten past the limit', () async {
      final s = await settings();
      expect(s.longOn('a'), isFalse);
      await s.setLongOn('a', true);
      expect(s.longOn('a'), isTrue);
      await s.setLongOn('a', false);
      expect(s.longOn('a'), isFalse);
      for (var i = 0; i < maxLongOnKept + 5; i++) {
        await s.setLongOn('c$i', true);
      }
      expect(s.longOn('c0'), isFalse);
      expect(s.longOn('c${maxLongOnKept + 4}'), isTrue);
    });
  });

  group('the progress of a long answer in the conversation', () {
    Future<ChatController> controller() async {
      final s = await settings();
      return ChatController(Api(s), s);
    }

    test('one line follows the work, and the corrected text replaces the text shown', () async {
      final c = await controller();
      Map<String, dynamic> ev(Map<String, dynamic> d) => {'event': 'agent', 'turn': 't1', 'data': {'type': 'long_answer', ...d}};
      c.applyEvents([
        {'event': 'delta', 'kind': 'content', 'text': 'first draft', 'turn': 't1'},
        ev({'event': 'estimate', 'long': true, 'words': 8200}),
        ev({'event': 'plan', 'segments': [1, 2, 3, 4]}),
        ev({'event': 'segment_start', 'index': 2, 'of': 4}),
      ]);
      final notes = [for (final it in c.items) ...it.blocks.where((b) => b.kind == 'long')];
      expect(notes, hasLength(1), reason: 'the same line is updated, not a new one per event');
      expect(notes.single.s, 'Long answer: writing part 2 of 4.');
      c.applyEvents([
        ev({'event': 'done', 'words': 8100, 'target_words': 8000, 'segments': 4, 'of': 4, 'applied': 2, 'unapplied': [1]}),
        ev({'event': 'revised', 'text': 'the corrected text'}),
      ]);
      expect(notes.single.s, 'Long answer: 8100 words (planned 8000) in 4 of 4 parts. 2 correction(s) made, 1 not made.');
      final text = [for (final it in c.items) ...it.blocks.where((b) => b.t == 'text')].single;
      expect(text.s, 'the corrected text');
    });

    test('a fix that was not made says why', () async {
      final c = await controller();
      c.applyEvents([
        {'event': 'agent', 'turn': 't1', 'data': {'type': 'long_answer', 'event': 'fix', 'status': 'skipped', 'n': 1, 'of': 3, 'chunk': 2, 'reason': 'not_found'}},
      ]);
      final note = [for (final it in c.items) ...it.blocks.where((b) => b.kind == 'long')].single;
      expect(note.s, 'Long answer: correction 1 of 3 in part 2 not made (the quoted words are not in the part).');
    });
  });

  group('the dialog', () {
    Widget host(AppSettings s, void Function(String?) got) => MaterialApp(
          home: Scaffold(
            body: Builder(
              builder: (c) => TextButton(
                onPressed: () async => got(await showLongAnswerSheet(c, s, armed: false)),
                child: const Text('open'),
              ),
            ),
          ),
        );

    testWidgets('a translation without a language is not accepted, with one it is armed for the next message', (t) async {
      t.view.physicalSize = const Size(360 * 3, 900 * 3);
      t.view.devicePixelRatio = 3;
      addTearDown(t.view.reset);
      final s = await settings();
      await s.setLongAnswer(const LongAnswerOptions(mode: 'translate'));
      String? got = 'unset';
      await t.pumpWidget(host(s, (v) => got = v));
      await t.tap(find.text('open'));
      await t.pumpAndSettle();
      await t.tap(find.text('Only this next prompt'));
      await t.pumpAndSettle();
      expect(find.text('Say which language to translate into.'), findsOneWidget);
      expect(got, 'unset');
      await t.enterText(find.byType(TextField).first, 'English');
      await t.tap(find.text('Only this next prompt'));
      await t.pumpAndSettle();
      expect(got, 'once');
      expect(s.longAnswer.lang, 'English');
    });
  });
}
