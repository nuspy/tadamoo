import 'package:flutter/material.dart';

import 'src/app.dart';
import 'src/i18n.dart';
import 'src/settings.dart';
import 'src/voice.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final settings = await AppSettings.load();
  Texts.use(settings.language);
  final voice = Voice(settings);
  await voice.init();
  runApp(BasaltApp(settings: settings, voice: voice));
}
