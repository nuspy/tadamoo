import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'api.dart';
import 'i18n.dart';

/// The register of the PC's work, read only: the open tasks of every project with their state, what is done and what is missing, and the branch and
/// commits that carry them (a tap opens the page of the project's remote when it has one). Written and changed on the PC.
class TasksScreen extends StatefulWidget {
  const TasksScreen({super.key, required this.api});
  final Api api;
  @override
  State<TasksScreen> createState() => _TasksScreenState();
}

class _TasksScreenState extends State<TasksScreen> {
  List<Map<String, dynamic>> projects = [];
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
      final r = await widget.api.get('/api/register', query: {'project': 'all'});
      projects = ((r['projects'] as List?) ?? const []).whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList();
      error = null;
    } on ApiError catch (e) {
      error = e.message;
    }
    if (mounted) setState(() => loading = false);
  }

  Future<void> _open(String url) async {
    final uri = Uri.tryParse(url);
    if (uri != null) await launchUrl(uri, mode: LaunchMode.externalApplication);
  }

  Widget _gitChip(IconData icon, String text, String? url) {
    final label = Text(text, style: const TextStyle(fontFamily: 'monospace'));
    return url == null ? Chip(avatar: Icon(icon, size: 18), label: label) : ActionChip(avatar: Icon(icon, size: 18), label: label, onPressed: () => _open(url));
  }

  Widget _task(Map<String, dynamic> project, Map<String, dynamic> t) {
    final theme = Theme.of(context);
    final links = t['links'] is Map ? Map<String, dynamic>.from(t['links'] as Map) : <String, dynamic>{};
    final commits = ((links['commits'] as List?) ?? const []).whereType<String>().toList();
    final branch = (links['git_branch'] ?? '').toString();
    final repo = (project['repo_url'] ?? '').toString();
    final plan = t['plan'] is Map ? Map<String, dynamic>.from(t['plan'] as Map) : null;
    final title = (plan?['title'] ?? t['title']).toString();
    final state = (t['state'] ?? 'queued').toString();
    final number = ((t['num'] as num?) ?? 0).toInt();
    final depth = ((t['depth'] as num?) ?? 0).toInt().clamp(0, 4);
    final description = (t['description'] ?? '').toString();
    final done = (t['done'] ?? '').toString();
    final missing = (t['missing'] ?? '').toString();
    final branchPath = branch.split('/').map(Uri.encodeComponent).join('/');
    return Card(
      margin: EdgeInsets.fromLTRB(12.0 + 12.0 * depth, 4, 12, 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            if (number > 0) Padding(padding: const EdgeInsets.only(right: 6), child: Text('#$number', style: theme.textTheme.bodySmall)),
            Expanded(child: Text(title, style: theme.textTheme.titleSmall)),
            const SizedBox(width: 8),
            Chip(label: Text(Texts.t('tasks.s.$state')), visualDensity: VisualDensity.compact),
          ]),
          if (description.isNotEmpty && plan == null) Padding(padding: const EdgeInsets.only(top: 4), child: Text(description)),
          if (done.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Text(Texts.t('tasks.done', [done]), style: theme.textTheme.bodySmall)),
          if (missing.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 2), child: Text(Texts.t('tasks.missing', [missing]), style: theme.textTheme.bodySmall)),
          if (branch.isNotEmpty || commits.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Wrap(spacing: 8, runSpacing: 4, children: [
                if (branch.isNotEmpty) _gitChip(Icons.account_tree_outlined, branch, repo.isEmpty ? null : '$repo/tree/$branchPath'),
                for (final c in commits) _gitChip(Icons.commit, c.substring(0, math.min(8, c.length)), repo.isEmpty ? null : '$repo/commit/$c'),
              ]),
            ),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final shown = [for (final p in projects) if (((p['tasks'] as List?) ?? const []).isNotEmpty) p];
    return Scaffold(
      appBar: AppBar(title: Text(Texts.t('tasks.title')), bottom: loading ? const PreferredSize(preferredSize: Size.fromHeight(2), child: LinearProgressIndicator()) : null),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: [
          Padding(padding: const EdgeInsets.fromLTRB(16, 8, 16, 0), child: Text(Texts.t('tasks.readonly'), style: theme.textTheme.bodySmall)),
          if (error != null) Padding(padding: const EdgeInsets.all(24), child: Text(error!)),
          if (error == null && shown.isEmpty) Padding(padding: const EdgeInsets.all(24), child: Text(Texts.t('tasks.empty'), textAlign: TextAlign.center)),
          for (final p in shown) ...[
            Padding(padding: const EdgeInsets.fromLTRB(16, 12, 16, 4), child: Text('${p['name'] ?? p['path']}', style: theme.textTheme.titleMedium)),
            for (final t in ((p['tasks'] as List).whereType<Map>())) _task(p, Map<String, dynamic>.from(t)),
          ],
          const SizedBox(height: 24),
        ]),
      ),
    );
  }
}
