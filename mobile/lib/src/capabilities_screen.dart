import 'package:flutter/material.dart';

import 'api.dart';
import 'i18n.dart';

/// The tools the agent can use (MCP servers and skills), switched on and off, and the requests it made to install more: each one
/// shows what the candidates are and is approved (one of them) or rejected here.
class CapabilitiesScreen extends StatefulWidget {
  const CapabilitiesScreen({super.key, required this.api});
  final Api api;
  @override
  State<CapabilitiesScreen> createState() => _CapabilitiesScreenState();
}

class _CapabilitiesScreenState extends State<CapabilitiesScreen> {
  List<Map<String, dynamic>> entries = [];
  List<Map<String, dynamic>> requests = [];
  Set<String> off = {};
  String? error;
  bool loading = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  static List<Map<String, dynamic>> _maps(Object? v) => ((v as List?) ?? const []).whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList();

  Future<void> _load() async {
    setState(() => loading = true);
    try {
      final r = await widget.api.get('/api/capabilities');
      entries = _maps(r['entries']).where((e) => e['kind'] == 'mcp' || e['kind'] == 'skill').toList();
      final s = r['settings'];
      off = {for (final i in ((s is Map ? s['off'] : null) as List? ?? const [])) '$i'};
      requests = _maps((await widget.api.get('/api/capabilities/requests'))['requests']);
      error = null;
    } on ApiError catch (e) {
      error = e.message;
    }
    if (mounted) setState(() => loading = false);
  }

  void _say(String t) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));
  }

  Future<void> _switch(Map<String, dynamic> e, bool on) async {
    final next = {...off};
    on ? next.remove('${e['id']}') : next.add('${e['id']}');
    try {
      await widget.api.post('/api/capabilities/settings', {'off': next.toList()});
    } on ApiError catch (x) {
      _say(x.message);
    }
    _load();
  }

  Future<void> _decide(Map<String, dynamic> r, {int? number}) async {
    try {
      if (number == null) {
        await widget.api.post('/api/capabilities/requests/reject', {'id': r['id']});
      } else {
        await widget.api.post('/api/capabilities/requests/approve', {'id': r['id'], 'number': number});
      }
    } on ApiError catch (x) {
      _say(x.message);
    }
    _load();
  }

  Widget _request(Map<String, dynamic> r) {
    final theme = Theme.of(context);
    final pending = r['status'] == 'pending';
    final candidates = _maps(r['candidates']);
    return Card(
      margin: const EdgeInsets.fromLTRB(12, 6, 12, 6),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${r['need'] ?? ''}', style: theme.textTheme.titleSmall),
          const SizedBox(height: 2),
          Text(Texts.t('cap.status.${r['status']}'), style: theme.textTheme.bodySmall),
          if ('${r['detail'] ?? ''}'.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Text('${r['detail']}', style: theme.textTheme.bodySmall)),
          for (var i = 0; i < candidates.length; i++) ...[
            const Divider(height: 20),
            _candidate(candidates[i], i + 1, pending, r),
          ],
          if (pending) Align(alignment: Alignment.centerRight, child: TextButton(onPressed: () => _decide(r), child: Text(Texts.t('cap.reject')))),
        ]),
      ),
    );
  }

  Widget _candidate(Map<String, dynamic> c, int number, bool pending, Map<String, dynamic> r) {
    final theme = Theme.of(context);
    final card = c['card'] is Map ? Map<String, dynamic>.from(c['card'] as Map) : <String, dynamic>{};
    final pros = ((c['pros'] as List?) ?? const []).map((x) => '+ $x').toList();
    final cons = ((c['cons'] as List?) ?? const []).map((x) => '- $x').toList();
    final lines = ['${c['summary'] ?? card['description'] ?? ''}', ...pros, ...cons, if ('${card['command'] ?? ''}'.isNotEmpty) '${Texts.t('cap.command')}: ${card['command']}'].where((l) => l.trim().isNotEmpty);
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('${card['name'] ?? card['id'] ?? ''}${'${card['package'] ?? ''}'.isEmpty ? '' : ' (${card['package']})'}', style: theme.textTheme.titleSmall),
      Text('${card['kind'] ?? ''} · ${card['source'] ?? ''}', style: theme.textTheme.bodySmall),
      const SizedBox(height: 4),
      for (final l in lines) Padding(padding: const EdgeInsets.only(bottom: 2), child: Text(l, style: theme.textTheme.bodySmall)),
      if (pending) Padding(padding: const EdgeInsets.only(top: 6), child: FilledButton.tonal(onPressed: () => _decide(r, number: number), child: Text(Texts.t('cap.approve')))),
    ]);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final waiting = requests.where((r) => r['status'] == 'pending').toList();
    final past = requests.where((r) => r['status'] != 'pending').take(10).toList();
    return Scaffold(
      appBar: AppBar(title: Text(Texts.t('cap.title')), bottom: loading ? const PreferredSize(preferredSize: Size.fromHeight(2), child: LinearProgressIndicator()) : null),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: [
          if (error != null) Padding(padding: const EdgeInsets.all(16), child: Text(error!)),
          Padding(padding: const EdgeInsets.fromLTRB(16, 12, 16, 4), child: Text(Texts.t('cap.requests'), style: theme.textTheme.titleMedium)),
          Padding(padding: const EdgeInsets.fromLTRB(16, 0, 16, 4), child: Text(Texts.t('cap.requests.help'), style: theme.textTheme.bodySmall)),
          if (error == null && waiting.isEmpty) Padding(padding: const EdgeInsets.all(16), child: Text(Texts.t('cap.requests.none'))),
          for (final r in waiting) _request(r),
          if (past.isNotEmpty) ExpansionTile(title: Text(Texts.t('cap.past')), children: [for (final r in past) _request(r)]),
          const Divider(height: 24),
          Padding(padding: const EdgeInsets.fromLTRB(16, 4, 16, 4), child: Text(Texts.t('cap.tools'), style: theme.textTheme.titleMedium)),
          Padding(padding: const EdgeInsets.fromLTRB(16, 0, 16, 4), child: Text(Texts.t('cap.tools.help'), style: theme.textTheme.bodySmall)),
          if (error == null && entries.isEmpty) Padding(padding: const EdgeInsets.all(16), child: Text(Texts.t('cap.tools.none'))),
          for (final e in entries)
            SwitchListTile(
              title: Text('${e['name'] ?? e['id']}'),
              subtitle: Text('${Texts.t('cap.kind.${e['kind']}')}${'${e['description'] ?? ''}'.isEmpty ? '' : ' · ${e['description']}'}', maxLines: 3, overflow: TextOverflow.ellipsis),
              value: !off.contains('${e['id']}'),
              onChanged: (on) => _switch(e, on),
            ),
        ]),
      ),
    );
  }
}
