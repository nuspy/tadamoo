import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/human_requests.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> choice() => {
      'plan_id': 'news', 'plan_title': 'Newscast', 'node_id': 'i1', 'title': 'i1', 'ask': 'value', 'kind': 'choice', 'index': 1,
      'prompt': 'Quante notizie vuoi?', 'prompt_en': 'How many stories do you want?', 'help': 'Da tre a nove.', 'help_en': 'From three to nine.',
      'choices': [
        {'value': 'n3', 'label': 'Tre notizie', 'label_en': 'Three stories'},
        {'value': 'n6', 'label': 'Sei notizie', 'label_en': 'Six stories'},
      ],
      'proposed': 'n6', 'content': '',
    };
Map<String, dynamic> topic() => {
      'plan_id': 'news', 'plan_title': 'Newscast', 'node_id': 'i2', 'title': 'i2', 'ask': 'value', 'kind': 'text', 'index': 2,
      'prompt': 'Di che cosa parla?', 'prompt_en': 'What is it about?', 'help': '', 'help_en': '', 'choices': [], 'proposed': 'politica', 'content': '',
    };
Map<String, dynamic> hours() => {
      'plan_id': 'news', 'plan_title': 'Newscast', 'node_id': 'i3', 'title': 'i3', 'ask': 'value', 'kind': 'number', 'index': 3,
      'prompt': 'Quante ore?', 'prompt_en': 'How many hours?', 'choices': [], 'proposed': null, 'content': '',
    };
Map<String, dynamic> approval() => {
      'plan_id': 'news', 'plan_title': 'Newscast', 'node_id': 'i4', 'title': 'i4', 'ask': 'approve', 'kind': 'text', 'index': 4,
      'prompt': 'Il copione va bene?', 'prompt_en': 'Is the script good?', 'choices': [], 'proposed': null, 'content': 'Buonasera.\nPrima notizia.',
    };
Map<String, dynamic> other() => {
      'plan_id': 'other', 'plan_title': 'Other', 'node_id': 'q', 'title': 'q', 'ask': 'value', 'kind': 'text', 'index': 1, 'prompt': 'Altro?', 'choices': [], 'content': '',
    };

List<HumanRequest> all([List<Map<String, dynamic>>? rows]) => [for (final j in rows ?? [approval(), topic(), choice(), hours()]) HumanRequest.fromJson(j)];
HumanGroup grouped([List<HumanRequest>? rows]) => HumanGroup.first(rows ?? all(), (_) => false)!;

Widget host(Widget Function(BuildContext) open, List<Object?> results) => MaterialApp(
      home: Builder(
        builder: (context) => Scaffold(
          body: Center(
            child: ElevatedButton(
              onPressed: () async => results.add(await showDialog<HumanOutcome>(context: context, barrierDismissible: false, builder: open)),
              child: const Text('open'),
            ),
          ),
        ),
      ),
    );

Future<void> opened(WidgetTester tester, Widget Function(BuildContext) open, List<Object?> results) async {
  await tester.pumpWidget(host(open, results));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
}

void main() {
  setUp(() => Texts.use('en'));

  group('the path through the questions', () {
    test('the questions of a plan are in the order of the plan, and what is proposed is in the fields', () {
      final g = grouped();
      expect(g.items.map((r) => r.nodeId), ['i1', 'i2', 'i3', 'i4']);
      final p = HumanPath(g.items);
      expect(p.answers, {'i1': 'n6', 'i2': 'politica'});
    });

    test('Next goes on only with a good answer; after the last it is the summary; Edit from the summary comes straight back', () {
      final p = HumanPath(grouped().items);
      expect(p.next(), isNull);
      expect(p.step, 1);
      expect(p.next(), isNull);
      expect(p.next(), 'hr.need');                              // the number has no answer
      p.answers['i3'] = 'many';
      expect(p.next(), 'hr.need.number');
      p.answers['i3'] = '12';
      expect(p.next(), isNull);
      expect(p.next(), 'hr.need.yesno');
      p.answers['i4'] = true;
      expect(p.next(), isNull);
      expect(p.inSummary, isTrue);
      expect(p.firstMissing, -1);
      p.edit(0);
      p.answers['i1'] = 'n3';
      expect(p.next(), isNull);
      expect(p.inSummary, isTrue);
      p.prev();
      p.prev();
      p.prev();
      p.prev();
      p.prev();
      expect(p.step, 0);                                        // it does not go below the first
    });

    test('a choice is shown by its label in the language of the app, never by its value; everything is sent together', () {
      final p = HumanPath(grouped().items)..answers['i3'] = '5'..answers['i4'] = false;
      expect(p.shown(p.items[0]), 'Six stories');
      Texts.use('it');
      expect(p.shown(p.items[0]), 'Sei notizie');
      expect(p.items[0].question, 'Quante notizie vuoi?');
      expect(p.shown(p.items[3]), 'No');
      expect(p.payload, {'i1': 'n6', 'i2': 'politica', 'i3': '5', 'i4': false});
    });

    test('when the questions that are asking change, the answers stay and the step stays where it can', () {
      final p = HumanPath(grouped().items)..answers['i2'] = 'mine'..step = 3;
      final r = p.reconcile(all([topic(), choice()]));
      expect(r.answers['i2'], 'mine');
      expect(r.step, 2);
    });
  });

  testWidgets('one question at a time in the language of the app, a menu that shows labels, the proposed one chosen', (tester) async {
    await opened(tester, (_) => HumanWizardDialog(group: grouped(all([choice(), topic()])), send: (a) async {}), []);
    expect(find.text('Question 1 of 2'), findsOneWidget);
    expect(find.text('How many stories do you want?'), findsOneWidget);
    expect(find.text('From three to nine.'), findsOneWidget);
    expect(find.text('Six stories'), findsOneWidget);
    expect(find.text('n6'), findsNothing);
    await tester.tap(find.text('Six stories'));
    await tester.pumpAndSettle();
    expect(find.text('Three stories'), findsOneWidget);
    expect(find.text('n3'), findsNothing);
    await tester.tap(find.text('Three stories').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Next'));
    await tester.pumpAndSettle();
    expect(find.text('Question 2 of 2'), findsOneWidget);
    expect(find.text('What is it about?'), findsOneWidget);
  });

  testWidgets('in Italian the same questions are in Italian', (tester) async {
    Texts.use('it');
    await opened(tester, (_) => HumanWizardDialog(group: grouped(all([choice(), topic()])), send: (a) async {}), []);
    expect(find.text('Domanda 1 di 2'), findsOneWidget);
    expect(find.text('Quante notizie vuoi?'), findsOneWidget);
    expect(find.text('Da tre a nove.'), findsOneWidget);
    expect(find.text('Sei notizie'), findsOneWidget);
    expect(find.text('Avanti'), findsOneWidget);
    expect(find.text('Più tardi'), findsOneWidget);
  });

  testWidgets('through to the summary, Edit one answer, and one Send gives all the answers together', (tester) async {
    final sent = <Map<String, Object>>[];
    final results = <Object?>[];
    await opened(tester, (_) => HumanWizardDialog(group: grouped(), send: (a) async => sent.add(a)), results);
    // the order of the plan is i1 (menu), i2 (text), i3 (number), i4 (yes or no)
    await tester.tap(find.text('Next'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Next'));                        // the text is proposed
    await tester.pumpAndSettle();
    await tester.tap(find.text('Next'));                        // the number is empty: refused
    await tester.pumpAndSettle();
    expect(find.text('Write an answer first.'), findsOneWidget);
    expect(find.text('Question 3 of 4'), findsOneWidget);
    await tester.enterText(find.byType(TextField), 'many');
    await tester.tap(find.text('Next'));
    await tester.pumpAndSettle();
    expect(find.text('The answer is a number.'), findsOneWidget);
    await tester.enterText(find.byType(TextField), '12');
    await tester.tap(find.text('Next'));
    await tester.pumpAndSettle();
    expect(find.text('Is the script good?'), findsOneWidget);
    expect(find.text('Buonasera.\nPrima notizia.'), findsOneWidget);
    await tester.tap(find.text('Yes, approve'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Next'));
    await tester.pumpAndSettle();
    expect(find.text('Check your answers'), findsOneWidget);
    expect(find.text('Six stories'), findsOneWidget);
    expect(find.text('politica'), findsOneWidget);
    expect(find.text('12'), findsOneWidget);
    expect(find.text('Yes'), findsOneWidget);
    expect(find.text('Edit'), findsNWidgets(4));
    expect(sent, isEmpty);
    await tester.tap(find.text('Edit').first);                  // change the menu: it comes straight back to the summary
    await tester.pumpAndSettle();
    expect(find.text('Question 1 of 4'), findsOneWidget);
    await tester.tap(find.text('Six stories'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Three stories').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Next'));
    await tester.pumpAndSettle();
    expect(find.text('Check your answers'), findsOneWidget);
    expect(find.text('Three stories'), findsOneWidget);
    expect(sent, isEmpty);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(sent, [
      {'i1': 'n3', 'i2': 'politica', 'i3': '12', 'i4': true}
    ]);
    expect(results, [HumanOutcome.answered]);
  });

  testWidgets('the arrow keys of a keyboard move along the path where a text is not being typed', (tester) async {
    await opened(tester, (_) => HumanWizardDialog(group: grouped(all([choice(), topic()])), send: (a) async {}), []);
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
    await tester.pumpAndSettle();
    expect(find.text('Question 2 of 2'), findsOneWidget);
  });

  testWidgets('a refusal of the PC is shown and the summary stays; Later leaves with the answers kept', (tester) async {
    final results = <Object?>[];
    HumanPath? kept;
    await opened(tester, (_) => HumanWizardDialog(group: grouped(all([topic()])), send: (a) async => throw ApiError(400, 'not asking now'), onPath: (p) => kept = p), results);
    await tester.tap(find.text('Next'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(find.text('They could not be sent: not asking now'), findsOneWidget);
    expect(results, isEmpty);
    await tester.tap(find.text('Later'));
    await tester.pumpAndSettle();
    expect(results, [HumanOutcome.later]);
    expect(kept!.answers['i2'], 'politica');
  });

  for (final size in const [Size(360, 640), Size(390, 844), Size(1280, 800)]) {
    testWidgets('at ${size.width.toInt()}x${size.height.toInt()} the buttons are at least 48 logical pixels and nothing overflows', (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      await opened(tester, (_) => HumanWizardDialog(group: grouped(all([approval()])), send: (a) async {}), []);
      for (final label in ['Yes, approve', 'No', 'Next', 'Later']) {
        final box = tester.getSize(find.ancestor(of: find.text(label), matching: find.byWidgetPredicate((w) => w is ButtonStyleButton)).first);
        expect(box.height, greaterThanOrEqualTo(48), reason: label);
      }
      expect(tester.takeException(), isNull);
    });
  }

  test('the poller shows the first plan, leaves one for later with its answers, and looks again after an answer', () async {
    var now = DateTime(2026, 11, 13, 10);
    final shown = <String>[];
    var rows = all([choice(), topic(), other()]);
    final outcomes = [HumanOutcome.later, HumanOutcome.answered];
    final poller = HumanPoller(
      fetch: () async => rows,
      show: (g, d) async {
        shown.add('${g.planId} ${g.items.length}');
        final out = outcomes.isEmpty ? HumanOutcome.later : outcomes.removeAt(0);
        if (out == HumanOutcome.answered) rows = [for (final r in rows) if (r.planId != g.planId) r];
        return out;
      },
      every: const Duration(hours: 1),
      clock: () => now,
    );
    poller.start();
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(shown, ['news 2', 'other 1']);                       // news was left for later, so the next plan is shown, and answered
    now = now.add(const Duration(minutes: 11));
    rows = all([choice(), topic()]);
    await poller.refresh();
    expect(shown.last, 'news 2');                               // after the pause it comes back
    poller.stop();
  });
}
