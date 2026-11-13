import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/human_requests.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

HumanRequest approval() => HumanRequest.fromJson({
      'plan_id': 'news', 'plan_title': 'Newscast', 'node_id': 'ok_script', 'title': 'Approve the script', 'ask': 'approve',
      'prompt': 'Is the script good?', 'content': 'Line one\nLine two', 'kind': 'text', 'choices': [],
    });

HumanRequest valueAsk({String kind = 'text', List<String> choices = const []}) => HumanRequest.fromJson({
      'plan_id': 'news', 'plan_title': 'Newscast', 'node_id': 'how_many', 'title': 'How many stories', 'ask': 'value',
      'prompt': 'How many stories?', 'content': '', 'kind': kind, 'choices': choices,
    });

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

void main() {
  setUp(() => Texts.use('en'));

  testWidgets('an approval asks, then confirms, then sends true', (tester) async {
    final sent = <Object>[];
    final results = <Object?>[];
    await tester.pumpWidget(host((_) => HumanRequestDialog(request: approval(), send: (a) async => sent.add(a)), results));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    expect(find.text('Newscast'), findsOneWidget);
    expect(find.text('Is the script good?'), findsOneWidget);
    expect(find.text('Line one\nLine two'), findsOneWidget);
    await tester.tap(find.text('Yes, approve'));
    await tester.pumpAndSettle();
    expect(sent, isEmpty);                                     // nothing is sent before the confirmation
    expect(find.text('Your answer: yes, approved'), findsOneWidget);
    await tester.tap(find.text('Confirm and send'));
    await tester.pumpAndSettle();
    expect(sent, [true]);
    expect(results, [HumanOutcome.answered]);
  });

  testWidgets('No is confirmed too, and Back from the confirmation returns to the choice', (tester) async {
    final sent = <Object>[];
    final results = <Object?>[];
    await tester.pumpWidget(host((_) => HumanRequestDialog(request: approval(), send: (a) async => sent.add(a)), results));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('No'));
    await tester.pumpAndSettle();
    expect(find.text('Your answer: no, not approved'), findsOneWidget);
    await tester.tap(find.text('Back'));
    await tester.pumpAndSettle();
    expect(find.text('Yes, approve'), findsOneWidget);
    expect(sent, isEmpty);
    await tester.tap(find.text('No'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Confirm and send'));
    await tester.pumpAndSettle();
    expect(sent, [false]);
  });

  testWidgets('Back on the first screen leaves the request for later', (tester) async {
    final results = <Object?>[];
    await tester.pumpWidget(host((_) => HumanRequestDialog(request: approval(), send: (a) async {}), results));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Back'));
    await tester.pumpAndSettle();
    expect(results, [HumanOutcome.later]);
  });

  testWidgets('a value: OK, then the confirmation, then it is sent; an empty one is refused', (tester) async {
    final sent = <Object>[];
    final results = <Object?>[];
    await tester.pumpWidget(host((_) => HumanRequestDialog(request: valueAsk(kind: 'number'), send: (a) async => sent.add(a)), results));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('OK'));
    await tester.pumpAndSettle();
    expect(find.text('Write an answer first.'), findsOneWidget);
    await tester.enterText(find.byType(TextField), 'abc');
    await tester.tap(find.text('OK'));
    await tester.pumpAndSettle();
    expect(find.text('The answer is a number.'), findsOneWidget);
    await tester.enterText(find.byType(TextField), '7');
    await tester.tap(find.text('OK'));
    await tester.pumpAndSettle();
    expect(sent, isEmpty);
    expect(find.text('Your answer: 7'), findsOneWidget);
    await tester.tap(find.text('Confirm and send'));
    await tester.pumpAndSettle();
    expect(sent, ['7']);
    expect(results, [HumanOutcome.answered]);
  });

  testWidgets('a refusal of the PC is shown and the window stays', (tester) async {
    final results = <Object?>[];
    await tester.pumpWidget(host((_) => HumanRequestDialog(request: valueAsk(), send: (a) async => throw ApiError(400, 'not asking now')), results));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'x');
    await tester.tap(find.text('OK'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Confirm and send'));
    await tester.pumpAndSettle();
    expect(find.text('It could not be sent: not asking now'), findsOneWidget);
    expect(results, isEmpty);
  });

  testWidgets('the buttons are at least 48 logical pixels high', (tester) async {
    await tester.pumpWidget(host((_) => HumanRequestDialog(request: approval(), send: (a) async {}), <Object?>[]));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    for (final label in ['Yes, approve', 'No', 'Back']) {
      final box = tester.getSize(find.ancestor(of: find.text(label), matching: find.byWidgetPredicate((w) => w is ButtonStyleButton)).first);
      expect(box.height, greaterThanOrEqualTo(48));
    }
  });

  test('the poller shows the oldest request, leaves a Back for later, and looks again after an answer', () async {
    var now = DateTime(2026, 11, 13, 10);
    final shown = <String>[];
    var list = [approval(), valueAsk()];
    final outcomes = [HumanOutcome.later, HumanOutcome.answered];
    final poller = HumanPoller(
      fetch: () async => list,
      show: (r, i, n) async {
        shown.add('${r.nodeId} $i/$n');
        final out = outcomes.isEmpty ? HumanOutcome.later : outcomes.removeAt(0);
        if (out == HumanOutcome.answered) list = [for (final x in list) if (x.key != r.key) x];
        return out;
      },
      every: const Duration(hours: 1),
      clock: () => now,
    );
    poller.start();
    await Future<void>.delayed(const Duration(milliseconds: 50));
    // the first was left for later, so the second is shown; it is answered; the first is still snoozed
    expect(shown, ['ok_script 1/2', 'how_many 1/1']);
    now = now.add(const Duration(minutes: 11));
    list = [approval()];
    await poller.refresh();
    expect(shown.last, 'ok_script 1/1');                       // after the pause it comes back
    poller.stop();
  });
}
