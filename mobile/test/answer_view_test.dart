import 'package:basalt_mobile/src/answer_view.dart';
import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

void main() {
  testWidgets('a link to a file is a button that names the file, a diagram keeps its source, other links stay as they are', (tester) async {
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.0.1:7232', 'flutter.key': 'bsk_x', 'flutter.language': 'en'});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    Texts.use('en');
    final taps = <List<String>>[];
    await tester.pumpWidget(MaterialApp(
      theme: ThemeData.dark(),
      home: Scaffold(
        body: SingleChildScrollView(
          child: AnswerView(
            api: Api(settings),
            sid: 's1',
            onFile: (kind, ref, name) => taps.add([kind, ref, name]),
            text: 'Here: [app.py](tadamoo-path:src/app.py) and [report.csv](tadamoo-file:abcdefabcdef).\n\n```mermaid\nflowchart TD\n  A --> B\n```\n\nAfter the diagram.',
          ),
        ),
      ),
    ));
    expect(find.textContaining('↓ app.py', findRichText: true), findsOneWidget);
    await tester.tapOnText(find.textRange.ofSubstring('↓ app.py'));
    await tester.tapOnText(find.textRange.ofSubstring('↓ report.csv'));
    expect(taps, [['path', 'src/app.py', 'app.py'], ['file', 'abcdefabcdef', 'report.csv']]);       // a tap on the link reaches the page
    expect(find.text('Diagram (source)'), findsOneWidget);
    expect(find.textContaining('After the diagram', findRichText: true), findsOneWidget);
    expect(find.textContaining('flowchart TD'), findsNothing);               // the source is in a box that is closed
    await tester.tap(find.text('Diagram (source)'));
    await tester.pumpAndSettle();
    expect(find.textContaining('flowchart TD'), findsOneWidget);
  });

  testWidgets('a picture carried as data is drawn, a link to a picture of the folder shows it and keeps the button, a picture at an address is not drawn', (tester) async {
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.0.1:7232', 'flutter.key': 'bsk_x', 'flutter.language': 'en'});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    Texts.use('en');
    const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
    final taps = <List<String>>[];
    await tester.pumpWidget(MaterialApp(
      theme: ThemeData.dark(),
      home: Scaffold(
        body: SingleChildScrollView(
          child: AnswerView(
            api: Api(settings),
            sid: 's1',
            onFile: (kind, ref, name) => taps.add([kind, ref, name]),
            text: '![cover](data:image/png;base64,$png)\n\n[cover.png](tadamoo-path:.tadamoo/artifacts/cover.png)\n\n![web](https://example.com/a.png)',
          ),
        ),
      ),
    ));
    expect(find.byType(Image), findsNWidgets(2));                            // the data one and the one of the folder; the address is not drawn
    expect(find.textContaining('↓ cover.png', findRichText: true), findsOneWidget);
    expect(find.text('web'), findsOneWidget);
  });
}
