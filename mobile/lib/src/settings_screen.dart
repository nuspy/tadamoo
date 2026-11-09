import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:url_launcher/url_launcher.dart';

import 'api.dart';
import 'audio_route.dart';
import 'avatar_settings.dart';
import 'capabilities_screen.dart';
import 'chat_options.dart';
import 'i18n.dart';
import 'place.dart';
import 'pskills_screen.dart';
import 'server_prefs.dart';
import 'settings.dart';
import 'voice.dart';

/// The server's address and this phone's key come first and are always in view; the rest is the voice and the language.
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key, required this.settings, required this.voice, required this.route});
  final AppSettings settings;
  final Voice voice;

  /// The audio routing the Avatar tab uses, so that the choice made here is the one in use there.
  final AudioRouteController route;

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController address = TextEditingController(text: widget.settings.address);
  late final TextEditingController key = TextEditingController(text: widget.settings.key);
  late final TextEditingController locale = TextEditingController(text: widget.settings.speechLocale);
  String message = '';
  bool bad = false;
  bool testing = false;
  bool hideKey = true;
  late final ServerPrefs prefs = ServerPrefs(Api(widget.settings));

  @override
  void initState() {
    super.initState();
    if (widget.settings.configured) prefs.load();
  }

  @override
  void dispose() {
    prefs.dispose();
    address.dispose();
    key.dispose();
    locale.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    await widget.settings.setServer(address: address.text, key: key.text);
    await widget.settings.setSpeechLocale(locale.text.trim());
    setState(() {
      message = Texts.t('set.saved');
      bad = false;
    });
  }

  Future<void> _test() async {
    await _save();
    setState(() {
      testing = true;
      message = '';
    });
    try {
      final hello = await Api(widget.settings).hello();
      message = Texts.t('set.test.ok', ['${hello['device'] ?? ''}']);
      bad = false;
      prefs.load();
    } on ApiError catch (e) {
      message = Texts.t('set.test.fail', [e.message]);
      bad = true;
    }
    if (mounted) setState(() => testing = false);
  }

  Future<void> _scan() async {
    final code = await Navigator.of(context).push<String>(MaterialPageRoute(builder: (_) => const _ScanScreen()));
    if (code == null) return;
    try {
      final j = jsonDecode(code);
      if (j is! Map || j['url'] == null || j['key'] == null) throw const FormatException('not basalt');
      final url = Uri.parse('${j['url']}');
      address.text = url.hasPort ? '${url.host}:${url.port}' : url.host;
      key.text = '${j['key']}';
      await widget.settings.setServer(address: address.text, key: key.text, name: '${j['name'] ?? ''}');
      await _test();
    } catch (_) {
      setState(() {
        message = Texts.t('set.scan.bad');
        bad = true;
      });
    }
  }

  Future<List<Map<String, String>>> _loadPersonas() async {
    if (!widget.settings.configured) return const [];
    final info = await Api(widget.settings).get('/api/voice/gateway');
    return [for (final p in (info['personas'] as List? ?? const []).whereType<Map>()) {'slug': '${p['slug']}', 'name': '${p['name']}'}];
  }

  /// Switching it on asks Android for the permission; switching it off makes the PC forget the last position it was given.
  Future<void> _switchPlace(bool on) async {
    if (on && !await PlaceFinder.allow()) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(Texts.t('set.place.denied'))));
      return;
    }
    await widget.settings.setSendPlace(on);
    if (!on) {
      try {
        await Api(widget.settings).post('/api/agent/place/forget');
      } on ApiError catch (_) {}
    }
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(Texts.t('set.title'))),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        Text(Texts.t('set.server'), style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        TextField(
          controller: address,
          keyboardType: TextInputType.url,
          autocorrect: false,
          decoration: InputDecoration(labelText: Texts.t('set.address'), helperText: Texts.t('set.address.help'), helperMaxLines: 3, border: const OutlineInputBorder(), prefixIcon: const Icon(Icons.dns_outlined)),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: key,
          obscureText: hideKey,
          autocorrect: false,
          enableSuggestions: false,
          decoration: InputDecoration(
            labelText: Texts.t('set.key'),
            helperText: Texts.t('set.key.help'),
            border: const OutlineInputBorder(),
            prefixIcon: const Icon(Icons.key),
            suffixIcon: IconButton(icon: Icon(hideKey ? Icons.visibility : Icons.visibility_off), onPressed: () => setState(() => hideKey = !hideKey)),
          ),
        ),
        const SizedBox(height: 12),
        Wrap(spacing: 8, runSpacing: 8, children: [
          FilledButton.icon(onPressed: _scan, icon: const Icon(Icons.qr_code_scanner), label: Text(Texts.t('set.scan'))),
          OutlinedButton.icon(onPressed: testing ? null : _test, icon: testing ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.wifi_tethering), label: Text(Texts.t('set.test'))),
          TextButton(onPressed: _save, child: Text(Texts.t('set.save'))),
        ]),
        if (message.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 10), child: Text(message, style: TextStyle(color: bad ? Colors.redAccent : Colors.lightGreenAccent))),
        const SizedBox(height: 12),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(Texts.t('set.tailscale')),
              TextButton.icon(
                onPressed: () async {
                  final app = Uri.parse('tailscale://');
                  if (!await launchUrl(app, mode: LaunchMode.externalApplication)) {
                    await launchUrl(Uri.parse('https://play.google.com/store/apps/details?id=com.tailscale.ipn'), mode: LaunchMode.externalApplication);
                  }
                },
                icon: const Icon(Icons.open_in_new),
                label: Text(Texts.t('set.tailscale.open')),
              ),
            ]),
          ),
        ),
        if (widget.settings.configured) ...[
          const Divider(height: 32),
          Text(Texts.t('set.more'), style: theme.textTheme.titleMedium),
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.auto_fix_high_outlined),
            title: Text(Texts.t('psk.title')),
            subtitle: Text(Texts.t('psk.sub')),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => PersonalSkillsScreen(api: Api(widget.settings), settings: widget.settings, voice: widget.voice))),
          ),
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.extension_outlined),
            title: Text(Texts.t('cap.title')),
            subtitle: Text(Texts.t('cap.sub')),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => CapabilitiesScreen(api: Api(widget.settings)))),
          ),
          const Divider(height: 32),
          Text(Texts.t('opts.title'), style: theme.textTheme.titleMedium),
          const SizedBox(height: 8),
          ServerOptionsBody(prefs: prefs),
        ],
        const Divider(height: 32),
        Text(Texts.t('set.voice'), style: theme.textTheme.titleMedium),
        SwitchListTile(contentPadding: EdgeInsets.zero, title: Text(Texts.t('set.autospeak')), value: widget.settings.autoSpeak, onChanged: (v) => widget.settings.setAutoSpeak(v).then((_) => setState(() {}))),
        TextField(controller: locale, autocorrect: false, onSubmitted: (_) => _save(), decoration: InputDecoration(labelText: Texts.t('set.speechlocale'), hintText: 'it-IT', border: const OutlineInputBorder())),
        const Divider(height: 32),
        Text(Texts.t('av.title'), style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        AvatarVoiceSettings(settings: widget.settings, route: widget.route, loadPersonas: _loadPersonas),
        const Divider(height: 32),
        SwitchListTile(
          contentPadding: EdgeInsets.zero,
          isThreeLine: true,
          title: Text(Texts.t('set.place')),
          subtitle: Text(Texts.t('set.place.help')),
          value: widget.settings.sendPlace,
          onChanged: _switchPlace,
        ),
        const Divider(height: 32),
        DropdownButtonFormField<String>(
          initialValue: widget.settings.language,
          decoration: InputDecoration(labelText: Texts.t('set.language'), border: const OutlineInputBorder()),
          items: [
            DropdownMenuItem(value: '', child: Text(Texts.t('set.language.auto'))),
            const DropdownMenuItem(value: 'it', child: Text('Italiano')),
            const DropdownMenuItem(value: 'en', child: Text('English')),
          ],
          onChanged: (v) => widget.settings.setLanguage(v ?? ''),
        ),
        const SizedBox(height: 12),
        DropdownButtonFormField<String>(
          initialValue: widget.settings.mode,
          decoration: InputDecoration(labelText: Texts.t('set.mode'), border: const OutlineInputBorder()),
          items: [for (final m in const ['plan', 'read_only', 'default', 'auto']) DropdownMenuItem(value: m, child: Text(Texts.t('mode.$m')))],
          onChanged: (v) => v == null ? null : widget.settings.setMode(v).then((_) => setState(() {})),
        ),
      ]),
    );
  }
}

class _ScanScreen extends StatefulWidget {
  const _ScanScreen();
  @override
  State<_ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends State<_ScanScreen> {
  bool done = false;
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(Texts.t('set.scan.title'))),
      body: MobileScanner(
        onDetect: (capture) {
          if (done) return;
          for (final b in capture.barcodes) {
            final text = b.rawValue;
            if (text != null && text.isNotEmpty) {
              done = true;
              Navigator.pop(context, text);
              return;
            }
          }
        },
      ),
    );
  }
}
