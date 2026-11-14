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
        {'value': 'n9', 'label': 'Nove notizie', 'label_en': 'Nine stories'},
      ],
      'other': true, 'proposed': 'n6', 'content': '',
    };
Map<String, dynamic> two() => {
      'plan_id': 'news', 'plan_title': 'Newscast', 'node_id': 'i5', 'title': 'i5', 'ask': 'value', 'kind': 'choice', 'index': 5, 'other': false,
      'prompt': 'Il microfono è attivo?', 'prompt_en': 'Is the microphone on?', 'content': '',
      'choices': [
        {'value': 'on', 'label': 'Attivo', 'label_en': 'On'},
        {'value': 'off', 'label': 'Non attivo', 'label_en': 'Off'},
      ],
    };
Map<String, dynamic> task() => {
      'plan_id': 'news', 'plan_title': 'Newscast', 'node_id': 't', 'title': 'Check the cable', 'ask': 'task', 'kind': 'text', 'index': 6, 'other': false, 'choices': [],
      'prompt': 'Controlla il cavo', 'prompt_en': 'Check the cable', 'content': 'Look at the microphone cable.', 'proposed': null,
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

  group('the list of questions', () {
    test('ALL the questions of a plan are in the list, in the order of the plan, and what is proposed is in the fields', () {
      final many = [for (var i = 0; i < 43; i++) HumanRequest.fromJson({...topic(), 'node_id': 'q$i', 'index': 43 - i})];
      final g = HumanGroup.first(many, (_) => false)!;
      expect(g.items.length, 43);
      expect(g.items.first.nodeId, 'q42');
      final f = HumanForm(grouped().items);
      expect(f.answers, {'i1': 'n6', 'i2': 'politica'});
    });

    test('the headings come before the first question of each group, in the language of the app', () {
      final rows = all([
        {...choice(), 'group': 'Cosa e quando', 'group_en': 'What and when'},
        {...topic(), 'group': 'Cosa e quando', 'group_en': 'What and when'},
        {...approval(), 'group': 'Approvazioni', 'group_en': 'Approvals'},
        hours(),
      ]);
      expect([for (final (_, h) in humanSections(grouped(rows).items)) h], ['What and when', '', '', 'Approvals']);
      Texts.use('it');
      expect([for (final (_, h) in humanSections(grouped(rows).items)) h], ['Cosa e quando', '', '', 'Approvazioni']);
    });

    test('the ones not answered are counted; the answers are checked for their question; everything is sent together', () {
      final f = HumanForm(grouped().items);
      expect(f.unanswered.map((r) => r.nodeId), ['i3', 'i4']);
      f.answers['i3'] = 'many';
      expect(f.problem(f.items[2]), 'hr.need.number');
      f.answers['i3'] = '5';
      f.answers['i4'] = false;
      expect(f.unanswered, isEmpty);
      expect(f.payload, {'i1': 'n6', 'i2': 'politica', 'i3': '5', 'i4': false});
    });

    test('a choice is shown by its label in the language of the app, never by its value', () {
      final r = grouped().items.first;
      expect(r.choices.firstWhere((c) => c.value == 'n6').shown, 'Six stories');
      Texts.use('it');
      expect(r.choices.firstWhere((c) => c.value == 'n6').shown, 'Sei notizie');
      expect(r.question, 'Quante notizie vuoi?');
    });

    test('when the questions that are asking change, the answers given stay', () {
      final f = HumanForm(grouped().items)..answers['i2'] = 'mine';
      final r = f.reconcile(all([topic(), choice()]));
      expect(r.answers['i2'], 'mine');
      expect(r.items.length, 2);
    });
  });

  testWidgets('all the questions are in one list in the language of the app, a menu that shows labels, the proposed one chosen', (tester) async {
    await opened(tester, (_) => HumanListDialog(group: grouped(all([choice(), topic()])), send: (a) async {}), []);
    expect(find.text('2 questions'), findsOneWidget);
    expect(find.text('How many stories do you want?'), findsOneWidget);
    expect(find.text('From three to nine.'), findsOneWidget);
    expect(find.text('What is it about?'), findsOneWidget);
    expect(find.text('Six stories'), findsOneWidget);
    expect(find.text('n6'), findsNothing);
    expect(find.text('Next'), findsNothing);
    await tester.tap(find.text('Six stories'));
    await tester.pumpAndSettle();
    expect(find.text('Three stories'), findsOneWidget);
    expect(find.text('n3'), findsNothing);
  });

  testWidgets('in Italian the same questions are in Italian', (tester) async {
    Texts.use('it');
    await opened(tester, (_) => HumanListDialog(group: grouped(all([choice(), topic()])), send: (a) async {}), []);
    expect(find.text('2 domande'), findsOneWidget);
    expect(find.text('Quante notizie vuoi?'), findsOneWidget);
    expect(find.text('Da tre a nove.'), findsOneWidget);
    expect(find.text('Sei notizie'), findsOneWidget);
    expect(find.text('Invia tutte le risposte'), findsOneWidget);
    expect(find.text('Più tardi'), findsOneWidget);
  });

  testWidgets('headings are shown; Send with some missing shows which and sends nothing; one Send gives all the answers together', (tester) async {
    final sent = <Map<String, Object>>[];
    final results = <Object?>[];
    final rows = all([
      {...choice(), 'group': 'Cosa e quando', 'group_en': 'What and when'},
      {...topic(), 'group': 'Cosa e quando', 'group_en': 'What and when'},
      hours(),
      {...approval(), 'group': 'Approvazioni', 'group_en': 'Approvals'},
    ]);
    await opened(tester, (_) => HumanListDialog(group: grouped(rows), send: (a) async => sent.add(a)), results);
    expect(find.text('What and when'), findsOneWidget);
    expect(find.text('2 not answered yet'), findsOneWidget);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(find.text('Write an answer first.'), findsOneWidget);
    expect(sent, isEmpty);
    await tester.enterText(find.widgetWithText(TextField, 'Your answer').at(1), '12');
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Yes, approve'), 200, scrollable: find.byType(Scrollable).first);
    await tester.tap(find.text('Yes, approve'));
    await tester.pumpAndSettle();
    expect(sent, isEmpty);                                      // choosing never sends
    expect(find.text('All answered: send them when you are ready.'), findsOneWidget);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(sent, [
      {'i1': 'n6', 'i2': 'politica', 'i3': '12', 'i4': true}
    ]);
    expect(results, [HumanOutcome.answered]);
  });

  testWidgets('a refusal of the PC is shown and the list stays; Later leaves with the answers kept', (tester) async {
    final results = <Object?>[];
    HumanForm? kept;
    await opened(tester, (_) => HumanListDialog(group: grouped(all([topic()])), send: (a) async => throw ApiError(400, 'not asking now'), onForm: (f) => kept = f), results);
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
      await opened(tester, (_) => HumanListDialog(group: grouped(all([approval()])), send: (a) async {}), []);
      for (final label in ['Yes, approve', 'No', 'Send all answers', 'Later']) {
        final box = tester.getSize(find.ancestor(of: find.text(label), matching: find.byWidgetPredicate((w) => w is ButtonStyleButton)).first);
        expect(box.height, greaterThanOrEqualTo(48), reason: label);
      }
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('two possible answers are two buttons, and choosing one sends nothing', (tester) async {
    final sent = <Map<String, Object>>[];
    await opened(tester, (_) => HumanListDialog(group: grouped(all([two()])), send: (a) async => sent.add(a)), []);
    expect(find.byType(DropdownButtonFormField<String>), findsNothing);
    expect(find.text('On'), findsOneWidget);
    expect(find.text('Off'), findsOneWidget);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(find.text('Choose one of the two answers.'), findsOneWidget);
    await tester.tap(find.text('Off'));
    await tester.pumpAndSettle();
    expect(sent, isEmpty);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(sent, [
      {'i5': 'off'}
    ]);
  });

  testWidgets('a menu of more than two answers offers "Other, I write", which opens a text field', (tester) async {
    final sent = <Map<String, Object>>[];
    await opened(tester, (_) => HumanListDialog(group: grouped(all([choice()])), send: (a) async => sent.add(a)), []);
    await tester.tap(find.text('Six stories'));
    await tester.pumpAndSettle();
    expect(find.text('Other, I write'), findsOneWidget);
    await tester.tap(find.text('Other, I write').last);
    await tester.pumpAndSettle();
    expect(find.byType(TextField), findsOneWidget);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(find.text('Write an answer first.'), findsOneWidget);
    expect(sent, isEmpty);
    await tester.enterText(find.byType(TextField), 'Nine and a half');
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(sent, [
      {'i1': 'Nine and a half'}
    ]);
  });

  testWidgets('a step for the person is in the same list: what to do, a note, "I did it" closes it with the note', (tester) async {
    final sent = <Map<String, Object>>[];
    await opened(tester, (_) => HumanListDialog(group: grouped(all([task()])), send: (a) async => sent.add(a)), []);
    expect(find.text('Check the cable'), findsOneWidget);
    expect(find.text('What to do'), findsOneWidget);
    expect(find.text('Look at the microphone cable.'), findsOneWidget);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Choose'), findsOneWidget);
    await tester.enterText(find.byType(TextField), 'cable is fine');
    await tester.tap(find.text('I did it'));
    await tester.pumpAndSettle();
    expect(sent, isEmpty);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(sent, [
      {'t': {'done': true, 'note': 'cable is fine'}}
    ]);
  });

  testWidgets("I can't now sends nothing and the window closes", (tester) async {
    final sent = <Map<String, Object>>[];
    final results = <Object?>[];
    HumanForm? kept;
    await opened(tester, (_) => HumanListDialog(group: grouped(all([task()])), send: (a) async => sent.add(a), onForm: (f) => kept = f), results);
    await tester.tap(find.text("I can't now"));
    await tester.pumpAndSettle();
    expect(find.text('It comes back in 10 minutes.'), findsOneWidget);
    await tester.tap(find.text('Send all answers'));
    await tester.pumpAndSettle();
    expect(sent, isEmpty);
    expect(results, [HumanOutcome.answered]);
    expect(kept!.notNow, ['t']);
    expect(kept!.payload, isEmpty);
  });

  test('the form leaves out a task that cannot be done now, and the poller holds only that task back for ten minutes', () async {
    final p = HumanForm(grouped(all([choice(), task()])).items);
    expect(p.problem(p.items[1]), 'hr.need.task');
    p.setTask(p.items[1], state: 'later');
    expect(p.problem(p.items[1]), isNull);
    expect(p.payload, {'i1': 'n6'});
    expect(p.notNow, ['t']);
    p.setTask(p.items[1], state: 'done', note: ' ok ');
    expect(p.payload, {'i1': 'n6', 't': {'done': true, 'note': 'ok'}});

    var now = DateTime(2026, 11, 14, 10);
    final shown = <List<String>>[];
    var rows = all([choice(), task()]);
    late HumanPoller poller;
    poller = HumanPoller(
      fetch: () async => rows,
      show: (g, d) async {
        shown.add([for (final r in g.items) r.nodeId]);
        if (shown.length == 1) {
          poller.drafts['news'] = HumanForm(g.items)..setTask(g.items[1], state: 'later');   // what the window leaves when "I can't now" was chosen
        } else {
          rows = shown.length == 2 ? [for (final r in rows) if (r.nodeId != 'i1') r] : <HumanRequest>[];       // what was answered is gone from the list
        }
        return HumanOutcome.answered;
      },
      every: const Duration(hours: 1),
      clock: () => now,
    );
    poller.start();
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(shown, [['i1', 't'], ['i1']]);                       // the task is held back while the rest of the plan is asked
    now = now.add(const Duration(minutes: 11));
    rows = all([task()]);
    await poller.refresh();
    expect(shown.last, ['t']);                                  // and it comes back after ten minutes
    poller.stop();
  });

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
