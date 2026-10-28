import 'dart:convert';

import 'package:flutter/material.dart';

import 'api.dart';
import 'chat_model.dart';
import 'chat_screen.dart';
import 'files.dart';
import 'i18n.dart';
import 'settings.dart';
import 'voice.dart';

const _template = 'from basalt_skill import tool\n\ndef main(params):\n    return tool("read_file", path="README.md")\n';

/// The person's skills: listed, read, made or imported on the phone, and run in a new conversation. A skill is usable as soon as it is saved.
class PersonalSkillsScreen extends StatefulWidget {
  const PersonalSkillsScreen({super.key, required this.api, required this.settings, required this.voice});
  final Api api;
  final AppSettings settings;
  final Voice voice;
  @override
  State<PersonalSkillsScreen> createState() => _PersonalSkillsScreenState();
}

class _PersonalSkillsScreenState extends State<PersonalSkillsScreen> {
  List<Map<String, dynamic>> skills = [];
  String? error;
  bool loading = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => loading = true);
    try {
      final r = await widget.api.get('/api/personal-skills');
      skills = ((r['skills'] as List?) ?? const []).whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList();
      error = null;
    } on ApiError catch (e) {
      error = e.message;
    }
    if (mounted) setState(() => loading = false);
  }

  void _say(String t) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));
  }

  Future<void> _make() async {
    final name = TextEditingController();
    final description = TextEditingController();
    final script = TextEditingController(text: _template);
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(Texts.t('psk.new')),
        content: SingleChildScrollView(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(controller: name, decoration: InputDecoration(labelText: Texts.t('psk.name'))),
            TextField(controller: description, decoration: InputDecoration(labelText: Texts.t('psk.description'))),
            TextField(controller: script, minLines: 5, maxLines: 12, autocorrect: false, enableSuggestions: false, style: const TextStyle(fontFamily: 'monospace', fontSize: 12), decoration: InputDecoration(labelText: Texts.t('psk.script'), helperText: Texts.t('psk.script.help'), helperMaxLines: 4)),
          ]),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(Texts.t('conv.cancel'))),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(Texts.t('psk.save'))),
        ],
      ),
    );
    final spec = {'name': name.text.trim(), 'description': description.text.trim(), 'params': <Map<String, dynamic>>[]};
    final text = script.text;
    name.dispose();
    description.dispose();
    script.dispose();
    if (ok != true) return;
    try {
      await widget.api.post('/api/personal-skills', {'skill': spec, 'script': text});
    } on ApiError catch (e) {
      _say(e.message);
    }
    _load();
  }

  /// A `.pskill` file from the phone: the file's bytes go to the PC in base64.
  Future<void> _import() async {
    try {
      final got = await Pickers.local();
      if (got.isEmpty) return;
      final bytes = await got.first.bytes();
      await widget.api.post('/api/personal-skills/import', {'data': base64Encode(bytes)});
      _say(Texts.t('psk.imported'));
    } on ApiError catch (e) {
      _say(e.message);
    } catch (_) {
      _say(Texts.t('psk.import.bad'));
    }
    _load();
  }

  Future<void> _open(Map<String, dynamic> s) async {
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => _SkillScreen(api: widget.api, settings: widget.settings, voice: widget.voice, slug: '${s['slug']}')));
    _load();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(Texts.t('psk.title')),
        actions: [
          IconButton(icon: const Icon(Icons.file_open_outlined), tooltip: Texts.t('psk.import'), onPressed: _import),
          IconButton(icon: const Icon(Icons.add), tooltip: Texts.t('psk.new'), onPressed: _make),
        ],
        bottom: loading ? const PreferredSize(preferredSize: Size.fromHeight(2), child: LinearProgressIndicator()) : null,
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: [
          Padding(padding: const EdgeInsets.fromLTRB(16, 12, 16, 8), child: Text(Texts.t('psk.help'), style: theme.textTheme.bodySmall)),
          if (error != null) Padding(padding: const EdgeInsets.all(16), child: Text(error!)),
          if (error == null && skills.isEmpty) Padding(padding: const EdgeInsets.all(24), child: Text(Texts.t('psk.empty'), textAlign: TextAlign.center)),
          for (final s in skills)
            ListTile(
              leading: const Icon(Icons.auto_fix_high_outlined),
              title: Text('${s['name']}'),
              subtitle: Text('${s['description'] ?? ''}', maxLines: 2, overflow: TextOverflow.ellipsis),
              trailing: const Icon(Icons.chevron_right),
              onTap: () => _open(s),
            ),
        ]),
      ),
    );
  }
}

/// One skill: what it asks for, its text, a button to run it and one to delete it.
class _SkillScreen extends StatefulWidget {
  const _SkillScreen({required this.api, required this.settings, required this.voice, required this.slug});
  final Api api;
  final AppSettings settings;
  final Voice voice;
  final String slug;
  @override
  State<_SkillScreen> createState() => _SkillScreenState();
}

class _SkillScreenState extends State<_SkillScreen> {
  Map<String, dynamic> skill = {};
  final Map<String, TextEditingController> fields = {};
  final Map<String, bool> flags = {};
  String? error;
  bool running = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    for (final f in fields.values) {
      f.dispose();
    }
    super.dispose();
  }

  List<Map<String, dynamic>> get params => ((skill['params'] as List?) ?? const []).whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList();

  Future<void> _load() async {
    try {
      final r = await widget.api.get('/api/personal-skills/${widget.slug}');
      skill = Map<String, dynamic>.from(r['skill'] as Map);
      for (final p in params) {
        final name = '${p['name']}';
        final d = p['default'];
        if (p['type'] == 'bool') {
          flags[name] = d == true;
        } else {
          fields.putIfAbsent(name, () => TextEditingController(text: d == null ? '' : '$d'));
        }
      }
      error = null;
    } on ApiError catch (e) {
      error = e.message;
    }
    if (mounted) setState(() {});
  }

  void _say(String t) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));
  }

  /// Makes a conversation in the Chat's own folder, runs the skill in it with the values, and opens it.
  Future<void> _run() async {
    setState(() => running = true);
    final values = <String, dynamic>{};
    for (final p in params) {
      final name = '${p['name']}';
      if (p['type'] == 'bool') {
        values[name] = flags[name] == true;
      } else {
        final text = fields[name]?.text.trim() ?? '';
        if (text.isNotEmpty) values[name] = text;
      }
    }
    try {
      final made = await widget.api.post('/api/agent/sessions', NewChat(mode: widget.settings.mode).body());
      final sid = '${(made['session'] as Map)['id']}';
      await widget.api.post('/api/agent/sessions/$sid/skill', {'slug': widget.slug, 'values': values});
      if (!mounted) return;
      await Navigator.of(context).push(MaterialPageRoute(builder: (_) => ChatScreen(api: widget.api, settings: widget.settings, voice: widget.voice, sessionId: sid)));
    } on ApiError catch (e) {
      _say(e.message);
    }
    if (mounted) setState(() => running = false);
  }

  Future<void> _delete() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(content: Text(Texts.t('psk.delete.sure', ['${skill['name']}'])), actions: [
        TextButton(onPressed: () => Navigator.pop(c, false), child: Text(Texts.t('conv.cancel'))),
        FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(Texts.t('conv.delete'))),
      ]),
    );
    if (ok != true) return;
    try {
      await widget.api.post('/api/personal-skills/${widget.slug}/delete');
      if (mounted) Navigator.pop(context);
    } on ApiError catch (e) {
      _say(e.message);
    }
  }

  Widget _field(Map<String, dynamic> p) {
    final name = '${p['name']}';
    final label = '${p['label'] ?? name}';
    final help = '${p['explanation'] ?? ''}';
    final type = '${p['type'] ?? 'text'}';
    if (type == 'bool') {
      return SwitchListTile(contentPadding: EdgeInsets.zero, title: Text(label), subtitle: help.isEmpty ? null : Text(help), value: flags[name] == true, onChanged: (v) => setState(() => flags[name] = v));
    }
    final choices = ((p['choices'] as List?) ?? const []).map((c) => '$c').toList();
    final kind = Texts.t('psk.type.$type');
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: type == 'choice' && choices.isNotEmpty
          ? DropdownButtonFormField<String>(
              initialValue: choices.contains(fields[name]?.text) ? fields[name]!.text : null,
              isExpanded: true,
              decoration: InputDecoration(labelText: label, helperText: help.isEmpty ? null : help, helperMaxLines: 3, border: const OutlineInputBorder()),
              items: [for (final c in choices) DropdownMenuItem(value: c, child: Text(c))],
              onChanged: (v) => fields[name]?.text = v ?? '',
            )
          : TextField(
              controller: fields[name],
              minLines: type == 'list' ? 2 : 1,
              maxLines: type == 'list' ? 5 : 1,
              keyboardType: type == 'number' ? const TextInputType.numberWithOptions(decimal: true, signed: true) : TextInputType.text,
              decoration: InputDecoration(labelText: label, helperText: help.isEmpty ? kind : '$help ($kind)', helperMaxLines: 3, border: const OutlineInputBorder()),
            ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: Text('${skill['name'] ?? ''}'), actions: [IconButton(icon: const Icon(Icons.delete_outline), tooltip: Texts.t('conv.delete'), onPressed: skill.isEmpty ? null : _delete)]),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        if (error != null) Text(error!),
        if ('${skill['description'] ?? ''}'.isNotEmpty) Padding(padding: const EdgeInsets.only(bottom: 12), child: Text('${skill['description']}')),
        for (final p in params) _field(p),
        FilledButton.icon(
          style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
          onPressed: running || skill.isEmpty ? null : _run,
          icon: running ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.play_arrow),
          label: Text(Texts.t('psk.run')),
        ),
        Padding(padding: const EdgeInsets.only(top: 6), child: Text(Texts.t('psk.run.help'), style: theme.textTheme.bodySmall)),
        const Divider(height: 32),
        Text(Texts.t('psk.script'), style: theme.textTheme.titleSmall),
        const SizedBox(height: 6),
        Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(color: theme.cardColor, borderRadius: BorderRadius.circular(8)),
          child: SelectableText('${skill['script'] ?? ''}', style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
        ),
      ]),
    );
  }
}
