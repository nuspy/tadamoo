import 'dart:async';

import 'package:flutter/material.dart';

import 'api.dart';
import 'i18n.dart';

/// One answer of an entry: `value` is what is sent, the label is what the person reads in the app's language.
class ActionOption {
  const ActionOption(this.value, this.label, this.labelEn);
  final String value, label, labelEn;
  String get shown => Texts.language == 'en' ? (labelEn.isEmpty ? label : labelEn) : (label.isEmpty ? labelEn : label);
}

/// Something that waits for the person: a question of a conversation, what a plan asks, a change of a plan, a request of a routine or a
/// security authorization. The PC lists them all (`/api/actions`, serve/actions.py); one that is answered anywhere is not listed any more.
class ActionEntry {
  const ActionEntry({
    required this.id,
    required this.source,
    required this.title,
    this.titleEn = '',
    this.context = '',
    this.text = '',
    this.kind = 'confirm',
    this.options = const [],
    this.free = false,
    this.freeHint = '',
    this.createdAt = 0,
    this.notified = false,
    this.details = const {},
    this.proposed,
  });

  factory ActionEntry.fromJson(Map<String, dynamic> j) => ActionEntry(
        id: '${j['id'] ?? ''}',
        source: '${j['source'] ?? ''}',
        title: '${j['title'] ?? ''}',
        titleEn: '${j['title_en'] ?? ''}',
        context: '${j['context'] ?? ''}',
        text: '${j['text'] ?? ''}',
        kind: '${j['kind'] ?? 'confirm'}',
        free: j['free'] == true,
        freeHint: '${j['free_hint'] ?? ''}',
        createdAt: (j['created_at'] as num?)?.toDouble() ?? 0,
        notified: j['notified'] == true,
        proposed: j['proposed'],
        details: j['details'] is Map ? Map<String, dynamic>.from(j['details'] as Map) : const {},
        options: [
          for (final o in (j['options'] as List?) ?? const [])
            if (o is Map) ActionOption('${o['value']}', '${o['label'] ?? o['value']}', '${o['label_en'] ?? o['label'] ?? o['value']}'),
        ],
      );

  final String id, source, title, titleEn, context, text, kind, freeHint;
  final List<ActionOption> options;
  final bool free, notified;
  final double createdAt;
  final Map<String, dynamic> details;
  final Object? proposed;

  String get question => Texts.language == 'en' ? (titleEn.isEmpty ? title : titleEn) : (title.isEmpty ? titleEn : title);

  /// A card of several questions is answered in its conversation, on the PC.
  bool get isCard => kind == 'card';

  /// What caused it, for a security authorization above all: the orders found in a text, the tool and its call.
  List<MapEntry<String, String>> get causes {
    final out = <MapEntry<String, String>>[];
    void add(String label, Object? v) {
      final s = v is String ? v : (v == null ? '' : '$v');
      if (s.isNotEmpty && s != '{}' && s != '[]') out.add(MapEntry(label, s.length > 1500 ? '${s.substring(0, 1500)}…' : s));
    }

    final facts = details['facts'];
    add(Texts.t('ac.d.instructions'), details['instructions'] ?? (facts is Map ? facts['instructions'] : null));
    if (facts is Map) {
      facts.forEach((k, v) {
        if (k != 'instructions') add('$k', v);
      });
    }
    add(Texts.t('ac.d.tool'), details['tool']);
    add(Texts.t('ac.d.arguments'), details['arguments']);
    add(Texts.t('ac.d.explain'), details['explain']);
    add(Texts.t('ac.d.effect'), details['effect']);
    return out;
  }
}

/// The routes of the list of required actions.
extension ActionsApi on Api {
  Future<List<ActionEntry>> actions({String channel = 'android'}) async {
    final r = await get('/api/actions', query: {'channel': channel});
    return [for (final m in (r['actions'] as List?) ?? const []) if (m is Map) ActionEntry.fromJson(Map<String, dynamic>.from(m))];
  }

  /// Sends the answer to the source of the entry; the first answer wins, a second is an [ApiError] with code `gone`.
  Future<void> answerAction(String id, {String value = '', String text = ''}) => post('/api/actions/${Uri.encodeComponent(id)}/answer', {'value': value, 'text': text});

  /// Says that these entries were shown (a notification), so that they are not announced again on this channel.
  Future<void> markActionsNotified(List<String> ids, {String channel = 'android'}) => post('/api/actions/notified', {'ids': ids, 'channel': channel});
}

/// The list while the app is open: looked for every few seconds, not while the app is in the background (the notifications are the background's way
/// of telling). What is already on the screen was seen by the person, so it is marked as announced and no notification follows.
class ActionsFeed extends ChangeNotifier {
  ActionsFeed(this.api, {this.every = const Duration(seconds: 8)});
  final Api api;
  final Duration every;
  List<ActionEntry> entries = const [];
  String? error;
  bool loaded = false;
  Timer? _timer;
  bool _busy = false;

  int get count => entries.length;

  void start() {
    _timer?.cancel();
    refresh();
    _timer = Timer.periodic(every, (_) => refresh());
  }

  void stop() {
    _timer?.cancel();
    _timer = null;
  }

  Future<void> refresh() async {
    if (_busy || !api.settings.configured) return;
    _busy = true;
    try {
      entries = await api.actions();
      error = null;
      loaded = true;
      final fresh = [for (final e in entries) if (!e.notified) e.id];
      if (fresh.isNotEmpty) {
        try {
          await api.markActionsNotified(fresh);
        } on ApiError catch (_) {/* the next look says it again */}
      }
    } on ApiError catch (e) {
      error = e.message;
    } finally {
      _busy = false;
    }
    notifyListeners();
  }

  /// Sends an answer. Returns null when it was taken, else what to tell the person (an entry answered elsewhere says so and goes).
  Future<String?> answer(ActionEntry e, {String value = '', String text = ''}) async {
    try {
      await api.answerAction(e.id, value: value, text: text);
      entries = [for (final x in entries) if (x.id != e.id) x];
      notifyListeners();
      refresh();
      return null;
    } on ApiError catch (err) {
      if (err.code == 'gone') {
        entries = [for (final x in entries) if (x.id != e.id) x];
        notifyListeners();
        return Texts.t('ac.gone');
      }
      return Texts.t('ac.e.send', [err.message]);
    }
  }

  @override
  void dispose() {
    stop();
    super.dispose();
  }
}

/// The tab: the entries with their buttons and a field for text where the entry takes it, over the Jobs of the agent (a second part of the same tab).
class ActionsScreen extends StatefulWidget {
  const ActionsScreen({super.key, required this.feed, required this.jobs});
  final ActionsFeed feed;
  final Widget jobs;
  @override
  State<ActionsScreen> createState() => _ActionsScreenState();
}

class _ActionsScreenState extends State<ActionsScreen> {
  bool showJobs = false;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
          child: SegmentedButton<bool>(
            showSelectedIcon: false,
            segments: [
              ButtonSegment(value: false, label: Text(Texts.t('ac.title')), icon: const Icon(Icons.inbox_outlined)),
              ButtonSegment(value: true, label: Text(Texts.t('tab.jobs')), icon: const Icon(Icons.schedule_outlined)),
            ],
            selected: {showJobs},
            onSelectionChanged: (s) => setState(() => showJobs = s.first),
          ),
        ),
        Expanded(
          child: showJobs ? widget.jobs : _ActionList(feed: widget.feed),
        ),
      ],
    );
  }
}

class _ActionList extends StatelessWidget {
  const _ActionList({required this.feed});
  final ActionsFeed feed;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: feed,
      builder: (context, _) {
        final theme = Theme.of(context);
        if (feed.error != null && feed.entries.isEmpty) {
          return Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(feed.error!, style: TextStyle(color: theme.colorScheme.error))));
        }
        return RefreshIndicator(
          onRefresh: feed.refresh,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 24),
            children: [
              Text(Texts.t('ac.help'), style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
              const SizedBox(height: 8),
              if (!feed.loaded) Text(Texts.t('ac.loading')) else if (feed.entries.isEmpty) Padding(padding: const EdgeInsets.all(16), child: Text(Texts.t('ac.none'))),
              for (final e in feed.entries) ActionCard(key: ValueKey(e.id), entry: e, onAnswer: (v, t) => feed.answer(e, value: v, text: t)),
            ],
          ),
        );
      },
    );
  }
}

/// One entry: where it comes from, the question, what is behind it, and its answers as buttons of at least 48 dp, with a field when the entry takes text.
class ActionCard extends StatefulWidget {
  const ActionCard({super.key, required this.entry, required this.onAnswer});
  final ActionEntry entry;

  /// Sends the answer: returns null when it was taken, else the text to show.
  final Future<String?> Function(String value, String text) onAnswer;
  @override
  State<ActionCard> createState() => _ActionCardState();
}

class _ActionCardState extends State<ActionCard> {
  late final TextEditingController text = TextEditingController(text: widget.entry.options.isEmpty && widget.entry.proposed != null ? '${widget.entry.proposed}' : '');
  bool busy = false;
  String? message;

  @override
  void dispose() {
    text.dispose();
    super.dispose();
  }

  Future<void> _send(String value) async {
    final written = text.text.trim();
    if (value.isEmpty && written.isEmpty) {
      setState(() => message = Texts.t('ac.e.empty'));
      return;
    }
    setState(() {
      busy = true;
      message = null;
    });
    final r = await widget.onAnswer(value, written);
    if (mounted) {
      setState(() {
        busy = false;
        message = r;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final e = widget.entry, theme = Theme.of(context);
    const button = Size(48, 48);
    final numeric = e.freeHint == 'number';
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('${Texts.t('ac.src.${e.source}')}${e.context.isEmpty ? '' : ' · ${e.context}'}', style: theme.textTheme.labelSmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            const SizedBox(height: 4),
            Text(e.question, style: theme.textTheme.titleMedium),
            if (e.text.isNotEmpty) ...[const SizedBox(height: 6), Text(e.text, maxLines: 12, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodyMedium)],
            if (e.causes.isNotEmpty)
              ExpansionTile(
                tilePadding: EdgeInsets.zero,
                initiallyExpanded: e.causes.any((c) => c.key == Texts.t('ac.d.instructions')),
                title: Text(Texts.t('ac.cause'), style: theme.textTheme.bodyMedium),
                children: [for (final c in e.causes) Align(alignment: Alignment.centerLeft, child: Padding(padding: const EdgeInsets.only(bottom: 6), child: SelectableText.rich(TextSpan(children: [TextSpan(text: '${c.key}: ', style: const TextStyle(fontWeight: FontWeight.bold)), TextSpan(text: c.value, style: const TextStyle(fontFamily: 'monospace'))]))))],
              ),
            if (e.free) ...[
              const SizedBox(height: 8),
              TextField(
                controller: text,
                enabled: !busy,
                keyboardType: numeric ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.multiline,
                minLines: 1,
                maxLines: numeric ? 1 : 4,
                maxLength: e.freeHint == 'code' ? 32 : null,
                decoration: InputDecoration(border: const OutlineInputBorder(), hintText: e.kind == 'task' ? Texts.t('ac.note') : (e.options.isEmpty ? Texts.t('ac.field.text') : Texts.t('ac.or.write'))),
              ),
            ],
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final o in e.options) FilledButton.tonal(style: FilledButton.styleFrom(minimumSize: button), onPressed: busy ? null : () => _send(o.value), child: Text(o.shown)),
                if (e.free && e.kind != 'task') FilledButton(style: FilledButton.styleFrom(minimumSize: button), onPressed: busy ? null : () => _send(''), child: Text(Texts.t('ac.send'))),
              ],
            ),
            if (e.isCard) Text(Texts.t('ac.card'), style: theme.textTheme.bodySmall),
            if (message != null) Padding(padding: const EdgeInsets.only(top: 6), child: Text(message!, style: TextStyle(color: theme.colorScheme.error))),
          ],
        ),
      ),
    );
  }
}
