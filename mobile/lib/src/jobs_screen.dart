import 'package:flutter/material.dart';

import 'api.dart';
import 'i18n.dart';

/// The recurring jobs of the agent: switched on and off and started from the phone; written and changed on the PC.
class JobsScreen extends StatefulWidget {
  const JobsScreen({super.key, required this.api});
  final Api api;
  @override
  State<JobsScreen> createState() => _JobsScreenState();
}

class _JobsScreenState extends State<JobsScreen> {
  List<Map<String, dynamic>> jobs = [];
  List<Map<String, dynamic>> pending = [];
  String? error;
  bool loading = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (!widget.api.settings.configured) return;
    setState(() => loading = true);
    try {
      final r = await widget.api.get('/api/routines');
      jobs = ((r['routines'] as List?) ?? const []).whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList();
      pending = ((r['pending'] as List?) ?? const []).whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList();
      error = null;
    } on ApiError catch (e) {
      error = e.message;
    }
    if (mounted) setState(() => loading = false);
  }

  String _when(num? ts) {
    if (ts == null || ts == 0) return '-';
    final d = DateTime.fromMillisecondsSinceEpoch((ts * 1000).round());
    return '${d.day.toString().padLeft(2, '0')}/${d.month.toString().padLeft(2, '0')} ${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}';
  }

  Future<void> _toggle(Map<String, dynamic> j) async {
    try {
      await widget.api.post('/api/routines/${j['id']}/${j['enabled'] == true ? 'disable' : 'enable'}');
    } on ApiError catch (e) {
      _say(e.message);
    }
    _load();
  }

  Future<void> _run(Map<String, dynamic> j) async {
    try {
      await widget.api.post('/api/routines/${j['id']}/run');
      _say(Texts.t('jobs.started', ['${j['name']}']));
    } on ApiError catch (e) {
      _say(e.message);
    }
    _load();
  }

  Future<void> _dismiss(Map<String, dynamic> p) async {
    try {
      await widget.api.post('/api/routines/${p['id']}/dismiss');
    } on ApiError catch (e) {
      _say(e.message);
    }
    _load();
  }

  void _say(String t) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(Texts.t('jobs.title')), bottom: loading ? const PreferredSize(preferredSize: Size.fromHeight(2), child: LinearProgressIndicator()) : null),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: [
          if (error != null) Padding(padding: const EdgeInsets.all(24), child: Text(error!)),
          if (error == null && jobs.isEmpty) Padding(padding: const EdgeInsets.all(24), child: Text(Texts.t('jobs.empty'), textAlign: TextAlign.center)),
          if (pending.isNotEmpty) Padding(padding: const EdgeInsets.fromLTRB(16, 8, 16, 0), child: Text(Texts.t('jobs.pending'), style: theme.textTheme.titleSmall)),
          for (final p in pending)
            Card(
              margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('${p['name']} · ${_when(p['created'] as num?)}', style: theme.textTheme.bodySmall),
                  Text('${p['question']}'),
                  if (((p['options'] as List?) ?? const []).isNotEmpty) Text(Texts.t('jobs.options', [((p['options'] as List).join(' / '))]), style: theme.textTheme.bodySmall),
                  Align(alignment: Alignment.centerRight, child: TextButton(onPressed: () => _dismiss(p), child: Text(Texts.t('jobs.dismiss')))),
                ]),
              ),
            ),
          for (final j in jobs)
            Card(
              margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Row(children: [
                    Expanded(child: Text('${j['name']}', style: theme.textTheme.titleSmall)),
                    Switch(value: j['enabled'] == true, onChanged: (_) => _toggle(j)),
                  ]),
                  Text('${j['schedule']} · ${j['enabled'] == true ? Texts.t('jobs.next', [_when(j['next_run'] as num?)]) : Texts.t('jobs.off')}', style: theme.textTheme.bodySmall),
                  Text((j['last_status'] ?? '').toString().isEmpty ? Texts.t('jobs.never') : Texts.t('jobs.last', ['${j['last_status']} ${_when(j['last_run'] as num?)}']), style: theme.textTheme.bodySmall),
                  if ((j['skill'] ?? '').toString().isNotEmpty) Text('skill: ${j['skill']}', style: theme.textTheme.bodySmall),
                  Align(alignment: Alignment.centerRight, child: TextButton.icon(onPressed: () => _run(j), icon: const Icon(Icons.play_arrow), label: Text(Texts.t('jobs.run')))),
                ]),
              ),
            ),
        ]),
      ),
    );
  }
}
