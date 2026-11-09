import 'dart:convert';

import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/ask_card.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

List<Map<String, dynamic>> card() => [
      {
        'id': 'q1', 'text': 'Which database?', 'kind': 'single', 'required': true, 'allow_other': true,
        'options': [
          {'id': 'o1', 'label': 'PostgreSQL', 'recommended': true},
          {'id': 'o2', 'label': 'SQLite'},
        ],
      },
      {
        'id': 'q2', 'text': 'Which sections?', 'kind': 'multi', 'required': false, 'allow_other': true,
        'options': [
          {'id': 'o1', 'label': 'Intro'},
          {'id': 'o2', 'label': 'End'},
        ],
      },
      {'id': 'q3', 'text': 'Out of scope?', 'kind': 'free', 'required': false, 'options': []},
      {
        'id': 'q4', 'text': 'Publish it?', 'kind': 'confirm', 'required': true, 'recommended': true,
        'options': [
          {'id': 'yes', 'label': 'Yes'},
          {'id': 'no', 'label': 'No'},
        ],
      },
    ];

Map<String, dynamic> decode(String s) => jsonDecode(s.substring(AskSelection.prefix.length)) as Map<String, dynamic>;

void main() {
  setUp(() => Texts.use('en'));

  test('the first required question is the one missing, and a single keeps one choice', () {
    final s = AskSelection(card());
    expect(s.missing(), 'q1');
    s.setPick('q1', 'o1', true);
    s.setPick('q1', 'o2', true);
    expect(s.picked('q1'), ['o2']);
    expect(s.missing(), 'q4');
    s.setPick('q4', 'no', true);
    expect(s.missing(), isNull);
  });

  test('the row of their own replaces a single choice, and typing in it chooses it', () {
    final s = AskSelection(card());
    s.setOther('q1', true);
    expect(s.missing(), 'q1');
    s.setText('q1', 'MariaDB');
    s.setPick('q4', 'yes', true);
    expect(s.missing(), isNull);
    expect(decode(s.encode('submitted'))['answers']['q1'], {'picked': <String>[], 'text': 'MariaDB'});
    s.setPick('q1', 'o1', true);
    expect(s.other('q1'), false);
    s.setText('q2', 'a glossary');
    expect(s.other('q2'), true);
  });

  test('the answer leaves as ask: and JSON of ids; skip and chat carry no answers', () {
    final s = AskSelection(card());
    s.setPick('q1', 'o2', true);
    s.setPick('q2', 'o1', true);
    s.setText('q3', 'free words');
    s.setPick('q4', 'yes', true);
    final sent = decode(s.encode('submitted'));
    expect(sent['status'], 'submitted');
    expect(sent['answers']['q3'], {'picked': <String>[], 'text': 'free words'});
    expect(sent['answers']['q4']['picked'], ['yes']);
    expect(decode(s.encode('skipped')), {'v': 1, 'status': 'skipped', 'answers': {}});
    expect(decode(s.encode('chat'))['status'], 'chat');
  });

  test('one question of single or confirm kind is answered by one tap', () {
    expect(AskSelection([card()[0]]).fast, true);
    expect(AskSelection([card()[3]]).fast, true);
    expect(AskSelection([card()[1]]).fast, false);
    expect(AskSelection(card()).fast, false);
  });

  testWidgets('the card has rows of 48 dp, tags the recommended option in words, and Send waits for the required answers', (tester) async {
    final sent = <List<String>>[];
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(body: SingleChildScrollView(child: QuestionsCard(questions: card(), intro: '', answered: null, onSend: (v, how) => sent.add([v, how])))),
    ));
    expect(find.text('Recommended'), findsNWidgets(2)); // PostgreSQL, and Yes on the yes/no question
    expect(find.text('PostgreSQL'), findsOneWidget);
    final send = find.widgetWithText(FilledButton, 'Send answers');
    expect(tester.widget<FilledButton>(send).onPressed, isNull);
    expect(find.textContaining('Answer first'), findsOneWidget);
    expect(tester.getSize(find.text('SQLite')).height, lessThan(48));
    for (final t in ['PostgreSQL', 'SQLite']) {
      final row = find.ancestor(of: find.text(t), matching: find.byType(ConstrainedBox)).first;
      expect(tester.getSize(row).height, greaterThanOrEqualTo(48));
    }
    await tester.tap(find.text('PostgreSQL'));
    await tester.pump();
    await tester.ensureVisible(find.text('Yes'));
    await tester.tap(find.text('Yes'));
    await tester.pump();
    expect(tester.widget<FilledButton>(send).onPressed, isNotNull);
    await tester.ensureVisible(send);
    await tester.tap(send);
    await tester.pump();
    expect(sent.length, 1);
    expect(sent.first[1], 'submitted');
    expect(decode(sent.first[0])['answers']['q1']['picked'], ['o1']);
  });

  testWidgets('a card with one single question is answered by one tap; Skip and answering in the chat are always there', (tester) async {
    final sent = <List<String>>[];
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(body: QuestionsCard(questions: [card()[0]], intro: 'Because.', answered: null, onSend: (v, how) => sent.add([v, how]))),
    ));
    expect(find.text('Because.'), findsOneWidget);
    await tester.tap(find.text('Skip'));
    await tester.tap(find.text('Answer in the chat instead'));
    expect(sent.map((x) => x[1]).toList(), ['skipped', 'chat']);
    sent.clear();
    await tester.tap(find.text('SQLite'));
    await tester.pump();
    expect(sent.single[1], 'submitted');
    expect(decode(sent.single[0])['answers']['q1']['picked'], ['o2']);
  });

  testWidgets('after the answer the card is a read-only summary', (tester) async {
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: QuestionsCard(questions: card(), intro: '', answered: 'skipped', onSend: (v, h) {}))));
    expect(find.text('You skipped the questions: the agent decides and says what it assumed.'), findsOneWidget);
    expect(find.text('Send answers'), findsNothing);
  });

  testWidgets('a chip puts its text in the box and never sends', (tester) async {
    final picked = <String>[];
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: SuggestionChips(items: const [{'prompt': 'Explain the validation step'}], onPick: picked.add))));
    await tester.tap(find.text('Explain the validation step'));
    expect(picked, ['Explain the validation step']);
  });

  test('the controller reads the card, the chips and the harness note from the events', () async {
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    final ctl = ChatController(Api(settings), settings);
    ctl.applyEvents([
      {'event': 'ask', 'id': 'k1', 'mode': 'question', 'kind': 'questions', 'question': 'Which database?', 'intro': 'Why', 'questions': card(), 'turn': 't1'},
    ]);
    final b = ctl.items.last.blocks.last;
    expect(b.kind, 'questions');
    expect(b.questions.length, 4);
    expect(b.intro, 'Why');
    ctl.applyEvents([
      {'event': 'suggestions', 'items': [{'label': 'More', 'prompt': 'Tell me more'}]},
    ]);
    expect(ctl.suggestions, [{'label': 'More', 'prompt': 'Tell me more'}]);
    ctl.applyEvents([
      {'event': 'user', 'text': 'hi'},
    ]);
    expect(ctl.suggestions, isEmpty);
    expect(b.answered, '');
    ctl.applyEvents([
      {'event': 'intake', 'turn': 't2', 'assumptions': ['db: PostgreSQL'], 'parked': []},
    ]);
    expect(ctl.items.last.blocks.last.s, contains('db: PostgreSQL'));
  });
}
