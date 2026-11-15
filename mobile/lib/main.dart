import 'dart:io';

import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';

import 'src/action_notifier.dart';
import 'src/api.dart';
import 'src/app.dart';
import 'src/avatar_assets.dart';
import 'src/avatar_hub.dart';
import 'src/avatar_platform.dart';
import 'src/i18n.dart';
import 'src/personal_engine.dart';
import 'src/settings.dart';
import 'src/voice.dart';
import 'src/wav2lip_engine.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final settings = await AppSettings.load();
  Texts.use(settings.language);
  final voice = Voice(settings);
  await voice.init();
  final support = await getApplicationSupportDirectory();
  final store = AvatarAssetStore(root: Directory('${support.path}/avatar'), source: HttpAssetSource(Api(settings)));
  final hub = EngineHub(settings: settings, store: store, backend: ChannelWav2LipBackend(), personal: ChannelPersonalBackend());
  final notifier = PlatformActionNotifier();
  runApp(BasaltApp(settings: settings, voice: voice, avatar: PlatformAvatarDevices(), hub: hub, notifier: notifier));
  _watchForActions(settings, notifier);
}

/// Notifications of required actions: the permission (Android 13 and later), then the periodic look of WorkManager. Once the phone is linked to the PC.
Future<void> _watchForActions(AppSettings settings, PlatformActionNotifier notifier) async {
  if (!Platform.isAndroid || !settings.configured) return;
  try {
    await notifier.allowed();
    await scheduleActionsCheck();
  } catch (e) {
    debugPrint('actions: the notifications could not be set up: $e');
  }
}
