import 'package:flutter/material.dart';

import 'api.dart';
import 'avatar_controller.dart';
import 'avatar_devices.dart';
import 'avatar_screen.dart';
import 'conversations.dart';
import 'i18n.dart';
import 'jobs_screen.dart';
import 'settings.dart';
import 'settings_screen.dart';
import 'voice.dart';

class BasaltApp extends StatelessWidget {
  const BasaltApp({super.key, required this.settings, required this.voice, required this.avatar});
  final AppSettings settings;
  final Voice voice;
  final AvatarDevices avatar;

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
          home: Home(settings: settings, voice: voice, avatar: avatar),
        );
      },
    );
  }
}

class Home extends StatefulWidget {
  const Home({super.key, required this.settings, required this.voice, required this.avatar});
  final AppSettings settings;
  final Voice voice;
  final AvatarDevices avatar;

  @override
  State<Home> createState() => _HomeState();
}

class _HomeState extends State<Home> {
  static const avatarTab = 2;
  static const settingsTab = 4;
  int tab = 0;
  late final AvatarController avatarController;

  @override
  void initState() {
    super.initState();
    avatarController = AvatarController(api: Api(widget.settings), settings: widget.settings, devices: widget.avatar);
    if (!widget.settings.configured) tab = settingsTab;
  }

  @override
  void dispose() {
    avatarController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = Api(widget.settings);
    final pages = <Widget>[
      ConversationsPane(api: api, settings: widget.settings, voice: widget.voice, agent: false),
      ConversationsPane(api: api, settings: widget.settings, voice: widget.voice, agent: true),
      AvatarScreen(controller: avatarController, active: tab == avatarTab),
      JobsScreen(api: api),
      SettingsScreen(settings: widget.settings, voice: widget.voice, route: avatarController.route),
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
          NavigationDestination(icon: const Icon(Icons.schedule_outlined), selectedIcon: const Icon(Icons.schedule), label: Texts.t('tab.jobs')),
          NavigationDestination(icon: const Icon(Icons.settings_outlined), selectedIcon: const Icon(Icons.settings), label: Texts.t('tab.settings')),
        ],
      ),
    );
  }
}
