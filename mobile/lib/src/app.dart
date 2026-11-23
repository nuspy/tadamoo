import 'package:flutter/material.dart';

import 'action_notifier.dart';
import 'actions.dart';
import 'api.dart';
import 'avatar_controller.dart';
import 'avatar_devices.dart';
import 'avatar_hub.dart';
import 'avatar_screen.dart';
import 'conversations.dart';
import 'human_requests.dart';
import 'i18n.dart';
import 'jobs_screen.dart';
import 'settings.dart';
import 'settings_screen.dart';
import 'tasks_screen.dart';
import 'voice.dart';

class BasaltApp extends StatelessWidget {
  const BasaltApp({super.key, required this.settings, required this.voice, required this.avatar, this.hub, this.notifier});
  final AppSettings settings;
  final Voice voice;
  final AvatarDevices avatar;

  /// What the phone can draw on its own; without it the avatar is the PC's video only.
  final EngineHub? hub;

  /// The notifications of required actions (the phone's own); none in a test.
  final PlatformActionNotifier? notifier;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: settings,
      builder: (context, _) {
        Texts.use(settings.language);
        final scheme = ColorScheme.fromSeed(seedColor: const Color(0xFFFF7A2F), brightness: Brightness.dark, surface: const Color(0xFF0E1114));
        return MaterialApp(
          title: 'TaDaMoo',
          debugShowCheckedModeBanner: false,
          theme: ThemeData(colorScheme: scheme, useMaterial3: true, scaffoldBackgroundColor: const Color(0xFF0E1114), cardColor: const Color(0xFF171C21)),
          home: Home(settings: settings, voice: voice, avatar: avatar, hub: hub, notifier: notifier),
        );
      },
    );
  }
}

class Home extends StatefulWidget {
  const Home({super.key, required this.settings, required this.voice, required this.avatar, this.hub, this.notifier});
  final AppSettings settings;
  final Voice voice;
  final AvatarDevices avatar;
  final EngineHub? hub;
  final PlatformActionNotifier? notifier;

  @override
  State<Home> createState() => _HomeState();
}

class _HomeState extends State<Home> with WidgetsBindingObserver {
  static const avatarTab = 2;
  static const actionsTab = 3;
  static const settingsTab = 4;
  int tab = 0;
  late final AvatarController avatarController;
  late final HumanPoller humans;
  late final ActionsFeed actions;

  @override
  void initState() {
    super.initState();
    avatarController = AvatarController(api: Api(widget.settings), settings: widget.settings, devices: widget.avatar, hub: widget.hub);
    if (!widget.settings.configured) tab = settingsTab;
    humans = HumanPoller(fetch: _humanRequests, show: _showHuman);
    actions = ActionsFeed(Api(widget.settings));
    WidgetsBinding.instance.addObserver(this);
    humans.start();
    actions.start();
    widget.notifier?.init(onResponse: _onNotification);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    humans.stop();
    actions.dispose();
    avatarController.dispose();
    super.dispose();
  }

  /// What a plan asks the person appears over any tab while the app is open, and stops being looked for while it is in the background; the same for
  /// the list of required actions (in the background only the notifications tell).
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      humans.start();
      actions.start();
    } else if (state == AppLifecycleState.paused) {
      humans.stop();
      actions.stop();
    }
  }

  /// A tap on a notification (not on one of its buttons) opens the Required actions tab; a button or a reply is sent from here while the app is open.
  void _onNotification(ActionReply? reply) {
    if (!mounted) return;
    if (reply == null) {
      setState(() => tab = actionsTab);
      return;
    }
    answerFromNotification(Api(widget.settings), widget.notifier!, reply).then((_) => actions.refresh(), onError: (_) => actions.refresh());
  }

  Future<List<HumanRequest>> _humanRequests() async {
    if (!widget.settings.configured) return const [];
    return [for (final j in await Api(widget.settings).humanRequests()) HumanRequest.fromJson(j)];
  }

  Future<HumanOutcome> _showHuman(HumanGroup group, HumanForm? draft) async {
    if (!mounted) return HumanOutcome.later;
    final api = Api(widget.settings);
    final out = await showDialog<HumanOutcome>(
      context: context,
      barrierDismissible: false,
      useRootNavigator: true,
      builder: (_) => HumanListDialog(
        group: group,
        draft: draft,
        onForm: (f) => humans.drafts[group.planId] = f,
        send: (answers) => api.answerHumanAll(group.planId, answers),
      ),
    );
    return out ?? HumanOutcome.later;
  }

  @override
  Widget build(BuildContext context) {
    final api = Api(widget.settings);
    final pages = <Widget>[
      ConversationsPane(api: api, settings: widget.settings, voice: widget.voice, agent: false),
      ConversationsPane(api: api, settings: widget.settings, voice: widget.voice, agent: true),
      AvatarScreen(controller: avatarController, active: tab == avatarTab),
      ActionsScreen(feed: actions, jobs: JobsScreen(api: api), tasks: TasksScreen(api: api)),
      SettingsScreen(settings: widget.settings, voice: widget.voice, route: avatarController.route, hub: avatarController.hub),
    ];
    return Scaffold(
      body: SafeArea(bottom: false, child: IndexedStack(index: tab, children: pages)),
      bottomNavigationBar: NavigationBar(
        selectedIndex: tab,
        onDestinationSelected: (i) => setState(() => tab = i),
        destinations: [
          NavigationDestination(icon: const Icon(Icons.chat_bubble_outline), selectedIcon: const Icon(Icons.chat_bubble), label: Texts.t('tab.chat')),
          NavigationDestination(icon: const Icon(Icons.folder_outlined), selectedIcon: const Icon(Icons.folder), label: Texts.t('tab.agent')),
          NavigationDestination(icon: const Icon(Icons.face_outlined), selectedIcon: const Icon(Icons.face), label: Texts.t('tab.avatar')),
          NavigationDestination(
            icon: ListenableBuilder(listenable: actions, builder: (_, _) => Badge(isLabelVisible: actions.count > 0, label: Text('${actions.count}'), child: const Icon(Icons.inbox_outlined))),
            selectedIcon: ListenableBuilder(listenable: actions, builder: (_, _) => Badge(isLabelVisible: actions.count > 0, label: Text('${actions.count}'), child: const Icon(Icons.inbox))),
            label: Texts.t('tab.actions'),
          ),
          NavigationDestination(icon: const Icon(Icons.settings_outlined), selectedIcon: const Icon(Icons.settings), label: Texts.t('tab.settings')),
        ],
      ),
    );
  }
}
