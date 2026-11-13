import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'i18n.dart';

/// A choice of a question: `value` is what the plan gets and is never shown; the label is what the person reads, in the app's language.
class HumanChoice {
  const HumanChoice(this.value, this.label, this.labelEn);
  final String value, label, labelEn;
  String get shown => Texts.language == 'en' ? (labelEn.isEmpty ? label : labelEn) : (label.isEmpty ? labelEn : label);
}

/// What a plan asks the person: to approve what it shows (yes or no), or to give a value. The PC lists these for all the plans (`/api/plans/human`).
class HumanRequest {
  const HumanRequest({
    required this.planId,
    required this.planTitle,
    required this.nodeId,
    required this.title,
    required this.ask,
    required this.prompt,
    this.promptEn = '',
    this.help = '',
    this.helpEn = '',
    this.content = '',
    this.kind = 'text',
    this.choices = const [],
    this.index = 0,
    this.proposed,
  });

  factory HumanRequest.fromJson(Map<String, dynamic> j) => HumanRequest(
        planId: '${j['plan_id'] ?? ''}',
        planTitle: '${j['plan_title'] ?? ''}',
        nodeId: '${j['node_id'] ?? ''}',
        title: '${j['title'] ?? ''}',
        ask: '${j['ask'] ?? 'value'}',
        prompt: '${j['prompt'] ?? ''}',
        promptEn: '${j['prompt_en'] ?? ''}',
        help: '${j['help'] ?? ''}',
        helpEn: '${j['help_en'] ?? ''}',
        content: '${j['content'] ?? ''}',
        kind: '${j['kind'] ?? 'text'}',
        index: (j['index'] as num?)?.toInt() ?? 0,
        proposed: j['proposed'],
        choices: [
          for (final c in (j['choices'] as List?) ?? const [])
            if (c is Map) HumanChoice('${c['value']}', '${c['label'] ?? c['value']}', '${c['label_en'] ?? c['label'] ?? c['value']}') else HumanChoice('$c', '$c', '$c'),
        ],
      );

  final String planId, planTitle, nodeId, title, ask, prompt, promptEn, help, helpEn, content, kind;
  final List<HumanChoice> choices;
  final int index;

  /// What the plan proposes, or what was answered the last time.
  final Object? proposed;
  bool get approve => ask == 'approve';

  String get question => Texts.language == 'en' ? (promptEn.isEmpty ? prompt : promptEn) : (prompt.isEmpty ? promptEn : prompt);
  String get explanation => Texts.language == 'en' ? (helpEn.isEmpty ? help : helpEn) : (help.isEmpty ? helpEn : help);
}

/// The requests of one plan that are asking now, in the order of the plan: one path of questions.
class HumanGroup {
  HumanGroup(this.planId, this.planTitle, this.items);
  final String planId, planTitle;
  final List<HumanRequest> items;
  String get ids => items.map((r) => r.nodeId).join('|');

  /// The first plan that has requests asking and that `skip` does not hold back, or null.
  static HumanGroup? first(List<HumanRequest> all, bool Function(String planId) skip) {
    for (final r in all) {
      if (skip(r.planId)) continue;
      final items = [for (final x in all) if (x.planId == r.planId) x]..sort((a, b) => a.index.compareTo(b.index));
      return HumanGroup(r.planId, r.planTitle, items);
    }
    return null;
  }
}

/// The path through the questions: step i of n is a question, step n is the summary. `answers` start with what the plan proposes.
class HumanPath {
  HumanPath(this.items, [Map<String, Object?>? before])
      : answers = {
          for (final r in items)
            if (before != null && before.containsKey(r.nodeId)) r.nodeId: before[r.nodeId] else if (r.proposed != null) r.nodeId: r.proposed,
        };
  final List<HumanRequest> items;
  final Map<String, Object?> answers;
  int step = 0;
  bool fromSummary = false;

  bool get inSummary => step >= items.length;

  /// A word of the page for what is wrong with an answer, or null when it is good.
  String? problem(HumanRequest r) {
    final v = answers[r.nodeId];
    if (r.approve) return v is bool ? null : 'hr.need.yesno';
    final t = '${v ?? ''}'.trim();
    if (r.kind == 'choice') return r.choices.any((c) => c.value == t) ? null : 'hr.need.choice';
    if (t.isEmpty) return 'hr.need';
    if (r.kind == 'number' && num.tryParse(t) == null) return 'hr.need.number';
    return null;
  }

  /// Goes on if the answer is good (after a change made from the summary, straight back to it); else says what is wrong.
  String? next() {
    if (inSummary) return null;
    final bad = problem(items[step]);
    if (bad != null) return bad;
    final r = items[step];
    if (!r.approve) answers[r.nodeId] = '${answers[r.nodeId]}'.trim();
    step = fromSummary ? items.length : step + 1;
    fromSummary = false;
    return null;
  }

  void prev() {
    if (step > 0) step -= 1;
    fromSummary = false;
  }

  void edit(int i) {
    step = i;
    fromSummary = true;
  }

  int get firstMissing => items.indexWhere((r) => problem(r) != null);

  /// The answers as the PC takes them: step id -> `true`/`false` or the text.
  Map<String, Object> get payload => {for (final r in items) r.nodeId: answers[r.nodeId] as Object};

  String shown(HumanRequest r) {
    final v = answers[r.nodeId];
    if (r.approve) return v == true ? Texts.t('hr.answer.yes') : v == false ? Texts.t('hr.answer.no') : '';
    if (r.kind == 'choice') {
      for (final c in r.choices) {
        if (c.value == v) return c.shown;
      }
    }
    return '${v ?? ''}';
  }

  /// The path when the questions that are asking changed under it: the answers given stay, the step stays where it can.
  HumanPath reconcile(List<HumanRequest> now) {
    final fresh = HumanPath(now, answers);
    fresh.step = step < now.length ? step : now.length;
    return fresh;
  }
}

enum HumanOutcome { answered, later }

/// Looks at the PC's list every few seconds and hands the first plan that was not left for later to `show`, one window at a time. Later leaves a plan's questions for
/// `snooze` with the answers kept; an answered plan is gone from the list, and the next is looked for at once.
class HumanPoller {
  HumanPoller({
    required this.fetch,
    required this.show,
    this.every = const Duration(seconds: 4),
    this.snooze = const Duration(minutes: 10),
    DateTime Function()? clock,
  }) : clock = clock ?? DateTime.now;

  final Future<List<HumanRequest>> Function() fetch;
  final Future<HumanOutcome> Function(HumanGroup group, HumanPath? draft) show;
  final Duration every, snooze;
  final DateTime Function() clock;
  final Map<String, DateTime> _snoozed = {};
  final Map<String, HumanPath> drafts = {};
  Timer? _timer;
  bool _busy = false;

  bool get running => _timer != null;

  void start() {
    _timer ??= Timer.periodic(every, (_) => refresh());
    refresh();
  }

  void stop() {
    _timer?.cancel();
    _timer = null;
  }

  Future<void> refresh() async {
    if (_busy) return;
    _busy = true;
    var again = false;
    try {
      final all = await fetch();
      final now = clock();
      final group = HumanGroup.first(all, (id) => _snoozed[id]?.isAfter(now) ?? false);
      if (group != null && running) {
        final outcome = await show(group, drafts[group.planId]);
        if (outcome == HumanOutcome.later) {
          _snoozed[group.planId] = clock().add(snooze);
        } else {
          drafts.remove(group.planId);
        }
        again = true;
      }
    } catch (_) {
      // the PC is not reachable now: the next look tries again
    } finally {
      _busy = false;
    }
    if (again && running) await refresh();
  }
}

/// The window: one question at a time ("Question 3 of 22"), Back and Next (and a swipe, and the arrow keys of a keyboard), then the summary of all the answers
/// with Edit for each and one Send for all of them. Later leaves it with the answers kept in `path`.
class HumanWizardDialog extends StatefulWidget {
  const HumanWizardDialog({super.key, required this.group, required this.send, this.draft, this.onPath});
  final HumanGroup group;
  final HumanPath? draft;

  /// Sends all the answers together to the PC.
  final Future<void> Function(Map<String, Object> answers) send;

  /// Called with the path as it is, so that Later can keep it.
  final void Function(HumanPath path)? onPath;

  @override
  State<HumanWizardDialog> createState() => _HumanWizardDialogState();
}

class _HumanWizardDialogState extends State<HumanWizardDialog> {
  late HumanPath path;
  final text = TextEditingController();
  String? error;
  bool sending = false;

  @override
  void initState() {
    super.initState();
    final kept = widget.draft;
    path = kept != null && kept.items.map((r) => r.nodeId).join('|') == widget.group.ids ? kept : HumanPath(widget.group.items, kept?.answers);
    widget.onPath?.call(path);
    _syncText();
  }

  @override
  void dispose() {
    text.dispose();
    super.dispose();
  }

  HumanRequest? get current => path.inSummary ? null : path.items[path.step];

  void _syncText() {
    final r = current;
    text.text = r == null || r.approve || r.kind == 'choice' ? '' : '${path.answers[r.nodeId] ?? ''}';
  }

  void _go(void Function() move) {
    setState(() {
      error = null;
      move();
      _syncText();
    });
    widget.onPath?.call(path);
  }

  void _next() {
    final r = current;
    if (r != null && !r.approve && r.kind != 'choice') path.answers[r.nodeId] = text.text;
    final bad = path.next();
    setState(() {
      error = bad == null ? null : Texts.t(bad);
      if (bad == null) _syncText();
    });
    widget.onPath?.call(path);
  }

  Future<void> _send() async {
    final bad = path.firstMissing;
    if (bad >= 0) {
      _go(() => path.edit(bad));
      setState(() => error = Texts.t(path.problem(path.items[bad])!));
      return;
    }
    setState(() {
      sending = true;
      error = null;
    });
    try {
      await widget.send(path.payload);
      if (mounted) Navigator.of(context).pop(HumanOutcome.answered);
    } on ApiError catch (e) {
      if (mounted) {
        setState(() {
          sending = false;
          error = Texts.t('hr.err', [e.message]);
        });
      }
    }
  }

  void _later() => Navigator.of(context).pop(HumanOutcome.later);

  Widget _big(String label, VoidCallback? onPressed, {bool primary = false}) {
    final style = ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48)));
    return primary ? FilledButton(onPressed: onPressed, style: style, child: Text(label)) : OutlinedButton(onPressed: onPressed, style: style, child: Text(label));
  }

  Widget _field(HumanRequest r) {
    if (r.approve) {
      final v = path.answers[r.nodeId];
      Widget one(String label, bool yes) {
        final on = v == yes;
        final style = ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48)));
        void pick() => setState(() {
              path.answers[r.nodeId] = yes;
              error = null;
            });
        return Expanded(child: Semantics(selected: on, child: on ? FilledButton(onPressed: pick, style: style, child: Text(label)) : OutlinedButton(onPressed: pick, style: style, child: Text(label))));
      }

      return Row(children: [one(Texts.t('hr.yes'), true), const SizedBox(width: 8), one(Texts.t('hr.no'), false)]);
    }
    if (r.kind == 'choice') {
      final v = '${path.answers[r.nodeId] ?? ''}';
      return DropdownButtonFormField<String>(
        key: ValueKey('choice-${r.nodeId}'),
        initialValue: r.choices.any((c) => c.value == v) ? v : null,
        hint: Text(Texts.t('hr.choice')),
        isExpanded: true,
        items: [for (final c in r.choices) DropdownMenuItem(value: c.value, child: Text(c.shown))],
        onChanged: (x) => setState(() {
          path.answers[r.nodeId] = x;
          error = null;
        }),
      );
    }
    return TextField(
      key: ValueKey('text-${r.nodeId}'),
      controller: text,
      autofocus: true,
      keyboardType: r.kind == 'number' ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
      decoration: InputDecoration(border: const OutlineInputBorder(), labelText: Texts.t('hr.value')),
      onChanged: (x) => path.answers[r.nodeId] = x,
      onSubmitted: (_) => _next(),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    const gap = SizedBox(height: 10);
    final r = current;
    final n = path.items.length;
    final children = <Widget>[Text(widget.group.planTitle, style: theme.textTheme.bodySmall)];
    if (r != null) {
      children.addAll([
        Row(children: [
          Text(Texts.t('hr.q', [path.step + 1, n]), style: theme.textTheme.bodySmall),
          const SizedBox(width: 12),
          Expanded(child: LinearProgressIndicator(value: (path.step + 1) / n)),
        ]),
        gap,
        Text(r.question, style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w600)),
        if (r.explanation.isNotEmpty) ...[const SizedBox(height: 6), Text(r.explanation, style: theme.textTheme.bodyMedium?.copyWith(color: theme.hintColor))],
        if (r.content.isNotEmpty) ...[
          gap,
          Container(
            constraints: const BoxConstraints(maxHeight: 180),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(border: Border.all(color: theme.dividerColor), borderRadius: BorderRadius.circular(6)),
            child: SingleChildScrollView(child: SelectableText(r.content)),
          ),
        ],
        gap,
        _field(r),
        if (error != null) ...[gap, Text(error!, style: TextStyle(color: theme.colorScheme.error))],
        gap,
        Row(children: [
          Expanded(child: _big(Texts.t('hr.prev'), path.step == 0 ? null : () => _go(path.prev))),
          const SizedBox(width: 8),
          Expanded(child: _big(Texts.t('hr.next'), _next, primary: true)),
        ]),
      ]);
    } else {
      children.addAll([
        gap,
        Text(Texts.t('hr.review'), style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w600)),
        const SizedBox(height: 6),
        Text(Texts.t('hr.review.help'), style: theme.textTheme.bodyMedium?.copyWith(color: theme.hintColor)),
        gap,
        for (var i = 0; i < n; i++)
          Card(
            margin: const EdgeInsets.symmetric(vertical: 3),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 6, 4, 6),
              child: Row(children: [
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(path.items[i].question, style: theme.textTheme.bodySmall),
                    Text(path.problem(path.items[i]) != null ? Texts.t(path.problem(path.items[i])!) : path.shown(path.items[i]), style: const TextStyle(fontWeight: FontWeight.w600)),
                  ]),
                ),
                TextButton(
                  style: ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48))),
                  onPressed: sending ? null : () => _go(() => path.edit(i)),
                  child: Text(Texts.t('hr.edit')),
                ),
              ]),
            ),
          ),
        if (error != null) ...[gap, Text(error!, style: TextStyle(color: theme.colorScheme.error))],
        gap,
        Row(children: [
          Expanded(child: _big(Texts.t('hr.prev'), sending ? null : () => _go(path.prev))),
          const SizedBox(width: 8),
          Expanded(flex: 2, child: _big(Texts.t('hr.send'), sending ? null : _send, primary: true)),
        ]),
      ]);
    }
    children.addAll([
      const SizedBox(height: 6),
      TextButton(style: ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48))), onPressed: sending ? null : _later, child: Text(Texts.t('hr.later'))),
      Text(Texts.t('hr.later.help'), style: theme.textTheme.bodySmall),
    ]);
    final typing = r != null && !r.approve && r.kind != 'choice';
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && !sending) _later();
      },
      child: CallbackShortcuts(
        bindings: typing
            ? const {}
            : {
                const SingleActivator(LogicalKeyboardKey.arrowRight): () {
                  if (!path.inSummary && !sending) _next();
                },
                const SingleActivator(LogicalKeyboardKey.arrowLeft): () {
                  if (path.step > 0 && !sending) _go(path.prev);
                },
              },
        child: Focus(
          autofocus: !typing,
          child: GestureDetector(
            behavior: HitTestBehavior.translucent,
            onHorizontalDragEnd: (d) {
              final v = d.primaryVelocity ?? 0;
              if (sending) return;
              if (v < -300 && !path.inSummary) _next();
              if (v > 300 && path.step > 0) _go(path.prev);
            },
            child: Dialog(
              insetPadding: const EdgeInsets.all(16),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 560),
                child: SingleChildScrollView(padding: const EdgeInsets.all(16), child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: children)),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
