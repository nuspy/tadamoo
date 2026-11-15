import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:workmanager/workmanager.dart';

import 'actions.dart';
import 'api.dart';
import 'i18n.dart';
import 'settings.dart';

/// What a notification for one entry looks like: its buttons (at most three; with more answers than that the notification only opens the app),
/// and whether it takes a written answer (an inline reply). Pure data, so that it can be tested without a phone.
class ActionButton {
  const ActionButton(this.id, this.label, {this.reply = false});
  final String id, label;

  /// A button that opens a field in the notification and sends what is written.
  final bool reply;
}

class NotificationPlan {
  const NotificationPlan({required this.id, required this.entryId, required this.title, required this.body, required this.buttons});
  final int id;
  final String entryId, title, body;
  final List<ActionButton> buttons;
}

const maxButtons = 3;
const replyAction = 'reply';
const answerPrefix = 'ans:';

/// A number for the entry that is the same in every process (a Dart string hash is not).
int notificationId(String entryId) {
  var h = 0x811c9dc5;
  for (final c in entryId.codeUnits) {
    h = ((h ^ c) * 0x01000193) & 0x7fffffff;
  }
  return h;
}

NotificationPlan planFor(ActionEntry e) {
  final buttons = <ActionButton>[];
  final reply = e.free && !e.isCard;
  final room = maxButtons - (reply ? 1 : 0);
  if (!e.isCard && e.options.length <= room) {
    for (final o in e.options) {
      buttons.add(ActionButton('$answerPrefix${o.value}', o.shown));
    }
  }
  if (reply) buttons.add(ActionButton(replyAction, e.kind == 'task' ? Texts.t('ac.note') : Texts.t('ac.reply'), reply: true));
  final src = Texts.t('ac.src.${e.source}');
  return NotificationPlan(
    id: notificationId(e.id),
    entryId: e.id,
    title: e.context.isEmpty ? src : '$src · ${e.context}',
    body: e.question,
    buttons: buttons,
  );
}

/// What a tap on a notification or one of its buttons means: null opens the app; else the answer to send.
class ActionReply {
  const ActionReply(this.entryId, this.value, this.text);
  final String entryId, value, text;
}

ActionReply? replyFor(String? payload, String? actionId, String? input) {
  if (payload == null || payload.isEmpty || actionId == null || actionId.isEmpty) return null;
  if (actionId == replyAction) {
    final t = (input ?? '').trim();
    return t.isEmpty ? null : ActionReply(payload, '', t);
  }
  if (actionId.startsWith(answerPrefix)) return ActionReply(payload, actionId.substring(answerPrefix.length), (input ?? '').trim());
  return null;
}

/// Shows a notification; the phone's own implementation is below, a test has a recorder.
abstract class ActionNotifier {
  Future<void> show(NotificationPlan plan);
  Future<void> cancel(int id);
}

/// One look for new entries: a standard notification for each entry that was not announced yet, then the PC is told so (an entry is announced once, never
/// again). Returns how many were announced.
Future<int> checkActions(Api api, ActionNotifier notifier) async {
  if (!api.settings.configured) return 0;
  final fresh = [for (final e in await api.actions()) if (!e.notified) e];
  for (final e in fresh) {
    await notifier.show(planFor(e));
  }
  if (fresh.isNotEmpty) await api.markActionsNotified([for (final e in fresh) e.id]);
  return fresh.length;
}

/// The answer chosen on a notification (a button, or text written in it), sent to the PC. An entry that was answered elsewhere is not an error here.
Future<void> answerFromNotification(Api api, ActionNotifier notifier, ActionReply r) async {
  try {
    await api.answerAction(r.entryId, value: r.value, text: r.text);
  } on ApiError catch (e) {
    if (e.code != 'gone') rethrow;
  }
  await notifier.cancel(notificationId(r.entryId));
}

// ---- the phone ----------------------------------------------------------------------------------------------------------------------------

const _channel = 'actions';
const taskName = 'actions-check';

class PlatformActionNotifier implements ActionNotifier {
  PlatformActionNotifier([FlutterLocalNotificationsPlugin? plugin]) : _plugin = plugin ?? FlutterLocalNotificationsPlugin();
  final FlutterLocalNotificationsPlugin _plugin;

  /// The app itself (foreground): a tap or a button comes here; `onOpen` brings the Required actions tab to the front.
  Future<void> init({void Function(ActionReply?)? onResponse}) async {
    await _plugin.initialize(
      settings: const InitializationSettings(android: AndroidInitializationSettings('@mipmap/ic_launcher')),
      onDidReceiveNotificationResponse: (r) => onResponse?.call(replyFor(r.payload, r.actionId, r.input)),
      onDidReceiveBackgroundNotificationResponse: actionNotificationBackground,
    );
  }

  /// Android 13 and later ask before notifications are shown.
  Future<bool> allowed() async => await _plugin.resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>()?.requestNotificationsPermission() ?? false;

  @override
  Future<void> show(NotificationPlan plan) => _plugin.show(
        id: plan.id,
        title: plan.title,
        body: plan.body,
        payload: plan.entryId,
        notificationDetails: NotificationDetails(
          android: AndroidNotificationDetails(
            _channel,
            Texts.t('ac.title'),
            channelDescription: Texts.t('ac.help'),
            importance: Importance.high,
            priority: Priority.high,
            category: AndroidNotificationCategory.message,
            styleInformation: BigTextStyleInformation(plan.body),
            actions: [
              for (final b in plan.buttons)
                AndroidNotificationAction(b.id, b.label, cancelNotification: true, showsUserInterface: false, inputs: b.reply ? [AndroidNotificationActionInput(label: b.label)] : const []),
            ],
          ),
        ),
      );

  @override
  Future<void> cancel(int id) => _plugin.cancel(id: id);
}

/// A button or a written reply on a notification while the app is not running: the answer goes to the PC from here.
@pragma('vm:entry-point')
Future<void> actionNotificationBackground(NotificationResponse r) async {
  final reply = replyFor(r.payload, r.actionId, r.input);
  if (reply == null) return;
  try {
    final api = Api(await AppSettings.load());
    await answerFromNotification(api, PlatformActionNotifier(), reply);
  } catch (e) {
    debugPrint('actions: the answer from a notification was not sent: $e');
  }
}

/// The periodic look (WorkManager, about every 15 minutes, only with a network; no service stays running).
@pragma('vm:entry-point')
void actionsCallbackDispatcher() {
  Workmanager().executeTask((task, input) async {
    try {
      final settings = await AppSettings.load();
      Texts.use(settings.language);
      final notifier = PlatformActionNotifier();
      await notifier.init();
      await checkActions(Api(settings), notifier);
    } catch (e) {
      debugPrint('actions: the look failed: $e');
    }
    return true;
  });
}

Future<void> scheduleActionsCheck() async {
  await Workmanager().initialize(actionsCallbackDispatcher);
  await Workmanager().registerPeriodicTask(
    taskName,
    taskName,
    frequency: const Duration(minutes: 15),
    existingWorkPolicy: ExistingPeriodicWorkPolicy.update,
    constraints: Constraints(networkType: NetworkType.connected),
  );
}
