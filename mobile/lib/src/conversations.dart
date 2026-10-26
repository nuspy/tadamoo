import 'package:flutter/material.dart';

import 'api.dart';
import 'chat_model.dart';
import 'chat_screen.dart';
import 'i18n.dart';
import 'settings.dart';
import 'voice.dart';

/// The conversations, as a list: the Chat's own, or (Agent) the ones held in the folders added on the PC, grouped by folder.
class ConversationsPane extends StatefulWidget {
  const ConversationsPane({super.key, required this.api, required this.settings, required this.voice, required this.agent});
  final Api api;
  final AppSettings settings;
  final Voice voice;
  final bool agent;

  @override
  State<ConversationsPane> createState() => _ConversationsPaneState();
}

class _ConversationsPaneState extends State<ConversationsPane> {
  List<SessionInfo> sessions = [];
  List<Map<String, dynamic>> projects = [];
  String defaultWorkspace = '';
  String? error;
  bool loading = false;

  @override
  void initState() {
    super.initState();
    widget.settings.addListener(_settingsChanged);
    _load();
  }

  @override
  void dispose() {
    widget.settings.removeListener(_settingsChanged);
    super.dispose();
  }

  void _settingsChanged() => _load();

  bool _same(String a, String b) => a.replaceAll('/', '\\').toLowerCase().replaceAll(RegExp(r'\\+$'), '') == b.replaceAll('/', '\\').toLowerCase().replaceAll(RegExp(r'\\+$'), '');
  bool _isChat(SessionInfo s) => s.workspace.isEmpty || (defaultWorkspace.isNotEmpty && _same(s.workspace, defaultWorkspace));

  Future<void> _load() async {
    if (!widget.settings.configured) return;
    setState(() => loading = true);
    try {
      final r = await widget.api.get('/api/agent/sessions');
      sessions = ((r['sessions'] as List?) ?? const []).whereType<Map>().map((m) => SessionInfo(Map<String, dynamic>.from(m))).where((s) => s.state != 'closed').toList();
      defaultWorkspace = '${r['default_workspace'] ?? ''}';
      if (widget.agent) {
        final p = await widget.api.get('/api/agent/projects');
        projects = ((p['projects'] as List?) ?? const []).whereType<Map>().map((m) => Map<String, dynamic>.from(m)).where((m) => m['exists'] != false).toList();
      }
      error = null;
    } on ApiError catch (e) {
      error = e.message;
    }
    if (mounted) setState(() => loading = false);
  }

  Future<void> _open({SessionInfo? session, String workspace = ''}) async {
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => ChatScreen(api: widget.api, settings: widget.settings, voice: widget.voice, sessionId: session?.id, workspace: workspace)));
    _load();
  }

  Future<void> _new() async {
    if (!widget.agent) return _open();
    if (projects.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(Texts.t('agent.nofolders'))));
      return;
    }
    final folder = await showModalBottomSheet<String>(
      context: context,
      builder: (c) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Padding(padding: const EdgeInsets.all(16), child: Text(Texts.t('agent.pick'), style: Theme.of(context).textTheme.titleMedium)),
          for (final p in projects) ListTile(leading: const Icon(Icons.folder), title: Text('${p['name']}'), subtitle: Text('${p['path']}'), onTap: () => Navigator.pop(c, '${p['path']}')),
        ]),
      ),
    );
    if (folder != null) _open(workspace: folder);
  }

  Future<void> _delete(SessionInfo s) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(content: Text(Texts.t('conv.delete.sure')), actions: [
        TextButton(onPressed: () => Navigator.pop(c, false), child: Text(Texts.t('conv.cancel'))),
        FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(Texts.t('conv.delete'))),
      ]),
    );
    if (ok != true) return;
    try {
      await widget.api.post('/api/agent/sessions/${s.id}/delete');
    } on ApiError catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    }
    _load();
  }

  Widget _row(SessionInfo s) => ListTile(
        leading: Icon(s.busy ? Icons.autorenew : Icons.chat_bubble_outline),
        title: Text(s.title.isEmpty ? Texts.t('conv.untitled') : s.title, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(Texts.t('mode.${s.mode}')),
        trailing: s.favorite ? const Icon(Icons.star, size: 18) : null,
        onTap: () => _open(session: s),
        onLongPress: () => _delete(s),
      );

  @override
  Widget build(BuildContext context) {
    final children = <Widget>[];
    if (!widget.settings.configured) {
      children.add(Padding(padding: const EdgeInsets.all(24), child: Text(Texts.t('setup.first'), textAlign: TextAlign.center)));
    } else if (error != null) {
      children.add(Padding(padding: const EdgeInsets.all(24), child: Column(children: [Text(Texts.t('conv.offline'), textAlign: TextAlign.center), const SizedBox(height: 8), Text(error!, style: Theme.of(context).textTheme.bodySmall), const SizedBox(height: 12), FilledButton(onPressed: _load, child: Text(Texts.t('conv.retry')))])));
    } else if (widget.agent) {
      if (projects.isEmpty) children.add(Padding(padding: const EdgeInsets.all(24), child: Text(Texts.t('agent.nofolders'))));
      for (final p in projects) {
        final mine = sessions.where((s) => !_isChat(s) && _same(s.workspace, '${p['path']}')).toList();
        children.add(Padding(padding: const EdgeInsets.fromLTRB(16, 14, 16, 4), child: Row(children: [const Icon(Icons.folder, size: 18), const SizedBox(width: 8), Expanded(child: Text('${p['name']}', style: Theme.of(context).textTheme.titleSmall)), IconButton(icon: const Icon(Icons.add), tooltip: Texts.t('conv.new'), onPressed: () => _open(workspace: '${p['path']}'))])));
        children.addAll(mine.map(_row));
        if (mine.isEmpty) children.add(Padding(padding: const EdgeInsets.fromLTRB(16, 0, 16, 8), child: Text(Texts.t('conv.empty'), style: Theme.of(context).textTheme.bodySmall)));
      }
    } else {
      final mine = sessions.where(_isChat).toList();
      if (mine.isEmpty) children.add(Padding(padding: const EdgeInsets.all(24), child: Text(Texts.t('conv.empty'), textAlign: TextAlign.center)));
      children.addAll(mine.map(_row));
    }
    return Scaffold(
      appBar: AppBar(title: Text(widget.agent ? Texts.t('agent.folders') : Texts.t('tab.chat')), bottom: loading ? const PreferredSize(preferredSize: Size.fromHeight(2), child: LinearProgressIndicator()) : null),
      body: RefreshIndicator(onRefresh: _load, child: ListView(children: children)),
      floatingActionButton: widget.settings.configured && error == null ? FloatingActionButton.extended(onPressed: _new, icon: const Icon(Icons.add), label: Text(Texts.t('conv.new'))) : null,
    );
  }
}
